"""In-memory scheduler: one active upstream inference, a bounded priority/FIFO wait queue.

Invariant: `active` holds at most one job, and the slot is only handed to the next job
after the previous job's upstream response has been closed (`release`). If the gateway
cannot be sure the upstream stopped generating, the backend leaves the "ready" state and
nothing is dispatched until recovery (cooldown + probe) or an operator says so.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("gateway.scheduler")

# Urgent and important are independent flags of equal weight: level = number of flags set.
FLAGS = ("urgent", "important")
MAX_LEVEL = len(FLAGS)


def parse_priority(value: str | None) -> frozenset[str]:
    """Parse an X-Priority value such as "urgent", "important", "urgent,important" or "routine"."""
    if not value or not value.strip():
        return frozenset()
    tokens = {t for t in re.split(r"[^a-z]+", value.lower()) if t and t != "and"}
    if tokens == {"routine"}:
        return frozenset()
    if not tokens or not tokens <= set(FLAGS):
        raise ValueError(f"invalid priority {value!r}; use routine, urgent, important or urgent,important")
    return frozenset(tokens)


def priority_label(flags: frozenset[str]) -> str:
    return "+".join(f for f in FLAGS if f in flags) or "routine"


class Rejected(Exception):
    """The request was not admitted (queue full, backend unavailable, ...)."""

    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


class QueueTimeout(Exception):
    pass


class ClientGone(Exception):
    pass


class Stopped(Exception):
    """The job was stopped by its requester, an admin, or gateway shutdown."""

    def __init__(self, state: str):
        super().__init__(state)
        self.state = state


@dataclass(eq=False)
class Job:
    id: str
    flags: frozenset[str] = frozenset()
    stream: bool = False
    state: str = "queued"
    reason: str | None = None
    status_code: int | None = None
    notes: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    enqueued_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    seq: int = 0
    grant: asyncio.Future | None = None
    worker: asyncio.Task | None = None
    detach: Callable[[str], None] | None = None  # tells the caller "stopped" without closing upstream
    stop_reason: str | None = None

    @property
    def level(self) -> int:
        return len(self.flags)

    def note(self, text: str) -> None:
        self.notes.append(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {text}")


# Terminal job states and the counter each one increments.
_COUNTER = {"completed": "completed", "failed": "failed", "timed_out": "timed_out"}


class Scheduler:
    def __init__(
        self,
        *,
        max_queue_size: int,
        queue_timeout: float,
        reserved_priority_slots: int = 0,
        priority_aging: float = 0,
        recovery_cooldown: float = 10,
        recovery_attempts: int = 3,
        auto_recover: bool = True,
        retry_after: int = 30,
        poll_interval: float = 0.5,
        probe: Callable[[], Awaitable[bool]] | None = None,
        history_size: int = 200,
    ):
        self.max_queue_size = max_queue_size
        self.queue_timeout = queue_timeout
        self.reserved_priority_slots = max(0, min(reserved_priority_slots, max_queue_size - 1))
        self.priority_aging = priority_aging
        self.recovery_cooldown = recovery_cooldown
        self.recovery_attempts = max(1, recovery_attempts)
        self.auto_recover = auto_recover
        self.retry_after = retry_after
        self.poll_interval = poll_interval
        self._probe = probe
        self.history_size = history_size

        self.waiting: list[Job] = []
        self.active: Job | None = None
        self.backend = "ready"  # ready | recovering | uncertain | shutting_down
        self.backend_reason: str | None = None
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.counters = dict.fromkeys(("completed", "failed", "timed_out", "cancelled", "rejected"), 0)
        self._seq = itertools.count()
        self._recovery: asyncio.Task | None = None

    # ---- admission -------------------------------------------------------------------

    async def acquire(self, job: Job, is_gone: Callable[[], Awaitable[bool]] | None = None) -> None:
        """Queue `job` and return once it owns the upstream slot.

        On return the caller MUST eventually call `release(job, ...)`. On any exception the
        job no longer holds or waits for the slot.
        """
        if job.id in self.jobs:
            raise Rejected(409, "duplicate_job_id", f"job id {job.id} is already in use")
        if self.backend in ("uncertain", "shutting_down"):
            self.counters["rejected"] += 1
            raise Rejected(503, "backend_unavailable", self._unavailable_message(), self.retry_after)
        limit = self.max_queue_size if job.level else self.max_queue_size - self.reserved_priority_slots
        if len(self.waiting) >= limit:
            self.counters["rejected"] += 1
            raise Rejected(503, "queue_full", "gateway queue is full, retry later", self.retry_after)

        loop = asyncio.get_running_loop()
        job.seq = next(self._seq)
        job.grant = loop.create_future()
        self._remember(job)
        self.waiting.append(job)
        log.info("job %s queued priority=%s stream=%s waiting=%d", job.id, priority_label(job.flags), job.stream, len(self.waiting))
        self._dispatch()

        deadline = loop.time() + self.queue_timeout
        try:
            while not job.grant.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise QueueTimeout()
                # asyncio.wait never cancels the future, so `grant` keeps its true state.
                await asyncio.wait({job.grant}, timeout=min(self.poll_interval, remaining))
                if not job.grant.done() and is_gone is not None and await is_gone():
                    raise ClientGone()
            job.grant.result()  # raises Stopped if the job was stopped while queued
        except Stopped:
            raise
        except BaseException as exc:
            if job.grant.done() and not job.grant.cancelled() and job.grant.exception() is None:
                # We were handed the slot while leaving; upstream was never contacted.
                self.release(job, "cancelled", "caller left before the upstream request started", clean=True)
            elif job in self.waiting:
                self.waiting.remove(job)
                if isinstance(exc, QueueTimeout):
                    self._finish(job, "timed_out", "queue timeout")
                else:
                    self._finish(job, "client_disconnected", "caller left while queued; never sent upstream")
                self._dispatch()
            raise

    def release(self, job: Job, state: str, reason: str | None = None, *, clean: bool) -> None:
        """Free the slot. `clean=False` means upstream termination is not confirmed."""
        if self.active is not job:
            log.error("job %s released but is not the active job", job.id)
            return
        self.active = None
        self._finish(job, state, reason)
        if not clean:
            self._enter_recovery(f"job {job.id}: {reason or state}")
        self._dispatch()

    def _dispatch(self) -> None:
        if self.active is not None or self.backend != "ready":
            return
        candidates = [j for j in self.waiting if not j.grant.done()]
        if not candidates:
            return
        now = time.monotonic()
        job = max(candidates, key=lambda j: self._rank(j, now))
        self.waiting.remove(job)
        self.active = job
        job.state, job.started_at = "running", now
        log.info("job %s started priority=%s waited=%.1fs", job.id, priority_label(job.flags), now - job.enqueued_at)
        job.grant.set_result(None)

    def _rank(self, job: Job, now: float) -> tuple[int, int]:
        level = job.level
        if self.priority_aging > 0:
            level = min(MAX_LEVEL, level + int((now - job.enqueued_at) // self.priority_aging))
        return level, -job.seq  # highest level first, then first come first served

    # ---- stopping ----------------------------------------------------------------------

    def stop(self, job_id: str, state: str, *, force: bool = False) -> Job | None:
        """Stop a queued or running job. Returns the job, or None if unknown.

        A running job is detached from its caller and its upstream response drained, so the
        slot is only freed once upstream has really finished. `force` closes the upstream
        connection instead, which cannot be confirmed and so puts the backend into recovery.
        """
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job in self.waiting:
            self.waiting.remove(job)
            self._finish(job, state, "stopped while queued; never sent upstream")
            if not job.grant.done():
                job.grant.set_exception(Stopped(state))
            self._dispatch()
        elif job is self.active:
            self._stop_active(state, force)
        return job

    def _stop_active(self, state: str, force: bool) -> None:
        job = self.active
        if job is None:
            return
        if job.stop_reason is None:
            job.stop_reason = state
            job.state = "stopping"  # until upstream is confirmed finished
            log.warning("job %s: stop requested (%s, %s)", job.id, state, "force close" if force else "drain")
            if job.detach is not None:
                job.detach(state)
        if force and job.worker is not None and not job.worker.done():
            job.worker.cancel()

    def skip_current(self, force: bool = False) -> Job | None:
        job = self.active
        if job is not None:
            self._stop_active("skipped_by_admin", force)
        return job

    def reset_all(self, force: bool = False) -> int:
        stopped = 0
        for job in list(self.waiting):
            self.stop(job.id, "reset_by_admin")
            stopped += 1
        if self.active is not None:
            self._stop_active("reset_by_admin", force)
            stopped += 1
        return stopped

    def set_priority(self, job_id: str, flags: frozenset[str], by: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job not in self.waiting:
            raise ValueError(f"job {job_id} is {job.state}; only queued jobs can be re-prioritised")
        old = priority_label(job.flags)
        job.flags = flags
        job.note(f"priority {old} -> {priority_label(flags)} by {by}")
        log.info("job %s priority %s -> %s by %s", job_id, old, priority_label(flags), by)
        return job

    # ---- backend health ----------------------------------------------------------------

    def _enter_recovery(self, reason: str) -> None:
        if self.backend == "shutting_down":
            return
        self.backend_reason = reason
        if not self.auto_recover:
            self.backend = "uncertain"
            log.error("backend uncertain (%s); waiting for POST /admin/recover", reason)
            return
        self.backend = "recovering"
        log.warning("backend recovering (%s); cooldown %.1fs before the next job", reason, self.recovery_cooldown)
        if self._recovery is None or self._recovery.done():
            self._recovery = asyncio.create_task(self._recover())

    async def _recover(self) -> None:
        for attempt in range(1, self.recovery_attempts + 1):
            await asyncio.sleep(self.recovery_cooldown)
            if self.backend != "recovering":
                return
            ok = True
            if self._probe is not None:
                try:
                    ok = await self._probe()
                except Exception:
                    ok = False
            if ok:
                self._set_ready("recovered after cooldown")
                return
            log.warning("backend probe failed (attempt %d/%d)", attempt, self.recovery_attempts)
        self.backend = "uncertain"
        log.error("backend uncertain after %d failed probes; waiting for POST /admin/recover", self.recovery_attempts)

    def recover(self) -> bool:
        """Operator override: declare the backend idle and resume dispatching."""
        if self.backend not in ("recovering", "uncertain"):
            return False
        if self._recovery is not None:
            self._recovery.cancel()
        self._set_ready("operator recover")
        return True

    def _set_ready(self, why: str) -> None:
        self.backend, self.backend_reason = "ready", None
        log.info("backend ready (%s)", why)
        self._dispatch()

    def _unavailable_message(self) -> str:
        if self.backend == "shutting_down":
            return "gateway is shutting down"
        return f"backend state is uncertain ({self.backend_reason}); an operator must POST /admin/recover"

    async def shutdown(self, grace: float) -> None:
        """Stop admitting, fail queued jobs, give the active job `grace` seconds, then stop it."""
        self.backend = "shutting_down"
        if self._recovery is not None:
            self._recovery.cancel()
        for job in list(self.waiting):
            self.stop(job.id, "shutdown")
        job = self.active
        if job is None or job.worker is None:
            return
        done, _ = await asyncio.wait({job.worker}, timeout=grace)
        if not done:
            self._stop_active("shutdown", force=True)
            await asyncio.wait({job.worker}, timeout=5)

    # ---- bookkeeping -------------------------------------------------------------------

    def _finish(self, job: Job, state: str, reason: str | None) -> None:
        job.state, job.reason, job.finished_at = state, reason, time.monotonic()
        self.counters[_COUNTER.get(state, "cancelled")] += 1
        log.info("job %s finished state=%s reason=%s", job.id, state, reason)

    def _remember(self, job: Job) -> None:
        self.jobs[job.id] = job
        if len(self.jobs) > self.history_size:
            for old_id, old in list(self.jobs.items()):
                if len(self.jobs) <= self.history_size:
                    break
                if old.finished_at is not None:
                    del self.jobs[old_id]

    def position(self, job: Job) -> int | None:
        if job not in self.waiting:
            return None
        now = time.monotonic()
        order = sorted(self.waiting, key=lambda j: self._rank(j, now), reverse=True)
        return order.index(job) + 1

    def describe(self, job: Job) -> dict:
        now = time.monotonic()
        started = job.started_at
        waited_until = started or job.finished_at or now
        return {
            "id": job.id,
            "state": job.state,
            "reason": job.reason,
            "priority": priority_label(job.flags),
            "stream": job.stream,
            "queue_position": self.position(job),
            "wait_seconds": round(waited_until - job.enqueued_at, 1),
            "run_seconds": round((job.finished_at or now) - started, 1) if started else None,
            "upstream_status": job.status_code,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(job.created)),
            "notes": job.notes,
        }

    def status(self) -> dict:
        now = time.monotonic()
        by_priority = {"urgent+important": 0, "urgent": 0, "important": 0, "routine": 0}
        for job in self.waiting:
            by_priority[priority_label(job.flags)] += 1
        active = self.active
        return {
            "backend": self.backend,
            "backend_reason": self.backend_reason,
            "running": int(active is not None),
            "waiting": len(self.waiting),
            "waiting_by_priority": by_priority,
            "max_queue_size": self.max_queue_size,
            "oldest_wait_seconds": int(max((now - j.enqueued_at for j in self.waiting), default=0)),
            "current": None
            if active is None
            else {
                "priority": priority_label(active.flags),
                "stream": active.stream,
                "running_seconds": int(now - (active.started_at or now)),
            },
            "total_completed": self.counters["completed"],
            "total_failed": self.counters["failed"],
            "total_timed_out": self.counters["timed_out"],
            "total_cancelled": self.counters["cancelled"],
            "total_rejected": self.counters["rejected"],
        }
