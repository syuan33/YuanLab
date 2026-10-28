"""`doctor`: mandatory prerequisites (any FAIL => non-zero exit) vs optional warnings. Read-only; never reads file contents
of personal data paths (existence checks only)."""
from __future__ import annotations

import grp
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

from . import allowlist, compat, gitops, safety, selfinstall
from .config import MIN_GIT, MIN_PYTHON, PERSONAL_DATA_PATHS, REQUIRED_TOOLS, Paths
from .runner_install import InstallError, Pin, installed_version

Check = namedtuple("Check", "level name detail")  # level: ok | fail | warn | info


class Ctx:
    """Everything doctor observes about the machine; tests substitute a fake."""

    def __init__(self, system, machine, euid, username, groups, home, py, which, tool_version, exists, runner_version=None):
        self.system, self.machine, self.euid, self.username, self.groups = system, machine, euid, username, groups
        self.home, self.py, self.which, self.tool_version, self.exists = home, py, which, tool_version, exists
        self.runner_version = runner_version or installed_version  # callable(runner_dir) -> "x.y.z" or raises InstallError

    @classmethod
    def real(cls):
        def tv(name):
            try:
                out = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=10).stdout
                m = re.search(r"(\d+)\.(\d+)", out)
                return (int(m.group(1)), int(m.group(2))) if m else None
            except (OSError, subprocess.TimeoutExpired):
                return None

        def groups():
            names = []
            for g in os.getgroups():
                try:
                    names.append(grp.getgrgid(g).gr_name)
                except KeyError:
                    pass
            return names

        try:
            user = os.getlogin()
        except OSError:
            import pwd
            user = pwd.getpwuid(os.getuid()).pw_name
        return cls(platform.system().lower(), platform.machine().lower(), os.geteuid(), user, groups(), Path.home(),
                   sys.version_info[:2], shutil.which, tv, lambda p: os.path.lexists(p))


