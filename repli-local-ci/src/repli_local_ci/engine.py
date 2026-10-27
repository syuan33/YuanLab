from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from . import gitops, safety, verify, workspace
from .config import DEFAULT_TIMEOUT, DISCLAIMER, Paths
from .queue import JobQueue
from .redact import Redactor


def prod_remote(repo: str) -> str:
    return f"git@github.com:{repo}.git"


class Engine:
    def __init__(self, paths: Paths, remote_factory=prod_remote, strict_identity=True, submodule_allow=frozenset(),
                 sleep=time.sleep, euid=None, ssh_key=None):
        self.paths, self.remote_factory, self.strict = paths, remote_factory, strict_identity
        self.submodule_allow, self.sleep, self.euid, self.ssh_key = submodule_allow, sleep, euid, ssh_key
        self.queue = JobQueue(paths.queue)

    def run(self, repo, sha, ref=None, head_repo=None, base_sha=None, timeout=DEFAULT_TIMEOUT,
            cancel_event=None, keep_workspace=False, wait_seconds=0) -> dict:
        safety.refuse_root(self.euid)
        safety.validate_repo(repo)
        safety.validate_head_repo(repo, head_repo or repo)
        safety.validate_sha(sha)
        if base_sha:
            safety.validate_sha(base_sha)
        base = {"repository": repo, "head_sha": sha, "base_sha": base_sha, "ref": ref, "disclaimer": DISCLAIMER}

        job, st = self.queue.enqueue(repo, sha, ref)
        if st == "duplicate":
            return dict(base, status="duplicate", job_id=job["id"])
        jid = job["id"]
        deadline = time.time() + wait_seconds
        while True:
            cur = self.queue.get(jid)
            if cur["state"] == "superseded":
                return dict(base, status="superseded", job_id=jid)
            if cancel_event is not None and cancel_event.is_set():
                self.queue.finish(jid, "cancelled")
                return dict(base, status="cancelled", job_id=jid)
            if self.queue.claim(jid):
                break
            if time.time() >= deadline:
                self.queue.finish(jid, "abandoned")
                return dict(base, status="busy", job_id=jid)
            self.sleep(1)
        final = "failed"
        try:
            rep = self._execute(base, jid, repo, sha, timeout, cancel_event, keep_workspace)
            final = "done" if rep["status"] == "passed" else rep["status"]
            return rep
        finally:
            self.queue.finish(jid, final)

    def _execute(self, base, jid, repo, sha, timeout, cancel_event, keep_workspace) -> dict:
        p = self.paths
        run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{sha[:12]}-{uuid.uuid4().hex[:6]}"
        rdir = p.reports / run_id
        rdir.mkdir(parents=True, mode=0o700)
        rep = dict(base, run_id=run_id, job_id=jid, report_dir=str(rdir), log_path=str(rdir / "verify.log"),
                   status="error", exit_code=None, error=None, workspace=None)
        redactor = Redactor()
        ws = None
        try:
            if self.strict:
                gitops.ensure_known_hosts(p.known_hosts)
            env = gitops.git_env(p.known_hosts if self.strict else None, self.ssh_key)
            ws = workspace.create_workspace(p.runs)
            rep["workspace"] = str(ws)
            gitops.fetch_exact(ws / "src", self.remote_factory(repo), sha, env=env)
            gitops.verify_identity(ws / "src", repo, strict=self.strict)
            gitops.check_submodules(ws / "src", self.submodule_allow)
            if (ws / "src" / ".gitmodules").is_file():
                gitops.init_submodules(ws / "src", env)
            should_cancel = lambda: (cancel_event is not None and cancel_event.is_set()) or self.queue.cancel_requested(jid)
            res = verify.run_verify(ws / "src", ws / "results", Path(rep["log_path"]), timeout, should_cancel, ws / "tmp", redactor)
            rep.update(res)
            if res["status"] == "error":
                rep["error"] = res.get("error")
            else:
                rep["test_summary"] = verify.summarize_log(Path(rep["log_path"]))
                rep["results_files"] = verify.hash_results(ws / "results")
        except (safety.SafetyError, gitops.GitError, OSError) as e:
            rep["error"] = redactor.line(str(e))
        rep["tool_versions"] = verify.tool_versions()
        rep.setdefault("started_at", time.time())
        rep.setdefault("ended_at", time.time())
        for k in ("started_at", "ended_at"):
            rep[k] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rep[k]))
        rep["command_text"] = " ".join(rep["command"]) if rep.get("command") else None
        if ws is not None and not keep_workspace:
            try:
                safety.safe_remove(ws, p.runs)
            except safety.SafetyError as e:
                rep["cleanup_error"] = str(e)
        rep["summary"] = render_summary(rep)
        (rdir / "report.json").write_text(json.dumps(rep, indent=2, default=str))
        (rdir / "summary.txt").write_text(rep["summary"])
        return rep


def render_summary(r: dict) -> str:
    ts = r.get("test_summary") or {}
    lines = [
        f"repli-local-ci result: {str(r['status']).upper()}",
        f"repository : {r['repository']}",
        f"head sha   : {r['head_sha']}",
        f"base sha   : {r.get('base_sha') or '-'}",
        f"started    : {r.get('started_at')}",
        f"ended      : {r.get('ended_at')}",
        f"command    : {r.get('command_text')}",
        f"exit code  : {r.get('exit_code')}",
        f"log sha256 : {r.get('log_sha256')}",
        f"error      : {r.get('error') or '-'}",
        "test summary (heuristic):",
        *[f"  {l}" for l in ts.get("lines", [])[-5:]],
        "",
        r["disclaimer"],
    ]
    return "\n".join(lines) + "\n"
