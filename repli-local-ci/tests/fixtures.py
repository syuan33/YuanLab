"""Fully synthetic fixtures. Nothing here touches the network, REPLI, or any real runner."""
from __future__ import annotations

import gc
import os
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

SYNTH_REPO = "syuan33/repli"  # allow-listed *name* only; the remote is always a local bare repo

GIT_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "HOME": tempfile.gettempdir(),
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def git(*args, cwd=None):
    r = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


class Remote:
    """A bare repo plus a work clone used to author commits."""

    def __init__(self, root: Path, verify_body: str = "echo 'Ran 3 tests'; echo OK; exit 0"):
        self.bare = root / "remote.git"
        self.work = root / "work"
        git("init", "--bare", "-b", "main", str(self.bare))
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=self.bare)
        git("clone", str(self.bare), str(self.work))
        git("checkout", "-b", "main", cwd=self.work)
        self.commit_verify(verify_body)

    def commit_verify(self, body: str, branch: str = "main") -> str:
        d = self.work / "dev"
        d.mkdir(exist_ok=True)
        v = d / "verify.sh"
        v.write_text("#!/usr/bin/env bash\n" + body + "\n")
        v.chmod(0o755)
        git("add", "-A", cwd=self.work)
        git("commit", "-m", "c", cwd=self.work)
        git("push", "origin", f"HEAD:refs/heads/{branch}", cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    @property
    def url(self) -> str:
        return str(self.bare)


class StrictCase(unittest.TestCase):
    """Fails any test that leaks a process/file/socket.

    `-W error` alone is NOT enough: ResourceWarning is raised inside object finalizers, where Python only prints
    "Exception ignored" and the test still passes. This base class records both the warning and the unraisable
    exception, forces a gc pass after the test (and after all cleanups registered later), and fails.
    """

    def setUp(self):
        self._leaks = []
        self._old_hook = sys.unraisablehook
        sys.unraisablehook = lambda u: self._leaks.append(f"unraisable: {u.exc_type.__name__}: {u.exc_value}")
        self._wctx = warnings.catch_warnings(record=True)
        self._wlog = self._wctx.__enter__()
        warnings.simplefilter("always", ResourceWarning)
        self.addCleanup(self._strict_check)  # registered first => runs last (after other cleanups)

    def _strict_check(self):
        gc.collect()
        gc.collect()
        self._wctx.__exit__(None, None, None)
        sys.unraisablehook = self._old_hook
        leaks = self._leaks + [f"{w.category.__name__}: {w.message}" for w in self._wlog if issubclass(w.category, ResourceWarning)]
        if leaks:
            self.fail("resource leak(s) detected: " + "; ".join(leaks))


class TmpCase(StrictCase):
    def setUp(self):
        super().setUp()
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