def run_checks(ctx: Ctx, paths: Paths, pin_file: Path, workflow: Path = None):
    out = []
    add = lambda level, name, detail: out.append(Check(level, name, detail))

    ok = ctx.system == "darwin" and ctx.machine in ("arm64", "aarch64")
    add("ok" if ok else "fail", "platform", f"{ctx.system}/{ctx.machine} (required: darwin/arm64)")
    add("ok" if ctx.euid != 0 else "fail", "not root", f"euid={ctx.euid}")
    add("ok" if "admin" not in ctx.groups else "fail", "standard (non-admin) user",
        "member of group 'admin'" if "admin" in ctx.groups else f"user {ctx.username}")

    present = [p for p in PERSONAL_DATA_PATHS if ctx.exists(str(Path(ctx.home) / p))]
    att = _attestation(paths, ctx)
    if present:
        add("fail", "dedicated user: no personal data", "found: " + ", ".join(present))
    else:
        add("ok", "dedicated user: no personal data", "none of the known personal-data paths exist")
    add("ok" if att is True else "fail", "dedicated user attestation", "present" if att is True else att)

    add("ok" if ctx.py >= MIN_PYTHON else "fail", f"python>={'.'.join(map(str, MIN_PYTHON))}", ".".join(map(str, ctx.py)))
    for t in REQUIRED_TOOLS:
        p = ctx.which(t)
        add("ok" if p else "fail", f"tool {t}", p or "not on PATH")
    gv = ctx.tool_version("git") if ctx.which("git") else None
    add("ok" if gv and gv >= MIN_GIT else "fail", f"git>={'.'.join(map(str, MIN_GIT))}", "unknown" if not gv else ".".join(map(str, gv)))

    pin = None
    try:
        pin = Pin.load(pin_file)
        add("ok" if pin.pinned else "fail", "runner pin", f"v{pin.version} sha256 " + ("populated" if pin.pinned else "NOT populated"))
    except Exception as e:  # noqa: BLE001
        add("fail", "runner pin", str(e))

    exe = ctx.which("local-ci-runner")
    if not exe:
        add("fail", "manager executable", "local-ci-runner not found on PATH (run `install --apply`; PATH must include " + str(paths.bin_dir) + ")")
    elif Path(exe) != paths.launcher or not selfinstall.launcher_is_ours(paths) or not os.access(exe, os.X_OK):
        add("fail", "manager executable", f"{exe} is not the managed launcher at {paths.launcher}")
    else:
        add("ok", "manager executable", exe)

    wf = Path(workflow) if workflow else selfinstall.find_example(paths)
    if not wf or not Path(wf).is_file():
        add("fail", "proposed workflow scan", "workflow file not found; pass --workflow")
    else:
        fs = compat.scan_workflow_text(Path(wf).read_text())
        blockers = [f for f in fs if f.severity == "blocker"]
        add("fail" if blockers else "ok", "proposed workflow scan", f"{wf}: {len(blockers)} blocker(s)")
        for f in fs:
            add("fail" if f.severity == "blocker" else "warn" if f.severity == "warning" else "info",
                f"workflow:{f.id}", f"line {f.line}: {f.message}")

    if paths.known_hosts.exists():
        try:
            gitops.ensure_known_hosts(paths.known_hosts)
            add("ok", "known_hosts", "matches pinned key")
        except gitops.GitError as e:
            add("fail", "known_hosts", str(e))
    if paths.home.exists():
        mode = paths.home.stat().st_mode & 0o777
        add("ok" if mode & 0o077 == 0 else "fail", "state dir mode", oct(mode))
    if os.path.lexists(paths.config_file):
        try:
            cfg = allowlist.load(paths)
            add("ok", "allow-list config", f"{sorted(cfg.allowed_repos)} (0600, checksum ok)")
        except safety.SafetyError as e:
            add("fail", "allow-list config", str(e))
    else:
        add("info", "allow-list config", "not registered yet (created only by the approved `register --apply` step; hooks deny everything until then)")
    if paths.runner_dir.exists():
        add("ok" if safety.is_owned(paths.runner_dir, paths.home) else "fail", "runner dir ownership", str(paths.runner_dir))
        # No network: asks the installed runner itself. Mismatch / missing / malformed => NOT READY (fail closed).
        try:
            got = ctx.runner_version(paths.runner_dir)
            if pin is None:
                add("fail", "runner version", f"installed {got}, but the pin could not be loaded")
            elif got != pin.version:
                add("fail", "runner version", f"installed {got} != pinned {pin.version} (upgrade = reviewed pin change, then stop/uninstall/install/re-register)")
            else:
                add("ok", "runner version", f"{got} == pinned")
        except InstallError as e:
            add("fail", "runner version", str(e))
        add("info", "runner registered", str((paths.runner_dir / ".runner").exists()))
    else:
        add("info", "runner installed", "not yet (run install --apply after doctor passes)")

    # optional warnings: never affect the exit code
    for pr in compat.probe_host(which=ctx.which):
        if pr.status != "ok" and pr.name not in REQUIRED_TOOLS and pr.name not in ("git", "python3"):
            add("warn", f"optional {pr.name}", pr.detail + " (only matters if a step relies on it; node/java/python come from setup actions)")
    return out


def _attestation(paths: Paths, ctx: Ctx):
    try:
        d = json.loads(Path(paths.attest).read_text())
    except (OSError, ValueError):
        return f"missing; run `local-ci-runner attest-dedicated-user --apply` on the dedicated account ({paths.attest})"
    if d.get("tool") != safety.TOOL or d.get("user") != ctx.username:
        return f"attestation is for user {d.get('user')!r}, current user is {ctx.username!r}"
    return True


def exit_code(checks) -> int:
    return 1 if any(c.level == "fail" for c in checks) else 0


def render(checks) -> str:
    tag = {"ok": "ok  ", "fail": "FAIL", "warn": "warn", "info": "info"}
    lines = [f"[{tag[c.level]}] {c.name}: {c.detail}" for c in checks]
    nf = sum(c.level == "fail" for c in checks)
    lines.append(f"READY" if not nf else f"NOT READY: {nf} mandatory prerequisite(s) failed")
    return "\n".join(lines)
