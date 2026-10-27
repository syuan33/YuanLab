import json
import os
import shutil
from pathlib import Path

from repli_local_ci import readiness, selfinstall
from repli_local_ci.config import REQUIRED_TOOLS, Paths
from repli_local_ci.readiness import Ctx, exit_code, run_checks
from tests.fixtures import TmpCase

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "runner-pin.json"
WF = ROOT / "examples" / selfinstall.PROPOSED_WORKFLOW
PKG = ROOT / "src" / "repli_local_ci"


class Doctor(TmpCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "user"
        self.home.mkdir()
        self.paths = Paths(self.tmp / "state", launch_agents_dir=self.tmp / "LA", bin_dir=self.tmp / "bin")
        self.paths.home.mkdir(mode=0o700)
        selfinstall.install_app(self.paths, PKG, "t")
        self.paths.attest.write_text(json.dumps({"tool": "repli-local-ci", "user": "ci"}))
        self.kw = dict(system="darwin", machine="arm64", euid=501, username="ci", groups=["staff"], home=self.home,
                       py=(3, 9), tool_version=lambda n: (2, 39), exists=lambda p: os.path.lexists(p))
        self.exe = str(self.paths.launcher)
        self.missing = set()
        self.exe_path = None

    def which(self, n):
        if n in self.missing:
            return None
        if n == "repli-local-ci":
            return self.exe_path or self.exe
        return f"/usr/bin/{n}"

    def checks(self, pin=PIN, wf=WF, **over):
        kw = dict(self.kw, **over)
        return run_checks(Ctx(which=self.which, **kw), self.paths, pin, wf)

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
        self.missing = {"repli-local-ci"}
        self.assertNotReady(self.checks(), "manager executable")
        self.missing = set()
        self.exe_path = "/usr/local/bin/repli-local-ci"
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
        from repli_local_ci import compat
        self.assertEqual([f for f in compat.scan_workflow_text(WF.read_text()) if f.severity == "blocker"], [])

    def test_dedicated_user(self):
        self.paths.attest.unlink()
        self.assertNotReady(self.checks(), "attestation")
        self.paths.attest.write_text(json.dumps({"tool": "repli-local-ci", "user": "someone-else"}))
        self.assertNotReady(self.checks(), "attestation")
        self.paths.attest.write_text(json.dumps({"tool": "repli-local-ci", "user": "ci"}))
        for rel in (".ssh/id_ed25519", "Library/Safari", "Library/Mobile Documents", ".aws", "Dropbox"):
            target = self.home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.mkdir() if not rel.startswith(".ssh") else target.write_text("x")
            self.assertNotReady(self.checks(), "no personal data")
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        self.assertEqual(exit_code(self.checks()), 0)

    def test_state_and_known_hosts(self):
        os.chmod(self.paths.home, 0o755)
        self.assertNotReady(self.checks(), "state dir mode")
        os.chmod(self.paths.home, 0o700)
        self.paths.known_hosts.write_text("github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEVIL\n")
        self.assertNotReady(self.checks(), "known_hosts")

    def test_optional_warnings_do_not_fail(self):
        self.missing = {"gsed", "gdate", "ggrep", "node", "java", "clj-kondo"}
        c = self.checks()
        self.assertEqual(exit_code(c), 0)
        self.assertTrue(any(x.level == "warn" for x in c))
        wf = self.tmp / "warn.yml"
        wf.write_text("jobs:\n  verify:\n    runs-on: [self-hosted]\n    steps:\n      - run: sed -i 's/a/b/' f\n")
        c = self.checks(wf=wf)
        self.assertEqual(exit_code(c), 0)
        self.assertTrue(any(x.name == "workflow:sed-i" and x.level == "warn" for x in c))
