from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .config import ALLOWED_REPOS

MARKER = ".repli-local-ci-owner"
TOOL = "repli-local-ci"
_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class SafetyError(Exception):
    pass


def validate_repo(repo: str) -> str:
    if not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo not in ALLOWED_REPOS:
        raise SafetyError(f"repository not allow-listed: {repo!r}")
    return repo


def validate_head_repo(repo: str, head_repo: str) -> None:
    if head_repo != repo:
        raise SafetyError(f"fork/external head repository refused: {head_repo!r} != {repo!r}")


def validate_sha(sha: str) -> str:
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise SafetyError("a full 40-char lowercase hex commit SHA is required (no branch names, short SHAs or revisions)")
    return sha


def refuse_root(euid: int = None) -> None:
    if (os.geteuid() if euid is None else euid) == 0:
        raise SafetyError("refusing to run as root")


def write_marker(d: Path) -> None:
    d = Path(d)
    data = {"tool": TOOL, "path": str(d.resolve()), "uid": os.getuid()}
    fd = os.open(d / MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def is_owned(d: Path, root: Path) -> bool:
    """True only for a real (non-symlink) directory strictly inside `root`, owned by us, with a matching marker."""
    d, root = Path(d), Path(root)
    try:
        if d.is_symlink() or not d.is_dir() or root.is_symlink():
            return False
        rd, rr = d.resolve(), root.resolve()
        if rd == rr or rr not in rd.parents:
            return False
        if os.path.abspath(d) != str(rd):  # a path component was a symlink
            return False
        m = d / MARKER
        if m.is_symlink() or not m.is_file():
            return False
        if os.stat(d).st_uid != os.getuid():
            return False
        data = json.loads(m.read_text())
        return data.get("tool") == TOOL and data.get("path") == str(rd)
    except (OSError, ValueError):
        return False


def safe_remove(d: Path, root: Path) -> None:
    if not is_owned(d, root):
        raise SafetyError(f"not removing {d}: not a directory created by {TOOL} under {root}")
    shutil.rmtree(d)  # rmtree unlinks symlinks rather than following them
