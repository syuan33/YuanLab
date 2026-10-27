from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ACTIVE = ("queued", "running")


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, ValueError):
        return True  # exists (or unknown): never steal the slot on doubt


class JobQueue:
    """File-backed FIFO, concurrency 1, de-duplicated by (repo, sha), superseded by newer head of the same ref."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.root / ".lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _path(self, jid):
        return self.root / f"{jid}.json"

    def _read_all(self):
        out = []
        for p in self.root.glob("*.json"):
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda j: j["seq"])

    def _write(self, job):
        tmp = self.root / f".{job['id']}.tmp"
        tmp.write_text(json.dumps(job))
        os.replace(tmp, self._path(job["id"]))

    def enqueue(self, repo, sha, ref=None):
        with self._lock():
            jobs = self._read_all()
            for j in jobs:
                if j["repo"] == repo and j["sha"] == sha and j["state"] in ACTIVE:
                    return j, "duplicate"
            if ref:
                for j in jobs:
                    if j["repo"] == repo and j.get("ref") == ref and j["state"] in ACTIVE:
                        if j["state"] == "queued":
                            j["state"] = "superseded"
                        else:
                            j["cancel_requested"] = True
                        self._write(j)
            job = {"id": uuid.uuid4().hex[:12], "seq": time.time_ns(), "repo": repo, "sha": sha, "ref": ref,
                   "state": "queued", "pid": None, "cancel_requested": False}
            self._write(job)
            return job, "queued"

    def get(self, jid):
        try:
            return json.loads(self._path(jid).read_text())
        except (OSError, ValueError):
            return None

    def list(self):
        with self._lock():
            return self._read_all()

    def head(self):
        with self._lock():
            return next((j for j in self._read_all() if j["state"] == "queued"), None)

    def claim(self, jid, pid=None) -> bool:
        with self._lock():
            jobs = self._read_all()
            for j in jobs:
                if j["state"] == "running":
                    if _alive(j["pid"]):
                        return False
                    j["state"] = "failed"  # owner died without finishing
                    self._write(j)
            queued = [j for j in jobs if j["state"] == "queued"]
            if not queued or queued[0]["id"] != jid:
                return False
            queued[0].update(state="running", pid=os.getpid() if pid is None else pid)
            self._write(queued[0])
            return True

    def finish(self, jid, state):
        with self._lock():
            j = self.get(jid)
            if j:
                j["state"] = state
                self._write(j)

    def request_cancel(self, repo, sha) -> bool:
        with self._lock():
            hit = False
            for j in self._read_all():
                if j["repo"] == repo and j["sha"] == sha and j["state"] in ACTIVE:
                    j["cancel_requested"] = True
                    if j["state"] == "queued":
                        j["state"] = "cancelled"
                    self._write(j)
                    hit = True
            return hit

    def cancel_requested(self, jid) -> bool:
        j = self.get(jid)
        return bool(j and j.get("cancel_requested"))
