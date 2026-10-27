from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .redact import Redactor

_SUMMARY = re.compile(r"(?i)(\bRan \d+ tests?\b|\b\d+\s+(?:tests?|assertions?|passed|failed|failures?|errors?|skipped)\b|\bFAIL(?:ED)?\b|\bOK\b|\bPASS(?:ED)?\b)")
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "JAVA_HOME")


def sha256_file(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def child_env(tmpdir: Path) -> dict:
    """Allow-list environment: no tokens, no ssh-agent, no GITHUB_*/ACTIONS_* values reach repository code."""
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.update(CI="true", REPLI_LOCAL_CI="1", TMPDIR=str(tmpdir))
    return env


def _killpg(pgid, proc):
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(pgid, sig)  # our own session/process group only
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_verify(src: Path, results: Path, log_path: Path, timeout: float, should_cancel, tmpdir: Path, redactor: Redactor = None):
    """Run the repo's own dev/verify.sh. Returns dict(status, exit_code, command, started_at, ended_at, log_sha256)."""
    redactor = redactor or Redactor()
    script = Path(src) / "dev" / "verify.sh"
    command = [str(script), "-p", "standard", "-o", str(results)]
    started = time.time()
    if not script.is_file() or script.is_symlink() or not os.access(script, os.X_OK):
        return dict(status="error", exit_code=None, command=command, started_at=started, ended_at=time.time(),
                    error="dev/verify.sh missing, a symlink, or not executable", log_sha256=None)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    logf = os.fdopen(fd, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(command, cwd=src, env=child_env(tmpdir), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    pgid = proc.pid

    def pump():
        for raw in iter(proc.stdout.readline, b""):
            logf.write(redactor.line(raw.decode("utf-8", "replace").rstrip("\n")) + "\n")
            logf.flush()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    status = None
    try:
        while proc.poll() is None:
            if time.time() - started > timeout:
                status = "timeout"
            elif should_cancel():
                status = "cancelled"
            if status:
                _killpg(pgid, proc)
                break
            time.sleep(0.1)
    finally:
        # Every exit path (including exceptions in should_cancel) kills our own group and reaps the child.
        if proc.poll() is None:
            _killpg(pgid, proc)
        try:
            os.killpg(pgid, signal.SIGKILL)  # stragglers from our own group
        except ProcessLookupError:
            pass
        proc.wait()
        t.join(5)
        proc.stdout.close()
        logf.close()
    rc = proc.returncode
    if status is None:
        status = "passed" if rc == 0 else "failed"
    return dict(status=status, exit_code=rc, command=command, started_at=started, ended_at=time.time(),
                log_sha256=sha256_file(log_path))


def summarize_log(log_path: Path, limit: int = 20) -> dict:
    lines = [l.strip() for l in Path(log_path).read_text(errors="replace").splitlines() if _SUMMARY.search(l)]
    return {"parsed": bool(lines), "lines": lines[-limit:],
            "note": "heuristic extraction from the log tail; the exit code is authoritative"}


def hash_results(results: Path, limit: int = 500) -> list:
    out = []
    for p in sorted(Path(results).rglob("*")):
        if p.is_file() and not p.is_symlink():
            out.append({"path": str(p.relative_to(results)), "sha256": sha256_file(p)})
            if len(out) >= limit:
                break
    return out


def _ver(argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        return ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[0][:200] if (r.stdout or r.stderr) else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def tool_versions() -> dict:
    return {"python3": sys.version.split()[0], "bash": _ver(["bash", "--version"]), "git": _ver(["git", "--version"]),
            "node": _ver(["node", "--version"]), "java": _ver(["java", "-version"]),
            "clj-kondo": _ver(["clj-kondo", "--version"]), "uname": _ver(["uname", "-srm"])}
