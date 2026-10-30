"""Read-only reader for the CI progress timeline written by the Repli verify script.

Layout: <logs>/<run-id>/<job-id>/timeline.log (+ timeline.log.1..3 rotated, higher suffix = older), JSONL, one fixed-shape record
per line. This module never creates, chmods, repairs, renames or deletes anything, never shells out, and only ever returns/prints
fields that passed strict validation - raw file content is never echoed.
"""
from __future__ import annotations

import errno
import json
import math
import os
import re
import stat
import time
from datetime import datetime
from typing import Callable, List, NamedTuple, Optional, Tuple

STAGES = frozenset({"clj-kondo", "shadow-compile", "node-tests", "dev-tooling-tests", "integration-preflight",
                    "integration-node-tests", "arch-contract-map", "verify-job"})
JOB_STAGE = "verify-job"  # only its terminal events end a job; per-check failed/timeout/cancelled are not job-terminal
EVENTS = frozenset({"started", "heartbeat", "completed", "failed", "timeout", "cancelled"})
TERMINAL_EVENTS = frozenset({"completed", "failed", "timeout", "cancelled"})
PROCESS_STATES = frozenset({"running", "exited", "timed_out", "cancelled", "not_run"})
KEYS = frozenset({"stage", "event", "timestamp", "elapsed_ms", "pid", "process_state", "exit_status", "estimated_progress"})
TIMELINE = "timeline.log"
ROTATED = 3
MAX_FILE_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 2048
POLL_SECONDS = 1.0

_RUN_RE = re.compile(r"[0-9]{1,20}\Z")
_JOB_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z\Z")
_INT_LIMIT = 2 ** 63


class TimelineError(Exception):
    """Fixed, sanitized messages only: never file content, never a path."""


class Current(NamedTuple):
    run_id: str
    job_id: str
    record: dict


def _sleep(seconds):
    time.sleep(seconds)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and -_INT_LIMIT < v < _INT_LIMIT


