import io
import contextlib
import os
from unittest import mock

from repli_local_ci import cli, compat
from tests.fixtures import TmpCase


class CLI(TmpCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        env = {"REPLI_LOCAL_CI_HOME": str(self.tmp / "home"), "HOME": str(self.tmp)}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_dry_runs_default_and_no_side_effects(self):
        for cmd in (["install"], ["install", "--dry-run"], ["register"], ["register", "--dry-run"], ["uninstall"], ["uninstall", "--dry-run"], ["start", "--dry-run"], ["stop", "--dry-run"], ["attest-dedicated-user"], ["clean"]):
            rc, out, _ = self.run_cli(*cmd)
            self.assertEqual(rc, 0, cmd)
            self.assertIn("DRY RUN", out)
        self.assertFalse((self.tmp / "home").exists())
        self.assertFalse((self.tmp / "Library").exists())
        self.assertFalse((self.tmp / ".local").exists())

    def test_apply_and_dry_run_conflict(self):
        rc, _, _ = self.run_cli("install", "--dry-run", "--apply")
        self.assertNotEqual(rc, 0)

    def test_install_apply_refuses_unpinned_runner(self):
        import json
        unpinned = self.tmp / "pin.json"
        unpinned.write_text(json.dumps({"version": "1.0.0", "asset": "a.tgz", "sha256": None,
                                        "url": "https://github.com/actions/runner/releases/download/v1.0.0/a.tgz"}))
        with mock.patch.object(cli, "PIN_FILE", unpinned):
            rc, _, err = self.run_cli("install", "--apply")
        self.assertNotEqual(rc, 0)
        self.assertFalse((self.tmp / "Library").exists())

    def test_register_apply_needs_confirmation(self):
        with mock.patch.dict(os.environ, {"REPLI_RUNNER_TOKEN": "x"}):
            rc, _, _ = self.run_cli("register", "--apply")
        self.assertNotEqual(rc, 0)

    def test_run_rejects_non_sha_and_other_repo(self):
        self.assertNotEqual(self.run_cli("run", "--repo", "syuan33/repli", "--sha", "main")[0], 0)
        self.assertNotEqual(self.run_cli("run", "--repo", "x/y", "--sha", "a" * 40)[0], 0)

    def test_doctor_and_status_run_readonly(self):
        rc, out, _ = self.run_cli("doctor")
        self.assertEqual(rc, 1)  # this test host is not a provisioned dedicated macOS arm64 account
        self.assertIn("not root", out)
        self.assertIn("NOT READY", out)
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertFalse((self.tmp / "home").exists())

    def test_all_subcommands_exist(self):
        _, out, _ = self.run_cli("--help")
        for c in "doctor install register start status run stop uninstall cancel attest-dedicated-user hook report clean verify-pin".split():
            self.assertIn(c, out)


class Compat(TmpCase):
    def test_flags_linux_only_steps_loudly(self):
        wf = "jobs:\n  verify:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: sudo apt-get install -y shellcheck\n      - run: sed -i 's/a/b/' f\n      - run: readlink -f x && date -d yesterday\n      - run: echo /home/runner/work\n    container: ubuntu\n"
        ids = {f.id for f in compat.scan_workflow_text(wf)}
        for expected in ("apt-get", "sudo", "sed-i", "readlink-f", "date-d", "home-runner", "container", "runs-on-ubuntu"):
            self.assertIn(expected, ids)

    def test_clean_workflow(self):
        wf = "jobs:\n  verify:\n    runs-on: [self-hosted, macOS, ARM64, repli-local-ci]\n    steps:\n      - run: dev/verify.sh -p standard -o out\n"
        self.assertEqual([f for f in compat.scan_workflow_text(wf) if f.severity == "blocker"], [])

    def test_host_probe_reports_missing_tools(self):
        res = compat.probe_host(which=lambda n: None, bash_version=lambda: (3, 2))
        by = {r.name: r for r in res}
        self.assertEqual(by["clj-kondo"].status, "missing")
        self.assertEqual(by["bash>=4"].status, "missing")
        self.assertEqual(by["gnu-sed"].status, "missing")
