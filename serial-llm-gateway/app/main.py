"""OpenAI-compatible gateway that serialises inference against a single local model server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .scheduler import ClientGone, Job, QueueTimeout, Rejected, Scheduler, Stopped, parse_priority, priority_label

log = logging.getLogger("gateway")

JOB_ID_RE = re.compile(r"[A-Za-z0-9._-]{8,128}")
# Hop-by-hop and per-connection headers that must not be copied between connections.
DROP_RESPONSE_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "date", "server",
}
STOP_MESSAGES = {
    "cancelled_by_user": "cancelled by the requester",
    "skipped_by_admin": "skipped by an administrator (POST /admin/skip-current)",
    "reset_by_admin": "stopped by an administrator (POST /admin/reset-all)",
    "client_disconnected": "client disconnected",
    "shutdown": "gateway is shutting down",
}


@dataclass(frozen=True)
class Settings:
    upstream_url: str = "http://host.docker.internal:8000"
    max_queue_size: int = 8
    queue_timeout_seconds: float = 600
    upstream_timeout_seconds: float = 1200
    upstream_connect_timeout_seconds: float = 10
    max_request_bytes: int = 1_048_576
    stop_policy: str = "drain"  # drain | abort: what a disconnect or cancel does to a running job
    recovery_cooldown_seconds: float = 10
    recovery_attempts: int = 3
    auto_recover: bool = True
    priority_aging_seconds: float = 300
    reserved_priority_slots: int = 2
    retry_after_seconds: int = 30
    shutdown_grace_seconds: float = 15
    disconnect_poll_seconds: float = 0.5
    gateway_api_key: str = ""
    trusted_api_keys: str = ""  # comma separated
    admin_api_key: str = ""
    upstream_api_key: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        values = {}
        for f in fields(cls):
            raw = os.environ.get(f.name.upper())
            if raw is None:
                continue
            if f.type == "bool":
                values[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif f.type in ("int", "float"):
                values[f.name] = (int if f.type == "int" else float)(raw)
            else:
                values[f.name] = raw.strip()
        settings = cls(**values)
        if settings.stop_policy not in ("drain", "abort"):
            raise ValueError("STOP_POLICY must be 'drain' or 'abort'")
        return settings


class UpstreamFailed(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Relay:
    """Hands an upstream response from the job's worker task to the client-facing handler."""

    END = object()

    def __init__(self) -> None:
        self.started: asyncio.Future = asyncio.get_running_loop().create_future()
        self.started.add_done_callback(lambda f: f.cancelled() or f.exception())  # never "unretrieved"
        self.chunks: asyncio.Queue = asyncio.Queue()
        self.client_gone = False

    def fail(self, exc: Exception) -> None:
        if not self.started.done():
            self.started.set_exception(exc)
        elif not self.client_gone:
            self.chunks.put_nowait(exc)

    def abandon(self) -> None:
        self.client_gone = True
        while not self.chunks.empty():
            self.chunks.get_nowait()

    def detach(self, exc: Exception) -> None:
        """Tell the caller its job was stopped; further upstream output is discarded."""
        self.abandon()
        if not self.started.done():
            self.started.set_exception(exc)
        else:
            self.chunks.put_nowait(exc)


