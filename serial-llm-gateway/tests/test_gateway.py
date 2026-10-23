"""End-to-end tests: a real gateway and a fake upstream, each served by uvicorn on localhost.

The fake upstream counts how many inferences are "on the GPU" at once; every test asserts
that the maximum observed upstream concurrency is exactly 1.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from dataclasses import replace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.main import Settings, create_app

pytestmark = pytest.mark.anyio

ADMIN = {"authorization": "Bearer admin-key"}
TRUSTED = {"authorization": "Bearer trusted-key"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    def __init__(self, app, **config):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", **config))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            assert time.time() < deadline, "server did not start"
            time.sleep(0.01)
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=15)


class FakeUpstream:
    """Minimal OpenAI-ish server. Request field `fake` controls its behaviour."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.started: list[str] = []
        self.finished: list[str] = []
        self.bodies: list[dict] = []
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self.chat)
        self.app.get("/v1/models")(lambda: {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

    def enter(self, label):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.append(label)

    def exit(self, label):
        with self.lock:
            self.active -= 1
            self.finished.append(label)

    @staticmethod
    def sse_chunks(label, n):
        for i in range(n):
            chunk = {"id": f"c-{label}", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": f"{label}{i} "}}]}
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def chat(self, request: Request):
        body = await request.json()
        opts = body.get("fake", {})
        label, n, delay = opts.get("label", "?"), opts.get("chunks", 3), opts.get("delay", 0.05)
        linger, fail_after = opts.get("linger", 0), opts.get("fail_after")
        with self.lock:
            self.bodies.append(body)
        self.enter(label)
        if opts.get("status", 200) != 200:
            self.exit(label)
            return JSONResponse({"error": {"message": "fake upstream failure", "type": "server_error"}}, status_code=opts["status"])
        if not body.get("stream"):
            try:  # like a real server, a disconnect does not stop non-streaming generation
                await asyncio.sleep(n * delay)
            finally:
                self.exit(label)
            return {"id": f"c-{label}", "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": label}, "finish_reason": "stop"}]}

        async def gen():
            completed = False
            try:
                for i, chunk in enumerate(self.sse_chunks(label, n)):
                    await asyncio.sleep(delay)
                    if fail_after is not None and i == fail_after:
                        raise RuntimeError("fake upstream crashed mid-stream")
                    yield chunk
                completed = True
            finally:
                if linger and not completed:  # the "GPU" keeps working after the connection drops
                    asyncio.get_running_loop().call_later(linger, self.exit, label)
                else:
                    self.exit(label)

        return StreamingResponse(gen(), media_type="text/event-stream")


@pytest.fixture
def upstream():
    fake = FakeUpstream()
    server = ServerThread(fake.app).start()
    fake.url = server.url
    yield fake
    server.stop()
    assert fake.max_active <= 1, f"overlapping upstream inference: {fake.max_active}"


class Gateway:
    def __init__(self, settings):
        self.app = create_app(settings)
        self.scheduler = self.app.state.scheduler
        self.server = ServerThread(self.app, timeout_graceful_shutdown=1).start()
        self.url = self.server.url


@pytest.fixture
def make_gateway(upstream):
    gateways = []
    base = Settings(upstream_url=upstream.url, queue_timeout_seconds=10, upstream_timeout_seconds=10,
                    recovery_cooldown_seconds=0.2, disconnect_poll_seconds=0.05, shutdown_grace_seconds=0.5,
                    priority_aging_seconds=0, reserved_priority_slots=0,
                    trusted_api_keys="trusted-key", admin_api_key="admin-key")

    def factory(**overrides):
        gateway = Gateway(replace(base, **overrides))
        gateways.append(gateway)
        return gateway

    yield factory
    for gateway in gateways:
        gateway.server.stop()


def req(label, stream=False, chunks=3, delay=0.05, **fake):
    return {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}], "stream": stream,
            "fake": {"label": label, "chunks": chunks, "delay": delay, **fake}}