def _no_dupes(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError
        d[k] = v
    return d


def _bad_const(_):
    raise ValueError


def parse_record(raw: bytes) -> dict:
    try:
        r = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_bad_const)
    except (ValueError, RecursionError):
        raise TimelineError("malformed timeline record") from None
    if not isinstance(r, dict) or set(r) != KEYS:
        raise TimelineError("unexpected timeline record fields")
    ok = (r["stage"] in STAGES and r["event"] in EVENTS and r["process_state"] in PROCESS_STATES
          and isinstance(r["stage"], str) and isinstance(r["event"], str) and isinstance(r["process_state"], str)
          and isinstance(r["timestamp"], str) and _TS_RE.match(r["timestamp"]) is not None
          and _is_int(r["elapsed_ms"]) and r["elapsed_ms"] >= 0
          and (r["pid"] is None or (_is_int(r["pid"]) and r["pid"] > 0))
          and (r["exit_status"] is None or _is_int(r["exit_status"])))
    if ok:
        try:
            datetime.strptime(r["timestamp"].rstrip("Z").split(".")[0], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            ok = False
    est = r["estimated_progress"]
    if ok and est is not None:
        ok = (isinstance(est, dict) and set(est) == {"estimated", "percent"} and est["estimated"] is True
              and isinstance(est["percent"], (int, float)) and not isinstance(est["percent"], bool)
              and math.isfinite(est["percent"]) and 0 <= est["percent"] <= 100)
    if not ok:
        raise TimelineError("invalid timeline record")
    return r


def _check_owner_mode(st, max_bits, what):
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & ~max_bits:
        raise TimelineError(f"{what} has unsafe owner or mode")


def _open_dir(name: str, dir_fd: Optional[int] = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError:
        raise TimelineError("timeline directory is missing, a symlink, or not a directory") from None
    try:
        _check_owner_mode(os.fstat(fd), 0o700, "timeline directory")
    except TimelineError:
        os.close(fd)
        raise
    return fd


def _read_file(dir_fd: int, name: str) -> Optional[List[dict]]:
    """Records of one timeline file, or None when it does not exist. Raises on anything unsafe or malformed."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return None
        raise TimelineError("timeline file is a symlink or unreadable") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise TimelineError("timeline file is not a regular file")
        _check_owner_mode(st, 0o600, "timeline file")
        if st.st_size > MAX_FILE_BYTES:
            raise TimelineError("timeline file is too large")
        data = b""
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_FILE_BYTES:
            raise TimelineError("timeline file is too large")
    finally:
        os.close(fd)
    lines = data.split(b"\n")
    lines.pop()  # text after the last newline is an in-flight partial write (or empty); never parsed
    out = []
    for ln in lines:
        if len(ln) > MAX_RECORD_BYTES:
            raise TimelineError("timeline record is too large")
        out.append(parse_record(ln))
    return out


def _read_job(job_fd: int) -> List[dict]:
    records: List[dict] = []
    found = False
    for name in [f"{TIMELINE}.{i}" for i in range(ROTATED, 0, -1)] + [TIMELINE]:  # oldest first
        recs = _read_file(job_fd, name)
        if recs is not None:
            found = True
            records.extend(recs)
    if not found:
        raise TimelineError("no timeline file")
    return records


def _with_job(logs, run_id: str, job_id: str, fn):
    if not _RUN_RE.match(run_id or "") or not _JOB_RE.match(job_id or ""):
        raise TimelineError("invalid run or job id")
    fds = []
    try:
        fds.append(_open_dir(os.fspath(logs)))
        fds.append(_open_dir(run_id, fds[-1]))
        fds.append(_open_dir(job_id, fds[-1]))
        return fn(fds[-1])
    finally:
        for fd in reversed(fds):
            os.close(fd)


def read_timeline(logs, run_id: str, job_id: str) -> List[dict]:
    """Explicit lookup of one job's validated records (rotated files first), including finished jobs."""
    return _with_job(logs, run_id, job_id, _read_job)


def is_terminal(record: dict) -> bool:
    return record["stage"] == JOB_STAGE and record["event"] in TERMINAL_EVENTS


def find_current(logs) -> Tuple[Optional[Current], int]:
    """(newest active job or None, number of timelines rejected as unsafe/invalid). Scans only the managed logs directory."""
    try:
        root = _open_dir(os.fspath(logs))
    except TimelineError:
        return None, (1 if os.path.lexists(logs) else 0)
    best: Optional[Current] = None
    rejected = 0
    try:
        for run_id in sorted(n for n in os.listdir(root) if _RUN_RE.match(n)):
            try:
                run_fd = _open_dir(run_id, root)
            except TimelineError:
                rejected += 1
                continue
            try:
                for job_id in sorted(n for n in os.listdir(run_fd) if _JOB_RE.match(n)):
                    try:
                        job_fd = _open_dir(job_id, run_fd)
                    except TimelineError:
                        rejected += 1
                        continue
                    try:
                        recs = _read_job(job_fd)
                    except TimelineError:
                        rejected += 1
                        continue
                    finally:
                        os.close(job_fd)
                    if recs and not is_terminal(recs[-1]) and (best is None or recs[-1]["timestamp"] >= best.record["timestamp"]):
                        best = Current(run_id, job_id, recs[-1])
            finally:
                os.close(run_fd)
    finally:
        os.close(root)
    return best, rejected


def format_record(r: dict) -> str:
    est = r["estimated_progress"]
    return (f"{r['timestamp']} {r['stage']} {r['event']} elapsed={r['elapsed_ms'] / 1000:.1f}s "
            f"pid={r['pid'] if r['pid'] is not None else '-'} state={r['process_state']} "
            f"exit={r['exit_status'] if r['exit_status'] is not None else '-'}"
            + (f" progress=~{est['percent']:.0f}% (estimated)" if est else ""))


def status_lines(logs) -> List[str]:
    cur, rejected = find_current(logs)
    if cur is None:
        note = f" ({rejected} timeline(s) rejected as unsafe or invalid)" if rejected else ""
        return [f"progress    : none{note}"]
    return [f"progress    : run {cur.run_id} job {cur.job_id}", f"  {format_record(cur.record)}"]


def follow(logs, emit: Callable[[str], None]) -> int:
    """Print validated events of the current active job until a job-terminal event. 0 done, 1 no active job, 2 refused."""
    cur, _ = find_current(logs)
    if cur is None:
        emit("no active job")
        return 1
    last = None
    while True:
        try:
            recs = read_timeline(logs, cur.run_id, cur.job_id)
        except TimelineError as e:
            emit(f"refused: {e}")
            return 2
        start = 0
        if last is not None:
            for i in range(len(recs) - 1, -1, -1):
                if recs[i] == last:
                    start = i + 1
                    break
        for r in recs[start:]:
            emit(format_record(r))
            last = r
        if recs and is_terminal(recs[-1]):
            return 0
        _sleep(POLL_SECONDS)
