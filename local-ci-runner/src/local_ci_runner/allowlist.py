"""Protected local configuration: the ONLY source of the repository allow-list (and verify command).

It is written by the explicitly approved `register --apply` step, mode 0600, in a 0700 directory owned by the current user.
Workflows, PR content and environment variables can never widen it: nothing here (or in the hooks) consults the environment.
Every failure is fail-closed (`ConfigError`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .config import Paths
from .safety import SafetyError

SCHEMA = 1
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_KEYS = {"schema", "allowed_repos", "verify_command", "push_refs", "submodule_repos", "checksum"}


class ConfigError(SafetyError):
    pass


class Config:
    def __init__(self, allowed_repos, verify_command, push_refs=("refs/heads/main",), submodule_repos=()):
        self.allowed_repos = frozenset(allowed_repos)
        self.verify_command = list(verify_command)
        self.push_refs = frozenset(push_refs)
        self.submodule_repos = frozenset(submodule_repos)


def _normalized(d: dict) -> str:
    body = {k: v for k, v in d.items() if k != "checksum"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _checksum(d: dict) -> str:
    return hashlib.sha256(_normalized(d).encode()).hexdigest()


def validate_repo_name(repo) -> str:
    if not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo.count("/") != 1 or ".." in repo:
        raise ConfigError(f"invalid repository name: {repo!r}")
    return repo


def validate_command(cmd) -> list:
    if (not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c and "\0" not in c for c in cmd)):
        raise ConfigError("verify_command must be a non-empty list of strings")
    first = Path(cmd[0])
    if first.is_absolute() or ".." in first.parts or cmd[0].startswith("-"):
        raise ConfigError("verify_command[0] must be a relative path inside the checkout")
    return cmd


def _validate(d) -> Config:
    if not isinstance(d, dict) or set(d) - _KEYS or d.get("schema") != SCHEMA:
        raise ConfigError("unknown or missing keys / schema in config")
    if d.get("checksum") != _checksum(d):
        raise ConfigError("config checksum mismatch (edited outside `register`?)")
    repos = d.get("allowed_repos")
    if not isinstance(repos, list) or len(repos) != 1:
        raise ConfigError("allowed_repos must list exactly one repository")
    for r in repos:
        validate_repo_name(r)
    refs = d.get("push_refs", [])
    if not isinstance(refs, list) or not all(isinstance(x, str) and re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", x) and ".." not in x for x in refs):
        raise ConfigError("push_refs must be a list of refs/heads/* names")
    subs = d.get("submodule_repos", [])
    if not isinstance(subs, list):
        raise ConfigError("submodule_repos must be a list")
    for r in subs:
        validate_repo_name(r)
    return Config(repos, validate_command(d.get("verify_command")), refs, subs)


def load(paths: Paths) -> Config:
    """Strictly load the protected config or raise ConfigError."""
    home, f = Path(paths.home), Path(paths.config_file)
    try:
        hs = os.lstat(home)
        if not os.path.isdir(home) or os.path.islink(home) or hs.st_uid != os.getuid() or hs.st_mode & 0o077:
            raise ConfigError(f"{home} must be a real directory owned by the current user with mode 0700")
        fs = os.lstat(f)
    except FileNotFoundError:
        raise ConfigError("not registered: no protected config (run the approved `register --apply` step); denying everything")
    except OSError as e:
        raise ConfigError(f"cannot stat protected config: {e}")
    if os.path.islink(f) or not os.path.isfile(f):
        raise ConfigError(f"{f} must be a regular file, not a symlink")
    if fs.st_uid != os.getuid():
        raise ConfigError(f"{f} is not owned by the current user")
    if fs.st_mode & 0o777 != 0o600:
        raise ConfigError(f"{f} must have mode 0600 (found {oct(fs.st_mode & 0o777)})")
    try:
        with open(f, "rb") as fh:
            raw = fh.read(65536)
        return _validate(json.loads(raw.decode()))
    except (ValueError, UnicodeDecodeError) as e:
        raise ConfigError(f"unreadable config: {e}")


def render(repo: str, verify_command, push_refs=("refs/heads/main",), submodule_repos=()) -> dict:
    d = {"schema": SCHEMA, "allowed_repos": [validate_repo_name(repo)], "verify_command": validate_command(list(verify_command)),
         "push_refs": list(push_refs), "submodule_repos": [validate_repo_name(r) for r in submodule_repos]}
    d["checksum"] = _checksum(d)
    _validate(d)
    return d


def write(paths: Paths, repo: str, verify_command, **kw) -> Path:
    """Create the config exclusively (never overwrites), mode 0600."""
    d = render(repo, verify_command, **kw)
    Path(paths.home).mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.home, 0o700)
    fd = os.open(paths.config_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(d, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    return Path(paths.config_file)


def remove(paths: Paths) -> bool:
    f = Path(paths.config_file)
    if f.is_symlink() or not f.is_file() or os.lstat(f).st_uid != os.getuid():
        return False
    f.unlink()
    return True