async def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


async def status(client):
    return (await client.get("/status")).json()


async def post(client, body, headers=None):
    return await client.post("/v1/chat/completions", json=body, headers=headers or {})


async def stream_text(client, body, headers=None):
    async with client.stream("POST", "/v1/chat/completions", json=body, headers=headers or {}) as r:
        return r.status_code, b"".join([c async for c in r.aiter_raw()])


async def submit_in_order(client, gateway, bodies, headers=None):
    """Start requests one at a time, each only once the previous one is queued or running."""
    tasks = []
    for i, body in enumerate(bodies):
        coro = stream_text(client, body, headers and headers[i]) if body["stream"] else post(client, body, headers and headers[i])
        tasks.append(asyncio.create_task(coro))
        await wait_for(lambda: len(gateway.scheduler.jobs) == i + 1)
    return tasks


@pytest.fixture
async def client_for():
    clients = []

    def factory(gateway):
        c = httpx.AsyncClient(base_url=gateway.url, timeout=30)
        clients.append(c)
        return c

    yield factory
    for c in clients:
        await c.aclose()


# ---- 1-3: serial execution, shared queue, FIFO -------------------------------------------


async def test_five_simultaneous_requests_run_strictly_sequentially(upstream, make_gateway, client_for):
    client = client_for(make_gateway())
    results = await asyncio.gather(*[post(client, req(f"r{i}", delay=0.05)) for i in range(5)])
    assert [r.status_code for r in results] == [200] * 5
    assert sorted(upstream.started) == [f"r{i}" for i in range(5)]
    assert upstream.max_active == 1
    assert upstream.started == upstream.finished  # each finished before the next started


async def test_streaming_and_non_streaming_share_one_queue(upstream, make_gateway, client_for):
    gateway = make_gateway()
    client = client_for(gateway)
    bodies = [req("s1", stream=True), req("n1"), req("s2", stream=True), req("n2")]
    tasks = await submit_in_order(client, gateway, bodies)
    assert gateway.scheduler.status()["waiting"] == 3
    results = await asyncio.gather(*tasks)
    assert results[1].status_code == 200 and results[0][0] == 200
    assert upstream.started == ["s1", "n1", "s2", "n2"]
    assert upstream.max_active == 1


async def test_fifo_order(upstream, make_gateway, client_for):
    gateway = make_gateway()
    client = client_for(gateway)
    labels = [f"f{i}" for i in range(6)]
    await asyncio.gather(*await submit_in_order(client, gateway, [req(label) for label in labels]))
    assert upstream.started == labels
    assert upstream.max_active == 1


# ---- 4, 7: admission limits --------------------------------------------------------------


async def test_queue_overflow_returns_503_with_retry_after(upstream, make_gateway, client_for):
    gateway = make_gateway(max_queue_size=2)
    client = client_for(gateway)
    tasks = await submit_in_order(client, gateway, [req("a", delay=0.2), req("b"), req("c")])
    rejected = await post(client, req("d"))
    assert rejected.status_code == 503
    assert rejected.headers["retry-after"] == "30"
    assert rejected.json()["error"]["code"] == "queue_full"
    await asyncio.gather(*tasks)
    assert "d" not in upstream.started
    assert (await status(client))["total_rejected"] == 1


@pytest.mark.parametrize("reserved, served", [(2, 7), (0, 9)])
async def test_burst_of_eleven_routine_requests(upstream, make_gateway, client_for, reserved, served):
    # MAX_QUEUE_SIZE=8 counts waiting requests only. With the default of 2 reserved places routine jobs
    # may fill 6 of them: 1 running + 6 waiting = 7 served, 4 rejected. With none reserved: 9 and 2.
    client = client_for(make_gateway(max_queue_size=8, reserved_priority_slots=reserved))
    results = await asyncio.gather(*[post(client, req(f"b{i}", delay=0.3)) for i in range(11)])
    assert sorted(r.status_code for r in results) == [200] * served + [503] * (11 - served)
    assert len(upstream.started) == served


