from __future__ import annotations

import os
from pathlib import Path

RUNNER_LABELS = ("self-hosted", "macOS", "ARM64", "local-ci-runner")
CUSTOM_LABEL = "local-ci-runner"
HOME_ENV = "LOCAL_CI_RUNNER_HOME"
DEFAULT_TIMEOUT = 3600
SUPPORTED_EVENTS = frozenset({"pull_request", "push"})
MIN_PYTHON = (3, 9)  # Apple's Command Line Tools python3 is 3.9.x; verified on 3.9 (Linux) and /usr/bin/python3 (macOS CI)
MIN_GIT = (2, 32)  # GIT_CONFIG_GLOBAL support
REQUIRED_TOOLS = ("git", "ssh", "ssh-keyscan", "ssh-keygen", "curl", "unzip", "shasum", "awk", "tr")
# Existence-only checks (contents are never read). Presence means the account is not a dedicated CI account.
# Deliberately NOT listed: Library/Keychains/login.keychain-db. macOS creates an (empty) login keychain at first GUI login, and this
# tool requires that login, so its mere existence proves nothing. It is never opened, listed or unlocked; the operator attests it holds
# no personal or unrelated credentials (see `attest-dedicated-user`).
PERSONAL_DATA_PATHS = (
    ".ssh/id_rsa", ".ssh/id_ed25519", ".ssh/id_ecdsa", ".ssh/id_dsa", ".aws", ".config/gcloud", ".config/gh", ".netrc",
    ".docker/config.json", ".kube", "Library/Mobile Documents", "Library/Safari", "Library/Cookies",
    "Library/Application Support/Google/Chrome", "Library/Application Support/Firefox",
    "Dropbox", "Google Drive", "OneDrive", "Library/CloudStorage",
)
CACHE_PATHS = (".npm", ".m2", ".gradle", ".cache", "Library/Caches")
DISCLAIMER = (
    "Local result from local-ci-runner; this is not your repository's required merge gate. "
    "Using it as one requires a separate, explicit governance decision by the repository owners."
)


class Paths:
    """All on-disk locations. Everything the tool creates lives under `home` (plus one LaunchAgent plist)."""

    def __init__(self, home: Path, launch_agents_dir: Path = None, bin_dir: Path = None):
        self.home = Path(home)
        self.bin_dir = Path(bin_dir) if bin_dir else Path.home() / ".local" / "bin"
        self.launch_agents = Path(launch_agents_dir) if launch_agents_dir else Path.home() / "Library" / "LaunchAgents"

    @classmethod
    def from_env(cls) -> "Paths":
        return cls(Path(os.environ.get(HOME_ENV) or (Path.home() / ".local-ci-runner")))

    runner_dir = property(lambda s: s.home / "runner")
    runs = property(lambda s: s.home / "runs")
    reports = property(lambda s: s.home / "reports")
    queue = property(lambda s: s.home / "queue")
    logs = property(lambda s: s.home / "logs")
    known_hosts = property(lambda s: s.home / "known_hosts")
    downloads = property(lambda s: s.home / "downloads")
    plist = property(lambda s: s.launch_agents / "local.local-ci-runner.runner.plist")
    work_dir = property(lambda s: s.home / "work")
    app_dir = property(lambda s: s.home / "app")
    hooks_dir = property(lambda s: s.home / "app" / "hooks")
    manifest = property(lambda s: s.home / "manifest.json")
    attest = property(lambda s: s.home / "dedicated-user.attest")
    launcher = property(lambda s: s.bin_dir / "local-ci-runner")
    config_file = property(lambda s: s.home / "config.json")