def error_response(status: int, code: str, message: str, *, job: Job | None = None,
                   retry_after: int | None = None, no_retry: bool = False) -> JSONResponse:
    headers = {}
    if job is not None:
        headers["x-job-id"] = job.id
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    if no_retry:
        headers["x-should-retry"] = "false"  # honoured by the official OpenAI SDKs
    body = {"error": {"message": message, "type": code, "code": code, "param": None}}
    if job is not None:
        body["error"]["job_id"] = job.id
    return JSONResponse(body, status_code=status, headers=headers)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    upstream = settings.upstream_url.rstrip("/")
    trusted = [k.strip() for k in settings.trusted_api_keys.split(",") if k.strip()]
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.upstream_timeout_seconds, connect=settings.upstream_connect_timeout_seconds),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )
    upstream_headers = {"accept-encoding": "identity"}
    if settings.upstream_api_key:
        upstream_headers["authorization"] = f"Bearer {settings.upstream_api_key}"

    async def probe() -> bool:
        response = await client.get(f"{upstream}/v1/models", headers=upstream_headers, timeout=10)
        return response.status_code < 500

    scheduler = Scheduler(
        max_queue_size=settings.max_queue_size,
        queue_timeout=settings.queue_timeout_seconds,
        reserved_priority_slots=settings.reserved_priority_slots,
        priority_aging=settings.priority_aging_seconds,
        recovery_cooldown=settings.recovery_cooldown_seconds,
        recovery_attempts=settings.recovery_attempts,
        auto_recover=settings.auto_recover,
        retry_after=settings.retry_after_seconds,
        poll_interval=settings.disconnect_poll_seconds,
        probe=probe,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("gateway up: upstream=%s max_queue=%d stop_policy=%s", upstream, settings.max_queue_size, settings.stop_policy)
        yield
        await scheduler.shutdown(settings.shutdown_grace_seconds)
        await client.aclose()

    app = FastAPI(title="serial-llm-gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.scheduler = scheduler
    app.state.settings = settings

    # ---- auth ----------------------------------------------------------------------------

    def role_of(request: Request) -> str | None:
        """Return "admin", "trusted", "caller", or None if the caller is not authenticated."""
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        match = lambda key: bool(key) and secrets.compare_digest(token.encode(), key.encode())  # noqa: E731
        if match(settings.admin_api_key):
            return "admin"
        if any(match(k) for k in trusted):
            return "trusted"
        if not settings.gateway_api_key or match(settings.gateway_api_key):
            return "caller"
        return None

    def unauthorized() -> JSONResponse:
        return error_response(401, "invalid_api_key", "missing or invalid gateway API key")

    def require_admin(request: Request) -> JSONResponse | None:
        if not settings.admin_api_key:
            return error_response(403, "admin_disabled", "admin endpoints are disabled (ADMIN_API_KEY is not set)")
        if role_of(request) != "admin":
            return error_response(403, "forbidden", "admin API key required")
        return None

    # ---- upstream worker -----------------------------------------------------------------

    async def run_upstream(job: Job, body: bytes, relay: Relay) -> None:
        """Owns the upstream response for one job; always releases the slot exactly once."""
        state, reason, clean, finished = "failed", None, False, False
        response: httpx.Response | None = None
        try:
            if job.stop_reason:  # stopped between being granted the slot and starting
                state, clean = job.stop_reason, True
                relay.fail(Stopped(job.stop_reason))
                return
            request = client.build_request(
                "POST", f"{upstream}/v1/chat/completions", content=body,
                headers={**upstream_headers, "content-type": "application/json"},
            )
            async with asyncio.timeout(settings.upstream_timeout_seconds):
                try:
                    response = await client.send(request, stream=True)
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                    clean = True  # no connection was made, so nothing can be generating
                    raise
                job.status_code = response.status_code
                if not relay.started.done():  # may already hold Stopped from a detach
                    relay.started.set_result(response)
                async for chunk in response.aiter_raw():
                    if not relay.client_gone:
                        relay.chunks.put_nowait(chunk)
                finished = True
            clean = True
            if job.stop_reason:
                state, reason = job.stop_reason, "stopped before completion; upstream finished and its output was discarded"
            elif response.status_code < 400:
                state = "completed"
            else:
                state, reason = "failed", f"upstream returned HTTP {response.status_code}"
            if relay.client_gone and not job.stop_reason:
                reason = f"{reason + '; ' if reason else ''}client disconnected, output discarded"
            relay.chunks.put_nowait(Relay.END)
        except asyncio.CancelledError:
            state = job.stop_reason or "shutdown"
            reason = "stopped before completion; upstream connection closed"
            clean = finished
            relay.fail(Stopped(state))
        except (TimeoutError, httpx.TimeoutException) as exc:
            state = "timed_out"
            reason = f"upstream {'connect' if isinstance(exc, httpx.ConnectTimeout) else 'read'} timeout"
            relay.fail(UpstreamFailed(504, "upstream_timeout", f"upstream model server timed out ({reason})"))
        except httpx.HTTPError as exc:
            reason = f"upstream error: {type(exc).__name__}"
            relay.fail(UpstreamFailed(502, "upstream_unavailable", f"upstream model server error ({type(exc).__name__})"))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("job %s: unexpected gateway error", job.id)
            reason = f"gateway error: {type(exc).__name__}"
            relay.fail(UpstreamFailed(502, "gateway_error", "unexpected gateway error"))
        finally:
            if response is not None:
                try:
                    await asyncio.wait_for(response.aclose(), 5)
                except Exception:
                    log.exception("job %s: failed to close upstream response", job.id)
                    clean = False
            scheduler.release(job, state, reason, clean=clean)

    def client_left(job: Job, relay: Relay) -> None:
        relay.abandon()
        if settings.stop_policy == "abort":
            scheduler.stop(job.id, "client_disconnected", force=True)
        else:
            log.info("job %s: client disconnected; draining upstream response", job.id)

    async def wait_started(request: Request, relay: Relay) -> httpx.Response:
        while True:
            done, _ = await asyncio.wait({relay.started}, timeout=settings.disconnect_poll_seconds)
            if done:
                return relay.started.result()
            if await request.is_disconnected():
                raise ClientGone()

    def stopped_response(job: Job, state: str) -> JSONResponse:
        message = f"job {job.id} did not finish: {STOP_MESSAGES.get(state, state)}"
        if state == "shutdown":
            return error_response(503, "gateway_shutting_down", message, job=job, retry_after=settings.retry_after_seconds)
        return error_response(410, "job_cancelled", message, job=job, no_retry=True)

    async def relay_body(job: Job, relay: Relay, sse: bool):
        ended = False
        try:
            while True:
                item = await relay.chunks.get()
                if item is Relay.END:
                    ended = True
                    return
                if isinstance(item, Stopped):
                    ended = True
                    if sse:  # an OpenAI-style error event; the SDKs raise on it. No [DONE] follows.
                        payload = {"error": {"message": f"job {job.id} did not finish: {STOP_MESSAGES.get(item.state, item.state)}",
                                             "type": "job_cancelled", "code": item.state, "job_id": job.id}}
                        yield f"data: {json.dumps(payload)}\n\n".encode()
                        return
                    raise item
                if isinstance(item, Exception):
                    ended = True
                    log.warning("job %s: upstream failed mid-response; aborting client stream", job.id)
                    raise item
                yield item
        finally:
            if not ended:
                client_left(job, relay)

    # ---- routes --------------------------------------------------------------------------

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        role = role_of(request)
        if role is None:
            return unauthorized()

        limit = settings.max_request_bytes
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            return error_response(413, "request_too_large", f"request body exceeds {limit} bytes")
        parts, size = [], 0
        async for part in request.stream():
            size += len(part)
            if size > limit:
                return error_response(413, "request_too_large", f"request body exceeds {limit} bytes")
            parts.append(part)
        body = b"".join(parts)
        del parts
        try:
            payload = json.loads(body)
        except ValueError:
            return error_response(400, "invalid_request_error", "request body must be valid JSON")
        if not isinstance(payload, dict):
            return error_response(400, "invalid_request_error", "request body must be a JSON object")
        stream = payload.get("stream") is True
        del payload  # the raw bytes are forwarded unchanged

        try:
            flags = parse_priority(request.headers.get("x-priority"))
        except ValueError as exc:
            return error_response(400, "invalid_priority", str(exc))
        job_id = request.headers.get("x-job-id") or uuid.uuid4().hex
        if not JOB_ID_RE.fullmatch(job_id):
            return error_response(400, "invalid_job_id", "X-Job-Id must be 8-128 characters of [A-Za-z0-9._-]")
        job = Job(id=job_id, stream=stream)
        if flags and role == "caller":
            job.note(f"requested priority {priority_label(flags)} ignored: caller is not trusted")
        elif flags:
            job.flags = flags

        try:
            await scheduler.acquire(job, request.is_disconnected)
        except Rejected as exc:
            return error_response(exc.status, exc.code, exc.message, job=job, retry_after=exc.retry_after)
        except QueueTimeout:
            return error_response(503, "queue_timeout", f"waited {settings.queue_timeout_seconds:g}s in the queue without starting",
                                  job=job, retry_after=settings.retry_after_seconds)
        except Stopped as exc:
            return stopped_response(job, exc.state)
        except ClientGone:
            return Response(status_code=499)

        # This job now owns the upstream slot; the worker task releases it.
        relay = Relay()
        job.detach = lambda state: relay.detach(Stopped(state))
        job.worker = asyncio.create_task(run_upstream(job, body, relay))
        try:
            response = await wait_started(request, relay)
        except Stopped as exc:
            return stopped_response(job, exc.state)
        except UpstreamFailed as exc:
            return error_response(exc.status, exc.code, exc.message, job=job)
        except (ClientGone, asyncio.CancelledError) as exc:  # client left before the response started
            client_left(job, relay)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return Response(status_code=499)

        headers = {k: v for k, v in response.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS}
        headers["x-job-id"] = job.id
        sse = response.headers.get("content-type", "").startswith("text/event-stream")
        return StreamingResponse(relay_body(job, relay, sse), status_code=response.status_code, headers=headers)

    @app.get("/v1/models")
    async def models(request: Request):
        if role_of(request) is None:
            return unauthorized()
        try:
            response = await client.get(f"{upstream}/v1/models", headers=upstream_headers, timeout=10)
        except httpx.HTTPError as exc:
            return error_response(502, "upstream_unavailable", f"upstream model server unreachable ({type(exc).__name__})")
        return Response(response.content, status_code=response.status_code,
                        media_type=response.headers.get("content-type", "application/json"))

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/status")
    async def status():
        return scheduler.status()

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        if role_of(request) is None:
            return unauthorized()
        job = scheduler.jobs.get(job_id)
        if job is None:
            return error_response(404, "job_not_found", "unknown job id (job history is in memory only)")
        return scheduler.describe(job)

    def force_requested(request: Request) -> bool:
        return settings.stop_policy == "abort" or request.query_params.get("force", "").lower() in ("1", "true", "yes")

    def stop_job(job_id: str, state: str, force: bool) -> JSONResponse:
        job = scheduler.jobs.get(job_id)
        if job is None:
            return error_response(404, "job_not_found", "unknown job id")
        if job.finished_at is not None or job.stop_reason:
            return JSONResponse(scheduler.describe(job), status_code=409)
        scheduler.stop(job_id, state, force=force)
        return JSONResponse(scheduler.describe(job), status_code=202)

    @app.post("/user/cancel/{job_id}")
    async def user_cancel(job_id: str, request: Request):
        if role_of(request) is None:
            return unauthorized()
        return stop_job(job_id, "cancelled_by_user", settings.stop_policy == "abort")

    @app.post("/user/priority/{job_id}")
    async def user_priority(job_id: str, request: Request):
        role = role_of(request)
        if role is None:
            return unauthorized()
        if role not in ("trusted", "admin"):
            return error_response(403, "forbidden", "only trusted API keys can change priority")
        try:
            data = await request.json()
            flags = parse_priority(data.get("priority") if isinstance(data, dict) else None)
            job = scheduler.set_priority(job_id, flags, by=role)
        except KeyError:
            return error_response(404, "job_not_found", "unknown job id")
        except ValueError as exc:
            return error_response(409 if "re-prioritised" in str(exc) else 400, "invalid_priority", str(exc))
        return scheduler.describe(job)

    @app.post("/admin/skip-current")
    async def admin_skip_current(request: Request):
        if denied := require_admin(request):
            return denied
        job = scheduler.skip_current(force_requested(request))
        return {"skipped": None if job is None else scheduler.describe(job)}

    @app.post("/admin/reset-all")
    async def admin_reset_all(request: Request):
        if denied := require_admin(request):
            return denied
        return {"stopped": scheduler.reset_all(force_requested(request))}

    @app.post("/admin/recover")
    async def admin_recover(request: Request):
        if denied := require_admin(request):
            return denied
        return {"recovered": scheduler.recover(), "backend": scheduler.backend}

    return app


if not logging.getLogger().handlers:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = create_app()