async def test_queue_timeout(upstream, make_gateway, client_for):
    gateway = make_gateway(queue_timeout_seconds=0.3)
    client = client_for(gateway)
    first, second = await submit_in_order(client, gateway, [req("long", chunks=10, delay=0.1), req("late")])
    late = await second
    assert late.status_code == 503 and late.json()["error"]["code"] == "queue_timeout"
    assert (await first).status_code == 200
    assert upstream.started == ["long"]
    assert (await status(client))["total_timed_out"] == 1


async def test_reserved_slots_keep_room_for_escalated_jobs(upstream, make_gateway, client_for):
    gateway = make_gateway(max_queue_size=3, reserved_priority_slots=1)
    client = client_for(gateway)
    tasks = await submit_in_order(client, gateway, [req("run", delay=0.2), req("q1"), req("q2")])
    assert (await post(client, req("q3"))).status_code == 503  # routine may only use 2 of 3 places
    urgent = asyncio.create_task(post(client, req("u"), {"x-priority": "urgent", **TRUSTED}))
    assert all(r.status_code == 200 for r in await asyncio.gather(*tasks, urgent))
    assert upstream.started == ["run", "u", "q1", "q2"]


async def test_oversized_body_rejected_with_413(upstream, make_gateway, client_for):
    client = client_for(make_gateway(max_request_bytes=1000))
    body = req("big")
    body["messages"][0]["content"] = "x" * 2000
    response = await post(client, body)
    assert response.status_code == 413
    assert upstream.started == []


# ---- 5, 6: cancellation ------------------------------------------------------------------


async def test_cancelled_queued_request_never_reaches_upstream(upstream, make_gateway, client_for):
    gateway = make_gateway()
    client = client_for(gateway)
    tasks = await submit_in_order(client, gateway, [req("a", chunks=6, delay=0.1), req("gone", stream=True), req("b")])
    tasks[1].cancel()  # closes the client connection while it is queued
    await wait_for(lambda: gateway.scheduler.jobs and gateway.scheduler.status()["waiting"] == 1)
    await asyncio.gather(tasks[0], tasks[2])
    assert upstream.started == ["a", "b"]
    gone = next(j for j in gateway.scheduler.jobs.values() if j.state == "client_disconnected")
    assert gone.started_at is None


async def test_active_disconnect_drains_before_releasing_slot(upstream, make_gateway, client_for):
    gateway = make_gateway(stop_policy="drain")
    client = client_for(gateway)

    async def read_one_chunk_then_leave():
        async with client.stream("POST", "/v1/chat/completions", json=req("a", stream=True, chunks=8, delay=0.1)) as r:
            async for _ in r.aiter_raw():
                return

    leaver = asyncio.create_task(read_one_chunk_then_leave())
    await wait_for(lambda: upstream.started == ["a"])
    nxt = asyncio.create_task(post(client, req("b")))
    await leaver
    await wait_for(lambda: len(gateway.scheduler.jobs) == 2)
    await asyncio.sleep(0.3)
    assert upstream.started == ["a"], "next job must wait until the abandoned stream is drained"
    assert (await nxt).status_code == 200
    assert upstream.finished == ["a", "b"]
    first = list(gateway.scheduler.jobs.values())[0]
    assert first.state == "completed" and "discarded" in first.reason
    assert upstream.max_active == 1


async def test_active_disconnect_abort_waits_for_recovery(upstream, make_gateway, client_for):
    # The fake GPU keeps computing for 0.4s after the connection is closed; the cooldown covers it.
    gateway = make_gateway(stop_policy="abort", recovery_cooldown_seconds=0.8)
    client = client_for(gateway)

    async def leave_early():
        async with client.stream("POST", "/v1/chat/completions", json=req("a", stream=True, chunks=50, delay=0.05, linger=0.4)) as r:
            async for _ in r.aiter_raw():
                return

    await leave_early()
    await wait_for(lambda: gateway.scheduler.backend == "recovering")
    assert (await post(client, req("b"))).status_code == 200
    assert upstream.started == ["a", "b"]
    assert upstream.max_active == 1
    assert list(gateway.scheduler.jobs.values())[0].state == "client_disconnected"


