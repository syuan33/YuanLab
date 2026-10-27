from __future__ import annotations

import os
from pathlib import Path

ALLOWED_REPOS = frozenset({"syuan33/repli"})
RUNNER_LABELS = ("self-hosted", "macOS", "ARM64", "repli-local-ci")
CUSTOM_LABEL = "repli-local-ci"
HOME_ENV = "REPLI_LOCAL_CI_HOME"
DEFAULT_TIMEOUT = 3600
SUPPORTED_EVENTS = frozenset({"pull_request", "push"})
ALLOWED_PUSH_REFS = frozenset({"refs/heads/main"})
MIN_PYTHON = (3, 9)  # Apple's Command Line Tools python3 is 3.9.x; verified on 3.9 (Linux) and /usr/bin/python3 (macOS CI)
MIN_GIT = (2, 32)  # GIT_CONFIG_GLOBAL support
REQUIRED_TOOLS = ("git", "ssh", "ssh-keyscan", "ssh-keygen", "curl", "unzip", "shasum", "awk", "tr")
# Existence-only checks (contents are never read). Presence means the account is not a dedicated CI account.
PERSONAL_DATA_PATHS = (
    ".ssh/id_rsa", ".ssh/id_ed25519", ".ssh/id_ecdsa", ".ssh/id_dsa", ".aws", ".config/gcloud", ".config/gh", ".netrc",
    ".docker/config.json", ".kube", "Library/Mobile Documents", "Library/Safari", "Library/Cookies",
    "Library/Application Support/Google/Chrome", "Library/Application Support/Firefox", "Library/Keychains/login.keychain-db",
    "Dropbox", "Google Drive", "OneDrive", "Library/CloudStorage",
)
CACHE_PATHS = (".npm", ".m2", ".gradle", ".cache", "Library/Caches", ".cljs", ".shadow-cljs", ".clj-kondo")
DISCLAIMER = (
    "Local result from repli-local-ci; this is not the REPLI canonical merge gate. "
    "Replacing the gate requires separate REPLI governance review and approval."
)


class Paths:
    """All on-disk locations. Everything the tool creates lives under `home` (plus one LaunchAgent plist)."""

    def __init__(self, home: Path, launch_agents_dir: Path = None, bin_dir: Path = None):
        self.home = Path(home)
        self.bin_dir = Path(bin_dir) if bin_dir else Path.home() / ".local" / "bin"
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
    work_dir = property(lambda s: s.home / "work")
    app_dir = property(lambda s: s.home / "app")
    hooks_dir = property(lambda s: s.home / "app" / "hooks")
    manifest = property(lambda s: s.home / "manifest.json")
    attest = property(lambda s: s.home / "dedicated-user.attest")
    launcher = property(lambda s: s.bin_dir / "repli-local-ci")
