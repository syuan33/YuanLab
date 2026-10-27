"""Install/uninstall the manager itself into a stable, user-owned location (independent of the source checkout)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from . import safety
from .config import Paths

LAUNCHER_HEADER = "# local-ci-runner managed launcher: removed only by `local-ci-runner uninstall --apply`\n"
PROPOSED_WORKFLOW = "example-workflow.yml"


def _launcher_text(app_pkg: Path) -> str:
    return (
        "#!/bin/sh\n" + LAUNCHER_HEADER +
        "exec python3 -s -c 'import sys; sys.path.insert(0, sys.argv[1]); "
        "from local_ci_runner.cli import main; sys.exit(main(sys.argv[2:]))' "
        f"'{app_pkg}' \"$@\"\n"
    )


def _hook_text(launcher: Path, name: str, home: Path) -> str:
    # --home is baked in at install time: hooks never derive the state directory from the environment.
    return f"#!/bin/bash\nexec '{launcher}' hook {name} --home '{home}'\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def find_example(paths: Paths):
    here = Path(__file__).resolve().parents[2] / "examples" / PROPOSED_WORKFLOW
    for c in (here, paths.app_dir / "examples" / PROPOSED_WORKFLOW):
        if c.is_file():
            return c
    return None


def launcher_is_ours(paths: Paths) -> bool:
    l = paths.launcher
    try:
        m = json.loads(paths.manifest.read_text())
        return (not l.is_symlink() and l.is_file() and hashlib.sha256(l.read_bytes()).hexdigest() == m["launcher_sha256"])
    except (OSError, ValueError, KeyError):
        return False


def install_app(paths: Paths, pkg_dir: Path, version: str) -> None:
    if paths.app_dir.exists():
        raise safety.SafetyError(f"{paths.app_dir} already exists; uninstall first")
    if paths.launcher.exists() or paths.launcher.is_symlink():
        raise safety.SafetyError(f"{paths.launcher} already exists and is not managed by this install; refusing to overwrite")
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    if paths.bin_dir.stat().st_uid != os.getuid():
        raise safety.SafetyError(f"{paths.bin_dir} is not owned by the current user")
    tmp = paths.home / f"app.tmp-{uuid.uuid4().hex[:8]}"
    try:
        pkg = tmp / "pkg" / "local_ci_runner"
        pkg.mkdir(parents=True, mode=0o700)
        for f in sorted(Path(pkg_dir).glob("*.py")):
            shutil.copyfile(f, pkg / f.name)
        ex = find_example(paths)
        if ex:
            (tmp / "examples").mkdir()
            shutil.copyfile(ex, tmp / "examples" / PROPOSED_WORKFLOW)
        os.rename(tmp, paths.app_dir)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
    safety.write_marker(paths.app_dir)
    paths.hooks_dir.mkdir(mode=0o700)
    for name in ("job-started", "job-completed"):
        h = paths.hooks_dir / f"{name}.sh"
        h.write_text(_hook_text(paths.launcher, name, paths.home))
        h.chmod(0o700)
    text = _launcher_text(paths.app_dir / "pkg")
    tmpl = paths.bin_dir / f".local-ci-runner.{uuid.uuid4().hex[:8]}"
    tmpl.write_text(text)
    tmpl.chmod(0o755)
    os.replace(tmpl, paths.launcher)
    paths.manifest.write_text(json.dumps({"version": version, "launcher": str(paths.launcher), "launcher_sha256": _sha(text),
                                          "app_dir": str(paths.app_dir)}))


def check_app_removable(paths: Paths) -> None:
    if paths.app_dir.exists() and not safety.is_owned(paths.app_dir, paths.home):
        raise safety.SafetyError(f"{paths.app_dir} lacks a valid ownership marker; refusing to delete")


def uninstall_app(paths: Paths) -> list:
    """Remove app dir (marker-verified) and the launcher (only if byte-identical to what we wrote). Returns notes."""
    notes = []
    check_app_removable(paths)
    if paths.launcher.exists() or paths.launcher.is_symlink():
        if launcher_is_ours(paths):
            paths.launcher.unlink()
        else:
            notes.append(f"left {paths.launcher} in place: not the launcher recorded in the manifest")
    if paths.app_dir.exists():
        safety.safe_remove(paths.app_dir, paths.home)
    if paths.manifest.exists() and not paths.manifest.is_symlink():
        paths.manifest.unlink()
    return notes