async def test_user_cancel_of_running_stream(upstream, make_gateway, client_for):
    gateway = make_gateway()
    client = client_for(gateway)
    headers = {"x-job-id": "job-aaaa-0001"}
    running = asyncio.create_task(stream_text(client, req("a", stream=True, chunks=40, delay=0.05), headers))
    await wait_for(lambda: upstream.started == ["a"])
    waiting = asyncio.create_task(post(client, req("b")))
    await wait_for(lambda: gateway.scheduler.status()["waiting"] == 1)

    response = await client.post("/user/cancel/job-aaaa-0001")
    assert response.status_code == 202
    code, text = await running
    assert code == 200
    assert b'"type": "job_cancelled"' in text and b"[DONE]" not in text
    assert (await waiting).status_code == 200
    assert upstream.started == ["a", "b"]
    job = (await client.get("/jobs/job-aaaa-0001")).json()
    assert job["state"] == "cancelled_by_user" and "output was discarded" in job["reason"]
    assert upstream.finished == ["a", "b"]  # drained to the end before "b" started


async def test_user_cancel_of_queued_job_returns_410(upstream, make_gateway, client_for):
    gateway = make_gateway()
    client = client_for(gateway)
    tasks = await submit_in_order(client, gateway, [req("a", delay=0.1), req("b")], [{}, {"x-job-id": "job-bbbb-0002"}])
    assert (await client.post("/user/cancel/job-bbbb-0002")).status_code == 202
    cancelled = await tasks[1]
    assert cancelled.status_code == 410
    assert cancelled.headers["x-should-retry"] == "false"
    assert cancelled.json()["error"]["type"] == "job_cancelled"
    await tasks[0]
    assert upstream.started == ["a"]


async def test_admin_skip_current_and_reset_all(upstream, make_gateway, client_for):
    gateway = make_gateway()
    client = client_for(gateway)
    assert (await client.post("/admin/skip-current")).status_code == 403
    tasks = await submit_in_order(client, gateway, [req("a", chunks=40), req("b", chunks=40), req("c"), req("d")])
    skipped = await client.post("/admin/skip-current", headers=ADMIN)
    assert skipped.json()["skipped"] is not None
    assert (await tasks[0]).status_code == 410
    await wait_for(lambda: upstream.started == ["a", "b"])

    assert (await client.post("/admin/reset-all", headers=ADMIN)).json() == {"stopped": 3}
    results = await asyncio.gather(*tasks[1:])
    assert [r.status_code for r in results] == [410, 410, 410]
    assert gateway.scheduler.active.state == "stopping"  # caller released, upstream still draining
    await wait_for(lambda: gateway.scheduler.active is None)
    assert upstream.started == ["a", "b"] and upstream.finished == ["a", "b"]
    states = sorted(j.state for j in gateway.scheduler.jobs.values())
    assert states == ["reset_by_admin"] * 3 + ["skipped_by_admin"]


# ---- 8: upstream failures ----------------------------------------------------------------


async def test_upstream_error_status_and_body_are_forwarded(upstream, make_gateway, client_for):
    client = client_for(make_gateway())
    response = await post(client, req("bad", status=500))
    assert response.status_code == 500
    assert response.json() == {"error": {"message": "fake upstream failure", "type": "server_error"}}
    assert (await status(client))["backend"] == "ready"  # the upstream answered, so it is not generating


async def test_upstream_unreachable_returns_502(upstream, make_gateway, client_for):
    gateway = make_gateway(upstream_url=f"http://127.0.0.1:{free_port()}")
    client = client_for(gateway)
    response = await post(client, req("x"))
    assert response.status_code == 502 and response.json()["error"]["code"] == "upstream_unavailable"
    assert gateway.scheduler.backend == "ready"  # nothing was ever connected


