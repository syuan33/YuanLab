import json
import os
import subprocess
from pathlib import Path

from repli_local_ci import pincheck, report, safety, selfinstall
from repli_local_ci.config import Paths
from repli_local_ci.runner_install import Pin
from tests.fixtures import TmpCase

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "repli_local_ci"


class SelfInstall(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = Paths(self.tmp / "state", bin_dir=self.tmp / "bin")
        self.p.home.mkdir(mode=0o700)

    def test_launcher_runs_without_source_checkout(self):
        selfinstall.install_app(self.p, PKG, "t")
        env = {"PATH": os.environ["PATH"], "HOME": str(self.tmp), "REPLI_LOCAL_CI_HOME": str(self.tmp / "h2")}
        r = subprocess.run([str(self.p.launcher), "--help"], cwd=self.tmp, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("doctor", r.stdout)
        self.assertNotIn(str(ROOT), (self.p.launcher.read_text()))  # does not point into the checkout
        self.assertTrue((self.p.hooks_dir / "job-started.sh").read_text().startswith("#!/bin/bash"))
        self.assertTrue(os.access(self.p.hooks_dir / "job-started.sh", os.X_OK))

    def test_uninstall_ownership(self):
        selfinstall.install_app(self.p, PKG, "t")
        self.assertTrue(selfinstall.launcher_is_ours(self.p))
        self.assertEqual(selfinstall.uninstall_app(self.p), [])
        self.assertFalse(self.p.launcher.exists() or self.p.app_dir.exists())

    def test_symlinked_launcher_not_removed(self):
        selfinstall.install_app(self.p, PKG, "t")
        real = self.p.bin_dir / "real"
        real.write_bytes(self.p.launcher.read_bytes())
        self.p.launcher.unlink()
        self.p.launcher.symlink_to(real)
        notes = selfinstall.uninstall_app(self.p)
        self.assertTrue(self.p.launcher.is_symlink() and real.exists())
        self.assertTrue(notes)

    def test_unmarked_app_dir_refused(self):
        selfinstall.install_app(self.p, PKG, "t")
        (self.p.app_dir / safety.MARKER).unlink()
        with self.assertRaises(safety.SafetyError):
            selfinstall.uninstall_app(self.p)
        self.assertTrue(self.p.launcher.exists())


BODY = "## SHA-256 Checksums\n- actions-runner-osx-arm64-9.9.9.tar.gz <!-- BEGIN SHA osx-arm64 -->{h}<!-- END SHA osx-arm64 -->\n"
H = "a" * 64


class PinCheck(TmpCase):
    def pin(self, sha=H):
        return Pin("9.9.9", "actions-runner-osx-arm64-9.9.9.tar.gz", "https://github.com/actions/runner/releases/download/v9.9.9/actions-runner-osx-arm64-9.9.9.tar.gz", sha)

    def fetch(self, body=None, tag="v9.9.9", url=None, latest="v9.9.9"):
        pin = self.pin()

        def f(u):
            if u.endswith("/latest"):
                return {"tag_name": latest}
            return {"tag_name": tag, "draft": False, "prerelease": False, "body": BODY.format(h=H) if body is None else body,
                    "assets": [{"name": pin.asset, "browser_download_url": url or pin.url}]}
        return f

    def test_match(self):
        self.assertEqual(pincheck.check_pin(self.pin(), self.fetch()), ([], []))

    def test_newer_release_is_a_note_not_a_failure(self):
        problems, notes = pincheck.check_pin(self.pin(), self.fetch(latest="v10.0.0"))
        self.assertEqual(problems, [])
        self.assertIn("v10.0.0", notes[0])

    def test_mismatches_fail(self):
        for kw, pin in [({}, self.pin("b" * 64)), ({"body": "no checksums"}, self.pin()), ({"url": "https://evil/x"}, self.pin()),
                        ({"tag": "v1.0.0"}, self.pin()), ({}, self.pin(None))]:
            problems, _ = pincheck.check_pin(pin, self.fetch(**kw))
            self.assertTrue(problems, kw)


class Report(TmpCase):
    def test_report_records_integration_head_base_and_redacts(self):
        p = Paths(self.tmp / "state", bin_dir=self.tmp / "bin")
        ev = self.tmp / "ev.json"
        ev.write_text(json.dumps({"pull_request": {"head": {"sha": "1" * 40}, "base": {"sha": "2" * 40}}}))
        res = self.tmp / "res"
        res.mkdir()
        (res / "r.json").write_text("{}")
        summ = self.tmp / "summary.md"
        summ.write_text("")
        tok = "ghp_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6J7k8"
        env = {"GITHUB_REPOSITORY": "syuan33/repli", "GITHUB_SHA": "3" * 40, "GITHUB_EVENT_PATH": str(ev), "GITHUB_RUN_ID": "7",
               "GITHUB_RUN_ATTEMPT": "1", "GITHUB_WORKFLOW": f"CI token={tok}", "RUNNER_NAME": "r"}
        rep = report.build(env, res, "success", p)
        d = report.write(rep, p, str(summ))
        data = json.loads((d / "report.json").read_text())
        self.assertEqual((data["integration_sha"], data["head_sha"], data["base_sha"]), ("3" * 40, "1" * 40, "2" * 40))
        self.assertEqual(data["results_files"][0]["path"], "r.json")
        self.assertNotIn(tok, (d / "report.json").read_text())
        self.assertIn("canonical merge gate", summ.read_text())
