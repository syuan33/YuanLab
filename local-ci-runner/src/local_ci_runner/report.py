"""Post-job report built from the GitHub-provided job environment (the runner owns checkout and identity)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import DISCLAIMER, Paths
from .redact import Redactor
from .verify import hash_results, tool_versions


def build(env, results_dir, job_status: str, paths: Paths) -> dict:
    ev = {}
    try:
        ev = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
    except (KeyError, OSError, ValueError):
        pass
    pr = ev.get("pull_request") or {}
    rd = Path(results_dir)
    rep = {
        "repository": env.get("GITHUB_REPOSITORY"),
        "event": env.get("GITHUB_EVENT_NAME"),
        "integration_sha": env.get("GITHUB_SHA"),  # what the official checkout verified
        "head_sha": (pr.get("head") or {}).get("sha"),
        "base_sha": (pr.get("base") or {}).get("sha"),
        "ref": env.get("GITHUB_REF"),
        "run_id": env.get("GITHUB_RUN_ID"),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "workflow": env.get("GITHUB_WORKFLOW"),
        "job": env.get("GITHUB_JOB"),
        "runner_name": env.get("RUNNER_NAME"),
        "job_status": job_status,
        "reported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results_dir_present": rd.is_dir(),
        "results_files": hash_results(rd) if rd.is_dir() else [],
        "tool_versions": tool_versions(),
        "disclaimer": DISCLAIMER,
    }
    return rep


def write(rep: dict, paths: Paths, step_summary=None) -> Path:
    red = Redactor()

    def scrub(v):
        if isinstance(v, str):
            return red.line(v)
        if isinstance(v, dict):
            return {k: scrub(x) for k, x in v.items()}
        if isinstance(v, list):
            return [scrub(x) for x in v]
        return v
    rep = scrub(rep)
    name = f"ci-{rep.get('run_id') or 'manual'}-{rep.get('run_attempt') or '1'}-{(rep.get('integration_sha') or 'unknown')[:12]}"
    d = paths.reports / name
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    (d / "report.json").write_text(json.dumps(rep, indent=2))
    text = "\n".join([
        f"local-ci-runner job report: {rep['job_status']}",
        f"repository      : {rep['repository']}",
        f"integration sha : {rep['integration_sha']}",
        f"head / base sha : {rep['head_sha']} / {rep['base_sha']}",
        f"run             : {rep['run_id']} attempt {rep['run_attempt']} on {rep['runner_name']}",
        f"result files    : {len(rep['results_files'])} (sha256 in report.json)",
        "", rep["disclaimer"], ""])
    (d / "summary.txt").write_text(text)
    if step_summary and os.path.isfile(step_summary):
        with open(step_summary, "a") as f:
            f.write("```\n" + text + "```\n")
    return d