async def test_upstream_read_timeout_returns_504_and_waits_for_recovery(upstream, make_gateway, client_for):
    # The fake keeps "generating" for 1s after the gateway gives up at 0.3s.
    gateway = make_gateway(upstream_timeout_seconds=0.3, recovery_cooldown_seconds=1.0)
    client = client_for(gateway)
    response = await post(client, req("slow", chunks=10, delay=0.1))
    assert response.status_code == 504
    assert gateway.scheduler.backend == "recovering"
    assert (await post(client, req("next"))).status_code == 200
    assert upstream.max_active == 1


async def test_mid_stream_upstream_failure_has_no_fake_done(upstream, make_gateway, client_for):
    gateway = make_gateway()
    client = client_for(gateway)
    received = b""
    with pytest.raises(httpx.HTTPError):
        async with client.stream("POST", "/v1/chat/completions", json=req("boom", stream=True, chunks=5, fail_after=2)) as r:
            async for chunk in r.aiter_raw():
                received += chunk
    assert b"boom0" in received and b"[DONE]" not in received
    await wait_for(lambda: gateway.scheduler.active is None)
    assert list(gateway.scheduler.jobs.values())[0].state == "failed"


# ---- 9: transparency ---------------------------------------------------------------------


async def test_streaming_sse_bytes_are_preserved(upstream, make_gateway, client_for):
    client = client_for(make_gateway())
    body = req("sse", stream=True, chunks=4, delay=0.01)
    code, via_gateway = await stream_text(client, body)
    async with httpx.AsyncClient(base_url=upstream.url) as direct:
        _, direct_bytes = await stream_text(direct, body)
    assert code == 200
    assert via_gateway == direct_bytes == b"".join(FakeUpstream.sse_chunks("sse", 4))


async def test_unknown_fields_forwarded_and_models_proxied(upstream, make_gateway, client_for):
    client = client_for(make_gateway())
    body = req("x")
    body["custom_vendor_field"] = {"nested": [1, 2, 3]}
    body["temperature"] = 0.3
    response = await post(client, body)
    assert response.status_code == 200 and response.headers["x-job-id"]
    assert upstream.bodies[0] == body
    models = await client.get("/v1/models")
    assert models.status_code == 200 and models.json()["data"][0]["id"] == "fake-model"


# ---- 10, 11: shutdown and backend uncertainty --------------------------------------------


async def test_shutdown_does_not_start_overlapping_requests(upstream, make_gateway, client_for):
    gateway = make_gateway()
    client = client_for(gateway)
    tasks = await submit_in_order(client, gateway, [req("a", stream=True, chunks=200, delay=0.05), req("b"), req("c", stream=True)])
    await asyncio.to_thread(gateway.server.stop)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(not isinstance(r, httpx.Response) or r.status_code >= 500 for r in results[1:])
    await wait_for(lambda: upstream.active == 0)
    assert upstream.started == ["a"]
    assert upstream.max_active == 1
    assert gateway.scheduler.backend == "shutting_down"


async def test_backend_uncertainty_blocks_new_inference_until_recover(upstream, make_gateway, client_for):
    gateway = make_gateway(auto_recover=False, stop_policy="abort")
    client = client_for(gateway)
    running = asyncio.create_task(post(client, req("a", chunks=20, delay=0.05), {"x-job-id": "job-cccc-0003"}))
    await wait_for(lambda: upstream.started == ["a"])
    await client.post("/user/cancel/job-cccc-0003")
    assert (await running).status_code == 410
    assert (await status(client))["backend"] == "uncertain"

    blocked = await post(client, req("b"))
    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "backend_unavailable"
    assert (await client.post("/admin/recover", headers=ADMIN)).json() == {"recovered": True, "backend": "ready"}
    await wait_for(lambda: upstream.active == 0)
    assert (await post(client, req("c"))).status_code == 200
    assert upstream.started == ["a", "c"]


