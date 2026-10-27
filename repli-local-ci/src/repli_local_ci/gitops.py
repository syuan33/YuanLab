from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .safety import SafetyError

GITHUB_ED25519_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
GITHUB_ED25519_FINGERPRINT = "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
KNOWN_HOSTS_LINE = f"github.com ssh-ed25519 {GITHUB_ED25519_KEY}\n"
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_ID = re.compile(r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class GitError(Exception):
    pass


def parse_remote_identity(url: str):
    m = _ID.match(url or "")
    return m.group(1) if m else None


def git_env(known_hosts: Path = None, ssh_key: str = None) -> dict:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "SSH_AUTH_SOCK") if k in os.environ}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    if known_hosts:
        ssh = ["ssh", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
               "-o", "GlobalKnownHostsFile=/dev/null", "-o", "BatchMode=yes", "-o", "UpdateHostKeys=no"]
        if ssh_key:
            ssh += ["-i", ssh_key, "-o", "IdentitiesOnly=yes"]
        env["GIT_SSH_COMMAND"] = " ".join(ssh)
    return env


def _git(args, cwd=None, env=None, timeout=900):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, env=env or git_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out")
    if r.returncode:
        raise GitError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def resolve_ref(remote: str, ref: str, env=None) -> str:
    """Resolve an owner-repo *branch* to a SHA exactly once. Only refs/heads/* are accepted."""
    name = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    if ref.startswith("refs/") and not ref.startswith("refs/heads/") or ".." in name or not _BRANCH.fullmatch(name):
        raise SafetyError(f"only plain branch names are accepted, got {ref!r}")
    full = ref if ref.startswith("refs/heads/") else f"refs/heads/{ref}"
    out = _git(["ls-remote", "--", remote, full], env=env).split("\n")
    lines = [l for l in out if l.strip()]
    if len(lines) != 1 or lines[0].split("\t")[1] != full:
        raise GitError(f"could not resolve {full} to exactly one commit")
    return lines[0].split("\t")[0]


def fetch_exact(src: Path, remote: str, sha: str, env=None) -> None:
    src = Path(src)
    if any(src.iterdir()):
        raise SafetyError(f"checkout directory is not empty, refusing: {src}")
    _git(["init", "-q"], cwd=src, env=env)
    _git(["remote", "add", "origin", remote], cwd=src, env=env)
    _git(["fetch", "-q", "--no-tags", "origin", sha], cwd=src, env=env)
    _git(["checkout", "-q", "--detach", sha], cwd=src, env=env)
    head = _git(["rev-parse", "HEAD"], cwd=src, env=env).strip()
    if head != sha:
        raise GitError(f"checked-out HEAD {head} != requested {sha}")
    if _git(["status", "--porcelain", "--ignored"], cwd=src, env=env).strip():
        raise GitError("fresh checkout is not clean")


def verify_identity(src: Path, repo: str, strict: bool = True) -> None:
    url = _git(["config", "--get", "remote.origin.url"], cwd=src).strip()
    if strict and parse_remote_identity(url) != repo:
        raise GitError(f"repository identity mismatch: origin {url!r} is not github.com/{repo}")


def check_submodules(src: Path, allowed) -> None:
    gm = Path(src) / ".gitmodules"
    if not gm.is_file():
        return
    out = _git(["config", "-f", str(gm), "--get-regexp", r"^submodule\..*\.url$"])
    for line in out.splitlines():
        name, _, url = line.partition(" ")
        ident = parse_remote_identity(url.strip())
        if ident is None or ident not in allowed:
            raise GitError(f"submodule identity not allow-listed: {name} -> {url!r}")


def init_submodules(src: Path, env) -> None:
    _git(["-c", "protocol.allow=never", "-c", "protocol.ssh.allow=always", "-c", "protocol.https.allow=always",
          "submodule", "update", "--init", "--recursive"], cwd=src, env=env)


def ensure_known_hosts(path: Path) -> None:
    path = Path(path)
    if path.exists():
        if path.read_text() != KNOWN_HOSTS_LINE:
            raise GitError(f"{path} does not match the pinned GitHub host key; refusing (fail closed)")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(KNOWN_HOSTS_LINE)


def verify_scanned_host_key(scan_output: str) -> None:
    for line in scan_output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "github.com" and parts[1] == "ssh-ed25519" and parts[2] == GITHUB_ED25519_KEY:
            return
    raise GitError("GitHub ssh host key does not match the pinned ed25519 key")
