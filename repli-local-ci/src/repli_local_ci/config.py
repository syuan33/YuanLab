from __future__ import annotations

import os
from pathlib import Path

ALLOWED_REPOS = frozenset({"syuan33/repli"})
RUNNER_LABELS = ("self-hosted", "macOS", "ARM64", "repli-local-ci")
CUSTOM_LABEL = "repli-local-ci"
HOME_ENV = "REPLI_LOCAL_CI_HOME"
DEFAULT_TIMEOUT = 3600
DISCLAIMER = (
    "Local result from repli-local-ci; this is not the REPLI canonical merge gate. "
    "Replacing the gate requires separate REPLI governance review and approval."
)


class Paths:
    """All on-disk locations. Everything the tool creates lives under `home` (plus one LaunchAgent plist)."""

    def __init__(self, home: Path, launch_agents_dir: Path = None):
        self.home = Path(home)
        self.launch_agents = Path(launch_agents_dir) if launch_agents_dir else Path.home() / "Library" / "LaunchAgents"

    @classmethod
    def from_env(cls) -> "Paths":
        return cls(Path(os.environ.get(HOME_ENV) or (Path.home() / ".repli-local-ci")))

    runner_dir = property(lambda s: s.home / "runner")
    runs = property(lambda s: s.home / "runs")
    reports = property(lambda s: s.home / "reports")
    queue = property(lambda s: s.home / "queue")
    logs = property(lambda s: s.home / "logs")
    known_hosts = property(lambda s: s.home / "known_hosts")
    downloads = property(lambda s: s.home / "downloads")
    plist = property(lambda s: s.launch_agents / "local.repli-local-ci.runner.plist")
