"""Runner job hooks (ACTIONS_RUNNER_HOOK_JOB_STARTED / _COMPLETED). They live in the runner's own configuration,
not in the workflow, so a tampered workflow cannot remove them."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import allowlist, safety
from .config import SUPPORTED_EVENTS, Paths
from .redact import Redactor

SECRET_NAMES = ("deploy_key", "ssh_config", "gitconfig", "known_hosts", "github_host.pub")


def _log(paths: Paths, msg: str) -> None:
    try:
        paths.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(paths.logs / "hooks.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {Redactor().line(msg)}\n")
    except OSError:
        pass


def _inside_work(paths: Paths, p: str) -> Path:
    if not safety.is_owned(paths.work_dir, paths.home):
        raise safety.SafetyError("runner work directory is missing or lacks the ownership marker (was --work set by this tool?)")
    q = Path(p)
    if q.is_symlink() or not q.is_dir() or paths.work_dir.resolve() not in q.resolve().parents:
        raise safety.SafetyError(f"{p} is not strictly inside the managed work directory")
    return q


def job_started(env, paths: Paths, euid=None) -> None:
    """Fail closed unless this job is a same-repo PR integration build (or push to an allowed ref) of the repository
    registered in the protected local config. The environment is only *inspected* for the job's own facts; it never
    contributes to the allow-list."""
    safety.refuse_root(euid)
    cfg = allowlist.load(paths)
    repo = safety.validate_repo(env.get("GITHUB_REPOSITORY", ""), cfg.allowed_repos)
    safety.validate_sha(env.get("GITHUB_SHA", ""))
    event = env.get("GITHUB_EVENT_NAME", "")
    ref = env.get("GITHUB_REF", "")
    if event not in SUPPORTED_EVENTS:
        raise safety.SafetyError(f"event {event!r} is not allowed on the local runner")
    if event == "push":
        if ref not in cfg.push_refs:
            raise safety.SafetyError(f"push to {ref!r} is not allowed")
    else:
        if not (ref.startswith("refs/pull/") and ref.endswith("/merge")):
            raise safety.SafetyError(f"pull_request job is not on a GitHub integration ref: {ref!r}")
        try:
            ep = Path(env["GITHUB_EVENT_PATH"])
            if ep.stat().st_size > 5_000_000:
                raise safety.SafetyError("event payload too large")
            pr = json.loads(ep.read_text())["pull_request"]
            head, base = pr["head"]["repo"], pr["base"]["repo"]
            ok = head["full_name"] == base["full_name"] == repo and head.get("fork") is False
        except (KeyError, TypeError, ValueError, OSError):
            ok = False
        if not ok:
            raise safety.SafetyError("pull request is not same-repo (fork, or event payload unreadable); refusing")
    if env.get("GITHUB_WORKSPACE"):
        _inside_work(paths, env["GITHUB_WORKSPACE"])
    _log(paths, f"job-started ok repo={repo} event={event} sha={env['GITHUB_SHA']}")


def job_completed(env, paths: Paths) -> dict:
    """Remove the checkout and any credential-shaped leftovers inside the managed work dir."""
    res = {"workspace_entries_removed": 0, "secret_files_removed": 0}
    ws = env.get("GITHUB_WORKSPACE")
    if ws and Path(ws).exists():
        _inside_work(paths, ws)
        res["workspace_entries_removed"] = safety.safe_clean_contents(Path(ws), paths.work_dir, paths.home)
    tmp = env.get("RUNNER_TEMP")
    if tmp and Path(tmp).is_dir():
        t = _inside_work(paths, tmp)
        for p in list(t.rglob("*")):
            if p.is_file() and not p.is_symlink() and any(n in p.name for n in SECRET_NAMES):
                p.unlink()
                res["secret_files_removed"] += 1
    _log(paths, f"job-completed {res}")
    return res
