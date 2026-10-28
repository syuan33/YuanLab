from __future__ import annotations

from .config import Paths

LABEL = "local.local-ci-runner.runner"


def runner_path(paths: Paths) -> str:
    return f"{paths.bin_dir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def render_plist(paths: Paths) -> dict:
    """A per-user LaunchAgent (runs as the logged-in user; there is deliberately no UserName key and no LaunchDaemon)."""
    return {
        "Label": LABEL,
        "ProgramArguments": [str(paths.runner_dir / "run.sh")],
        "WorkingDirectory": str(paths.runner_dir),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(paths.logs / "runner.out.log"),
        "StandardErrorPath": str(paths.logs / "runner.err.log"),
        "EnvironmentVariables": {"PATH": runner_path(paths)},
    }


def bootstrap_cmd(uid: int, plist) -> list:
    return ["launchctl", "bootstrap", f"gui/{uid}", str(plist)]


def bootout_cmd(uid: int) -> list:
    return ["launchctl", "bootout", f"gui/{uid}/{LABEL}"]  # addresses exactly our label; kills nothing else


def domain_cmd(uid: int) -> list:
    """Read-only probe: exits non-zero when the user has no active GUI login session (no gui/<uid> domain)."""
    return ["launchctl", "print", f"gui/{uid}"]


def print_cmd(uid: int) -> list:
    return ["launchctl", "print", f"gui/{uid}/{LABEL}"]
