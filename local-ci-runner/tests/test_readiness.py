import json
import os
import shutil
from unittest import mock
from pathlib import Path

from local_ci_runner import readiness, selfinstall
from local_ci_runner.config import REQUIRED_TOOLS, Paths
from local_ci_runner.readiness import Ctx, exit_code, run_checks
from tests.fixtures import REAL_PIN_BYTES, TmpCase

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "runner-pin.json"
WF = ROOT / "examples" / selfinstall.PROPOSED_WORKFLOW
PKG = ROOT / "src" / "local_ci_runner"


class Doctor(TmpCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "user"
        self.home.mkdir()
        self.paths = Paths(self.tmp / "state", launch_agents_dir=self.tmp / "LA", bin_dir=self.tmp / "bin")
        self.paths.home.mkdir(mode=0o700)
        selfinstall.install_app(self.paths, PKG, "t", REAL_PIN_BYTES)
        self.paths.attest.write_text(json.dumps({"tool": "local-ci-runner", "user": "ci"}))
        self.kw = dict(system="darwin", machine="arm64", euid=501, username="ci", groups=["staff"], home=self.home,
                       py=(3, 9), tool_version=lambda n: (2, 39), exists=lambda p: os.path.lexists(p))
        self.exe = str(self.paths.launcher)
        self.missing = set()
        self.exe_path = None
        self.runner_version = lambda d: "2.338.2"

    def which(self, n):
        if n in self.missing:
            return None
        if n == "local-ci-runner":
            return self.exe_path or self.exe
        return f"/usr/bin/{n}"

    def checks(self, pin=PIN, wf=WF, **over):
        kw = dict(self.kw, **over)
        return run_checks(Ctx(which=self.which, runner_version=self.runner_version, **kw), self.paths, pin, wf)

    def failed(self, checks):
        return {c.name for c in checks if c.level == "fail"}

    def assertNotReady(self, checks, name):
        self.assertEqual(exit_code(checks), 1)
        self.assertTrue(any(name in f for f in self.failed(checks)), f"{name!r} not in {self.failed(checks)}")

    def test_baseline_ready(self):
        c = self.checks()
        self.assertEqual(self.failed(c), set(), [x for x in c if x.level == "fail"])
        self.assertEqual(exit_code(c), 0)

    def test_unpopulated_pin(self):
        d = json.loads(PIN.read_text())
        d["sha256"] = None
        p = self.tmp / "pin.json"
        p.write_text(json.dumps(d))
        self.assertNotReady(self.checks(pin=p), "runner pin")
        p.write_text("{not json")
        self.assertNotReady(self.checks(pin=p), "runner pin")

    def test_unsupported_platform(self):
        self.assertNotReady(self.checks(system="linux"), "platform")
        self.assertNotReady(self.checks(machine="x86_64"), "platform")

    def test_root_and_admin(self):
        self.assertNotReady(self.checks(euid=0), "not root")
        self.assertNotReady(self.checks(groups=["staff", "admin"]), "standard")

    def test_each_required_tool(self):
        for t in REQUIRED_TOOLS:
            self.missing = {t}
            self.assertNotReady(self.checks(), f"tool {t}")

    def test_tool_versions(self):
        self.assertNotReady(self.checks(tool_version=lambda n: (2, 30)), "git>=")
        self.assertNotReady(self.checks(tool_version=lambda n: None), "git>=")
        self.assertNotReady(self.checks(py=(3, 8)), "python>=")

    def test_manager_executable(self):
        self.missing = {"local-ci-runner"}
        self.assertNotReady(self.checks(), "manager executable")
        self.missing = set()
        self.exe_path = "/usr/local/bin/local-ci-runner"
        self.assertNotReady(self.checks(), "manager executable")
        self.exe_path = None
        self.paths.launcher.write_text("#!/bin/sh\necho tampered\n")
        self.assertNotReady(self.checks(), "manager executable")

    def test_workflow_blockers_and_missing(self):
        bad = self.tmp / "bad.yml"
        bad.write_text("jobs:\n  verify:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: sudo apt-get install -y x\n")
        self.assertNotReady(self.checks(wf=bad), "proposed workflow scan")
        self.assertNotReady(self.checks(wf=self.tmp / "nope.yml"), "proposed workflow scan")

    def test_shipped_proposed_workflow_has_no_blockers(self):
        from local_ci_runner import compat
        self.assertEqual([f for f in compat.scan_workflow_text(WF.read_text()) if f.severity == "blocker"], [])

    def test_dedicated_user(self):
        self.paths.attest.unlink()
        self.assertNotReady(self.checks(), "attestation")
        self.paths.attest.write_text(json.dumps({"tool": "local-ci-runner", "user": "someone-else"}))
        self.assertNotReady(self.checks(), "attestation")
        self.paths.attest.write_text(json.dumps({"tool": "local-ci-runner", "user": "ci"}))
        for rel in (".ssh/id_ed25519", ".config/gcloud", "Library/Mobile Documents", ".aws", "Dropbox"):
            target = self.home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.mkdir() if not rel.startswith(".ssh") else target.write_text("x")
            self.assertNotReady(self.checks(), "no personal data")
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        self.assertEqual(exit_code(self.checks()), 0)

    def test_allowlist_config_checked_when_present(self):
        from local_ci_runner import allowlist
        c = self.checks()
        self.assertTrue(any(x.name == "allow-list config" and x.level == "info" for x in c))  # unregistered: informational only
        allowlist.write(self.paths, "example-org/example-repo", ["ci/verify.sh"])
        self.assertTrue(any(x.name == "allow-list config" and x.level == "ok" for x in self.checks()))
        os.chmod(self.paths.config_file, 0o644)
        self.assertNotReady(self.checks(), "allow-list config")

    def test_installed_runner_version_must_match_the_pin(self):
        from local_ci_runner import safety
        from local_ci_runner.runner_install import InstallError
        self.paths.runner_dir.mkdir()
        safety.write_marker(self.paths.runner_dir)
        c = self.checks()  # match
        self.assertEqual(exit_code(c), 0)
        self.assertTrue(any(x.name == "runner version" and x.level == "ok" for x in c))
        self.runner_version = lambda d: "2.338.1"  # mismatch
        self.assertNotReady(self.checks(), "runner version")

        def missing(d):
            raise InstallError("installed runner not found or not executable")

        def malformed(d):
            raise InstallError("installed runner reported an unusable version")
        for fn in (missing, malformed):
            self.runner_version = fn
            self.assertNotReady(self.checks(), "runner version")

    def test_not_installed_runner_is_informational_only(self):
        self.assertFalse(self.paths.runner_dir.exists())
        c = self.checks()
        self.assertEqual(exit_code(c), 0)
        self.assertFalse(any(x.name == "runner version" for x in c))

    def test_system_created_login_keychain_is_not_a_personal_data_indicator(self):
        from local_ci_runner.config import PERSONAL_DATA_PATHS
        self.assertFalse(any("keychain" in p.lower() for p in PERSONAL_DATA_PATHS))
        kc = self.home / "Library" / "Keychains" / "login.keychain-db"
        kc.parent.mkdir(parents=True)
        kc.write_bytes(b"\0opaque-system-created-keychain")  # never opened by the tool
        queried = []
        real_exists = self.kw["exists"]
        c = self.checks(exists=lambda p: queried.append(p) or real_exists(p))
        self.assertEqual(self.failed(c), set(), [x for x in c if x.level == "fail"])
        self.assertEqual(exit_code(c), 0)
        self.assertTrue(queried)  # the personal-data probes did run ...
        self.assertFalse(any("Keychains" in p or "keychain" in p.lower() for p in queried))  # ... but never touched the keychain

    OS_CREATED = ("Library/Keychains/login.keychain-db", "Library/Safari/Bookmarks.plist", "Library/Cookies/Cookies.binarycookies")

    def make_fresh_gui_account(self):
        for rel in self.OS_CREATED:
            f = self.home / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"\0system-created")

    def test_fresh_account_after_gui_login_is_ready_and_os_dirs_are_never_touched(self):
        """Observed on a real fresh account: macOS creates Library/Safari (and the login keychain) at the first GUI login."""
        import builtins
        import pathlib
        self.make_fresh_gui_account()
        touched = []
        sensitive = lambda p: any(w in str(p) for w in ("Safari", "Cookies", "Keychains"))

        def spy(fn):
            def wrapper(*a, **k):
                if a and sensitive(a[0]):
                    touched.append(str(a[0]))
                return fn(*a, **k)
            return wrapper

        def spy_method(fn):
            def wrapper(self_, *a, **k):
                if sensitive(self_):
                    touched.append(str(self_))
                return fn(self_, *a, **k)
            return wrapper
        queried = []
        real_exists = self.kw["exists"]
        with mock.patch("os.listdir", spy(os.listdir)), mock.patch("os.scandir", spy(os.scandir)), mock.patch.object(builtins, "open", spy(builtins.open)), \
                mock.patch.object(pathlib.Path, "iterdir", spy_method(pathlib.Path.iterdir)), mock.patch.object(pathlib.Path, "read_text", spy_method(pathlib.Path.read_text)), \
                mock.patch.object(pathlib.Path, "read_bytes", spy_method(pathlib.Path.read_bytes)), mock.patch.object(pathlib.Path, "glob", spy_method(pathlib.Path.glob)), \
                mock.patch.object(pathlib.Path, "rglob", spy_method(pathlib.Path.rglob)):
            c = self.checks(exists=lambda p: queried.append(p) or real_exists(p))
        self.assertEqual(self.failed(c), set(), [x for x in c if x.level == "fail"])
        self.assertEqual(exit_code(c), 0)
        self.assertTrue(queried)
        self.assertEqual(touched, [])  # never opened, listed or read
        self.assertFalse(any(sensitive(p) for p in queried))  # not even an existence probe

    def test_os_created_dirs_do_not_hide_attestation_or_strong_indicators(self):
        self.make_fresh_gui_account()
        self.paths.attest.unlink()
        self.assertNotReady(self.checks(), "attestation")
        self.paths.attest.write_text(json.dumps({"tool": "local-ci-runner", "user": "ci"}))
        self.assertEqual(exit_code(self.checks()), 0)
        (self.home / ".config" / "gh").mkdir(parents=True)
        self.assertNotReady(self.checks(), "no personal data")

    def test_strong_indicator_set_is_exactly_the_reviewed_one(self):
        from local_ci_runner.config import PERSONAL_DATA_PATHS
        self.assertEqual(set(PERSONAL_DATA_PATHS), {
            ".ssh/id_rsa", ".ssh/id_ed25519", ".ssh/id_ecdsa", ".ssh/id_dsa", ".aws", ".config/gcloud", ".config/gh", ".netrc",
            ".docker/config.json", ".kube", "Library/Mobile Documents", "Library/Application Support/Google/Chrome",
            "Library/Application Support/Firefox", "Dropbox", "Google Drive", "OneDrive", "Library/CloudStorage"})
        for generic in ("Library/Safari", "Library/Cookies", "Library/Keychains/login.keychain-db"):
            self.assertNotIn(generic, PERSONAL_DATA_PATHS)

    def test_login_keychain_does_not_mask_real_personal_data(self):
        kc = self.home / "Library" / "Keychains" / "login.keychain-db"
        kc.parent.mkdir(parents=True)
        kc.write_bytes(b"x")
        self.assertEqual(exit_code(self.checks()), 0)
        (self.home / ".aws").mkdir()
        self.assertNotReady(self.checks(), "no personal data")

    def test_every_listed_personal_data_indicator_still_fails(self):
        from local_ci_runner.config import PERSONAL_DATA_PATHS
        for rel in PERSONAL_DATA_PATHS:
            target = self.home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if rel.startswith(".ssh/id_") or rel.endswith((".netrc", "config.json")):
                target.write_text("x")
            else:
                target.mkdir()
            self.assertNotReady(self.checks(), "no personal data")
            shutil.rmtree(target) if target.is_dir() else target.unlink()
            self.assertEqual(exit_code(self.checks()), 0, rel)

    def test_attestation_is_still_required_with_a_normal_keychain(self):
        kc = self.home / "Library" / "Keychains" / "login.keychain-db"
        kc.parent.mkdir(parents=True)
        kc.write_bytes(b"x")
        self.paths.attest.unlink()
        self.assertNotReady(self.checks(), "attestation")

    def test_state_and_known_hosts(self):
        os.chmod(self.paths.home, 0o755)
        self.assertNotReady(self.checks(), "state dir mode")
        os.chmod(self.paths.home, 0o700)
        self.paths.known_hosts.write_text("github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEVIL\n")
        self.assertNotReady(self.checks(), "known_hosts")

    def test_optional_warnings_do_not_fail(self):
        self.missing = {"gsed", "gdate", "ggrep", "node", "java"}
        c = self.checks()
        self.assertEqual(exit_code(c), 0)
        self.assertTrue(any(x.level == "warn" for x in c))
        wf = self.tmp / "warn.yml"
        wf.write_text("jobs:\n  verify:\n    runs-on: [self-hosted]\n    steps:\n      - run: sed -i 's/a/b/' f\n")
        c = self.checks(wf=wf)
        self.assertEqual(exit_code(c), 0)
        self.assertTrue(any(x.name == "workflow:sed-i" and x.level == "warn" for x in c))
