from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import tarfile
import urllib.request
import uuid
from pathlib import Path

from . import __version__, allowlist, launchd, safety, selfinstall
from .config import CUSTOM_LABEL, Paths

_SHA256 = re.compile(r"[0-9a-f]{64}")
DEFAULT_VERIFY = ("ci/verify.sh", "-o", "{results_dir}")
OFFICIAL_PREFIX = "https://github.com/actions/runner/releases/download/"


class InstallError(Exception):
    pass


class Pin:
    def __init__(self, version, asset, url, sha256):
        self.version, self.asset, self.url, self.sha256 = version, asset, url, sha256

    @classmethod
    def load(cls, path: Path) -> "Pin":
        d = json.loads(Path(path).read_text())
        pin = cls(d.get("version"), d.get("asset"), d.get("url"), d.get("sha256"))
        expected = f"{OFFICIAL_PREFIX}v{pin.version}/{pin.asset}"
        if not pin.version or pin.url != expected:
            raise InstallError(f"runner-pin.json url must be exactly {expected}")
        return pin

    @property
    def pinned(self) -> bool:
        return isinstance(self.sha256, str) and bool(_SHA256.fullmatch(self.sha256))


def sha256_file(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def default_fetch(url: str, dest: Path) -> None:
    if not url.startswith("https://"):
        raise InstallError("only https downloads are allowed")
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:  # noqa: S310 (https enforced above)
        shutil.copyfileobj(r, f)


def download_verified(url, dest: Path, expected_sha256, fetch=default_fetch) -> None:
    if not (isinstance(expected_sha256, str) and _SHA256.fullmatch(expected_sha256)):
        raise InstallError("runner SHA-256 is not pinned; refusing to download (see runner-pin.json / README)")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if dest.exists():
        dest.unlink()
    fetch(url, dest)
    got = sha256_file(dest)
    if got != expected_sha256:
        dest.unlink()
        raise InstallError(f"SHA-256 mismatch for runner download: expected {expected_sha256}, got {got}")


def safe_extract(tar_path: Path, dest: Path) -> None:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = dest.resolve()
    with tarfile.open(tar_path) as t:
        members = t.getmembers()
        for m in members:
            target = (root / m.name).resolve()
            if root != target and root not in target.parents:
                raise InstallError(f"unsafe path in archive: {m.name}")
            if m.issym() or m.islnk():
                link = (target.parent / m.linkname).resolve() if m.issym() else (root / m.linkname).resolve()
                if root not in link.parents and link != root:
                    raise InstallError(f"unsafe link in archive: {m.name} -> {m.linkname}")
            if not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
                raise InstallError(f"unsupported member type in archive: {m.name}")
        for m in members:
            m.mode &= 0o755  # no setuid/setgid/world-write
        try:
            t.extractall(root, members=members, filter="data")  # Python >= 3.12 / patched 3.9-3.11
        except TypeError:
            t.extractall(root, members=members)  # noqa: S202 (members validated above)


_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def installed_version(runner_dir, run=None) -> str:
    """Ask the installed official runner for its version (`bin/Runner.Listener --version`). Prints a version only; the call
    needs no network. Anything but a single clean x.y.z line is an error (fail closed)."""
    exe = Path(runner_dir) / "bin" / "Runner.Listener"
    if exe.is_symlink() or not exe.is_file() or not os.access(exe, os.X_OK):
        raise InstallError(f"installed runner not found or not executable: {exe}")
    if run is None:
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        try:
            r = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30, env=env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise InstallError(f"could not run the installed runner's version command: {e}")
        rc, out = r.returncode, r.stdout
    else:
        rc, out = run([str(exe), "--version"])
    text = out.strip() if isinstance(out, str) else ""
    if rc != 0 or "\n" in text or not _VERSION.fullmatch(text):
        raise InstallError(f"installed runner reported an unusable version (exit {rc}, output {text[:40]!r})")
    return text


def verify_installed_version(runner_dir, pin, probe=installed_version) -> str:
    """Fail closed unless the installed runner is exactly the version in runner-pin.json."""
    got = probe(Path(runner_dir))
    if got != pin.version:
        raise InstallError(f"installed runner version {got} != pinned {pin.version}: refusing (upgrades are reviewed pin changes: "
                           "stop, uninstall, install, re-register)")
    return got


def default_executor(argv, env=None, cwd=None) -> int:
    return subprocess.run(argv, env=env, cwd=cwd).returncode


class Installer:
    def __init__(self, paths: Paths, pin: Pin, executor=default_executor, fetch=default_fetch, euid=None, uid=None, gate=None,
                 version_probe=installed_version, pin_file=None):
        self.p, self.pin, self.exec, self.fetch, self.gate = paths, pin, executor, fetch, gate  # gate: callable raising if not ready
        self.version_probe = version_probe
        self.pin_file = pin_file  # the reviewed source runner-pin.json that is copied into the managed application
        self.euid, self.uid = euid, uid if uid is not None else os.getuid()

    def _guard(self):
        safety.refuse_root(self.euid)

    # ---- install -------------------------------------------------------
    def plan_install(self):
        pin = self.pin
        return [
            f"create {self.p.home} (mode 0700)",
            f"download {pin.url}",
            f"verify SHA-256 == {pin.sha256 or '<NOT PINNED - apply will refuse>'}",
            f"extract (validated, no links outside) into {self.p.runner_dir} and write ownership marker",
            f"copy the exact reviewed runner-pin.json into {self.p.pin_file} (read-only) and record its SHA-256 in {self.p.manifest}",
            f"copy the manager into {self.p.app_dir} and write launcher {self.p.launcher} (user-owned PATH location; no source checkout needed)",
            f"create managed runner work dir {self.p.work_dir} (ownership marker) and runner hooks in {self.p.hooks_dir}",
            f"write {self.p.runner_dir}/.env (job hooks + PATH)",
            f"write LaunchAgent {self.p.plist} (user agent, no root, not started)",
            "NOT done: register runner, start service, change GitHub settings",
        ]

    def _pin_payload(self) -> bytes:
        """The exact reviewed pin file, refused unless it is a regular file that loads and equals the Pin used for the download."""
        f = Path(self.pin_file) if self.pin_file else None
        if f is None or f.is_symlink() or not f.is_file():
            raise InstallError("source runner-pin.json is missing or a symlink; refusing to install")
        try:
            raw = f.read_bytes()
            if len(raw) > 65536:
                raise ValueError("too large")
            loaded = Pin.load(f)
        except InstallError:
            raise
        except Exception as e:  # noqa: BLE001 - any malformed pin is fatal
            raise InstallError(f"source runner-pin.json is malformed: {e}")
        mine = (self.pin.version, self.pin.asset, self.pin.url, self.pin.sha256)
        if (loaded.version, loaded.asset, loaded.url, loaded.sha256) != mine:
            raise InstallError("source runner-pin.json differs from the pin used for this installation; refusing")
        return raw

    def apply_install(self):
        self._guard()
        if not self.pin.pinned:
            raise InstallError("runner SHA-256 is not pinned in runner-pin.json; refusing to install")
        pin_bytes = self._pin_payload()  # validated before any side effect
        if self.p.runner_dir.exists():
            raise InstallError(f"{self.p.runner_dir} already exists; run uninstall first (upgrade = uninstall + install)")
        self.p.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.p.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.p.app_dir.exists() or self.p.work_dir.exists():
            raise InstallError("app/work directory already exists; run uninstall first")
        tgz = self.p.downloads / self.pin.asset
        download_verified(self.pin.url, tgz, self.pin.sha256, self.fetch)
        tmp = self.p.home / f"runner.tmp-{uuid.uuid4().hex[:8]}"
        try:
            safe_extract(tgz, tmp)
            os.rename(tmp, self.p.runner_dir)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        safety.write_marker(self.p.runner_dir)
        selfinstall.install_app(self.p, Path(__file__).resolve().parent, __version__, pin_bytes)
        os.mkdir(self.p.work_dir, 0o700)
        safety.write_marker(self.p.work_dir)
        env_lines = [f"ACTIONS_RUNNER_HOOK_JOB_STARTED={self.p.hooks_dir / 'job-started.sh'}",
                     f"ACTIONS_RUNNER_HOOK_JOB_COMPLETED={self.p.hooks_dir / 'job-completed.sh'}",
                     f"PATH={launchd.runner_path(self.p)}"]
        (self.p.runner_dir / ".env").write_text("\n".join(env_lines) + "\n")
        self.p.launch_agents.mkdir(parents=True, exist_ok=True)
        self.p.plist.write_bytes(plistlib.dumps(launchd.render_plist(self.p)))

    # ---- register ------------------------------------------------------
    def _runner_name(self):
        return "local-ci-runner-" + re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname().split(".")[0])[:40]

    def register_argv(self, repo):
        """The one definition of the config.sh invocation, used by both the displayed plan and the real call.
        --disableupdate: the pinned, checksum-verified runner must never replace itself; upgrades are reviewed pin changes."""
        return [str(self.p.runner_dir / "config.sh"), "--unattended", "--url", f"https://github.com/{repo}",
                "--labels", CUSTOM_LABEL, "--name", self._runner_name(), "--work", str(self.p.work_dir), "--disableupdate"]

    def plan_register(self, repo, verify_command=DEFAULT_VERIFY):
        allowlist.validate_repo_name(repo)
        allowlist.validate_command(list(verify_command))
        return [
            f"write protected config {self.p.config_file} (mode 0600, exclusive create): allowed_repos=[{repo}], verify_command={list(verify_command)}",
            "run " + " ".join(self.register_argv(repo)),
            "the registration token is passed ONLY via env ACTIONS_RUNNER_INPUT_TOKEN (from $LOCAL_CI_RUNNER_TOKEN or stdin), never argv",
            "REQUIRES: GitHub repo admin to create the token (Settings > Actions > Runners > New self-hosted runner)",
            "REQUIRES: explicit user approval (--i-have-admin-approval)",
            "REQUIRES: `doctor` passing in full, including the dedicated-user precondition (checked again at apply time)",
        ]

    def apply_register(self, repo, token, confirm_admin_approved, verify_command=DEFAULT_VERIFY):
        self._guard()
        allowlist.validate_repo_name(repo)
        allowlist.validate_command(list(verify_command))
        if self.gate:
            self.gate()
        if not confirm_admin_approved:
            raise InstallError("registration needs explicit approval (--i-have-admin-approval)")
        if not token:
            raise InstallError("no registration token supplied (set LOCAL_CI_RUNNER_TOKEN or use --token-stdin)")
        cfg = self.p.runner_dir / "config.sh"
        if not cfg.is_file():
            raise InstallError("runner is not installed")
        if (self.p.runner_dir / ".runner").exists():
            raise InstallError("runner already registered; uninstall first")
        verify_installed_version(self.p.runner_dir, self.pin, self.version_probe)  # never register a runner that is not the pinned one
        env = {k: os.environ[k] for k in ("HOME", "LANG") if k in os.environ}
        env["PATH"] = launchd.runner_path(self.p)  # config.sh records this into the runner's .path
        env["ACTIONS_RUNNER_INPUT_TOKEN"] = token
        if self.p.config_file.exists() or self.p.config_file.is_symlink():
            raise InstallError(f"{self.p.config_file} already exists; uninstall first (the allow-list is never overwritten)")
        allowlist.write(self.p, repo, verify_command)  # explicit, approved registration step creates the allow-list
        rc = self.exec(self.register_argv(repo), env=env, cwd=str(self.p.runner_dir))
        if rc:
            allowlist.remove(self.p)
            raise InstallError(f"config.sh failed with exit code {rc}; protected config removed")

    # ---- start / stop --------------------------------------------------
    def plan_start(self):
        return [" ".join(launchd.domain_cmd(self.uid)) + "   (read-only: the dedicated user must have an active GUI login session)",
                " ".join(launchd.bootstrap_cmd(self.uid, self.p.plist))]

    def plan_stop(self):
        return [" ".join(launchd.bootout_cmd(self.uid))]

    def apply_start(self):
        self._guard()
        if self.gate:
            self.gate()
        if not self.p.plist.exists():
            raise InstallError("not installed")
        if not (self.p.runner_dir / ".runner").exists():
            raise InstallError("runner is not registered (manual, admin-approved step); refusing to start")
        verify_installed_version(self.p.runner_dir, self.pin, self.version_probe)
        if self.exec(launchd.domain_cmd(self.uid)):
            raise InstallError(f"no active GUI login session for this user (launchctl domain gui/{self.uid} is unavailable): the dedicated "
                               "user must be logged in to the macOS desktop for the LaunchAgent to load; log in as that user, then retry")
        rc = self.exec(launchd.bootstrap_cmd(self.uid, self.p.plist))
        if rc:
            raise InstallError(f"launchctl bootstrap failed (exit {rc}). Most likely the dedicated user is not logged in to an active GUI "
                               f"session (gui/{self.uid} required), or the service is already loaded (`status`, `stop`)")

    def apply_stop(self):
        self._guard()
        self.exec(launchd.bootout_cmd(self.uid))  # non-zero == not loaded; harmless

    # ---- uninstall -----------------------------------------------------
    def plan_uninstall(self):
        return [
            " ".join(launchd.bootout_cmd(self.uid)),
            f"remove {self.p.plist} (only if its Label is {launchd.LABEL})",
            f"remove {self.p.runner_dir}, {self.p.work_dir}, {self.p.app_dir} (each only with a valid ownership marker)",
            f"remove {self.p.launcher} (only if byte-identical to the launcher recorded in {self.p.manifest})",
            f"remove {self.p.config_file} (protected allow-list; only a regular file owned by the current user)",
            f"KEPT: {self.p.reports}, {self.p.home}/queue, {self.p.attest} (delete by hand if wanted)",
            "MANUAL: deregister the runner in GitHub (Settings > Actions > Runners) or `config.sh remove` with a removal token (repo admin)",
        ]

    def apply_uninstall(self) -> list:
        self._guard()
        for d in (self.p.runner_dir, self.p.work_dir):
            if d.exists() and not safety.is_owned(d, self.p.home):
                raise safety.SafetyError(f"{d} lacks a valid ownership marker; refusing to delete")
        selfinstall.check_app_removable(self.p)
        plist_ours = False
        if self.p.plist.exists():
            try:
                plist_ours = plistlib.loads(self.p.plist.read_bytes()).get("Label") == launchd.LABEL
            except Exception:
                plist_ours = False
            if not plist_ours:
                raise safety.SafetyError(f"{self.p.plist} is not this tool's plist; refusing to delete")
        self.exec(launchd.bootout_cmd(self.uid))
        if plist_ours:
            self.p.plist.unlink()
        for d in (self.p.runner_dir, self.p.work_dir):
            if d.exists():
                safety.safe_remove(d, self.p.home)
        notes = selfinstall.uninstall_app(self.p)
        if (self.p.config_file.exists() or self.p.config_file.is_symlink()) and not allowlist.remove(self.p):
            notes.append(f"left {self.p.config_file} in place: not a regular file owned by the current user")
        return notes