# ---- 12: status --------------------------------------------------------------------------


async def test_status_reports_running_and_waiting(upstream, make_gateway, client_for):
    gateway = make_gateway()
    client = client_for(gateway)
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    tasks = await submit_in_order(client, gateway, [req("a", chunks=20, delay=0.1), req("b"), req("c", stream=True)])
    await asyncio.sleep(1.1)
    s = await status(client)
    assert (s["backend"], s["running"], s["waiting"], s["max_queue_size"]) == ("ready", 1, 2, 8)
    assert s["oldest_wait_seconds"] >= 1
    assert s["waiting_by_priority"]["routine"] == 2
    assert "hi" not in json.dumps(s)  # no prompt content
    await asyncio.gather(*tasks)
    s = await status(client)
    assert (s["running"], s["waiting"], s["total_completed"]) == (0, 0, 3)


# ---- priority tiers ----------------------------------------------------------------------


async def test_priority_tiers_and_untrusted_callers(upstream, make_gateway, client_for):
    gateway = make_gateway()
    client = client_for(gateway)
    plan = [
        ("run", {}),
        ("r1", {}),
        ("u", {"x-priority": "urgent", **TRUSTED}),
        ("fake-urgent", {"x-priority": "urgent,important"}),  # not trusted -> routine
        ("i", {"x-priority": "important", **TRUSTED}),
        ("ui", {"x-priority": "urgent,important", **TRUSTED}),
        ("r2", {}),
    ]
    tasks = await submit_in_order(client, gateway, [req(label, delay=0.1 if label == "run" else 0.01) for label, _ in plan],
                                  [h for _, h in plan])
    await asyncio.gather(*tasks)
    assert upstream.started == ["run", "ui", "u", "i", "r1", "fake-urgent", "r2"]
    ignored = next(j for j in gateway.scheduler.jobs.values() if "not trusted" in " ".join(j.notes))
    assert ignored.flags == frozenset()


async def test_trusted_caller_can_escalate_queued_job(upstream, make_gateway, client_for):
    gateway = make_gateway()
    client = client_for(gateway)
    tasks = await submit_in_order(client, gateway, [req("run", delay=0.2), req("r1"), req("r2")],
                                  [{}, {}, {"x-job-id": "job-dddd-0004"}])
    assert (await client.post("/user/priority/job-dddd-0004", json={"priority": "urgent"})).status_code == 403
    escalated = await client.post("/user/priority/job-dddd-0004", json={"priority": "urgent"}, headers=TRUSTED)
    assert escalated.status_code == 200
    assert escalated.json()["queue_position"] == 1 and escalated.json()["priority"] == "urgent"
    await asyncio.gather(*tasks)
    assert upstream.started == ["run", "r2", "r1"]


async def test_priority_aging_prevents_starvation(upstream, make_gateway, client_for):
    gateway = make_gateway(priority_aging_seconds=0.3)
    client = client_for(gateway)
    tasks = await submit_in_order(client, gateway, [req("run", chunks=8, delay=0.1), req("old-routine")])
    await asyncio.sleep(0.4)  # old-routine has aged into the single-flag tier
    tasks.append(asyncio.create_task(post(client, req("new-urgent"), {"x-priority": "urgent", **TRUSTED})))
    await asyncio.gather(*tasks)
    assert upstream.started == ["run", "old-routine", "new-urgent"]


async def test_gateway_api_key_required_when_configured(upstream, make_gateway, client_for):
    client = client_for(make_gateway(gateway_api_key="caller-key"))
    assert (await post(client, req("x"))).status_code == 401
    assert (await post(client, req("x"), {"authorization": "Bearer caller-key"})).status_code == 200
    assert (await post(client, req("y"), TRUSTED)).status_code == 200
    assert upstream.started == ["x", "y"]
