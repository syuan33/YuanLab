import json
import os
from pathlib import Path

from local_ci_runner import allowlist, hooks, safety
from local_ci_runner.config import Paths
from tests.fixtures import TmpCase

SHA = "a" * 40
R = "example-org/example-repo"


class Hooks(TmpCase):
    def setUp(self):
        super().setUp()
        self.paths = Paths(self.tmp / "state", bin_dir=self.tmp / "bin")
        self.paths.home.mkdir(mode=0o700)
        allowlist.write(self.paths, R, ["ci/verify.sh"])
        os.mkdir(self.paths.work_dir, 0o700)
        safety.write_marker(self.paths.work_dir)
        self.ws = self.paths.work_dir / "example-repo" / "example-repo"
        self.ws.mkdir(parents=True)
        self.rt = self.paths.work_dir / "_temp"
        self.rt.mkdir()

    _n = 0

    def event(self, head=R, base=R, fork=False):
        Hooks._n += 1
        p = self.tmp / f"event{Hooks._n}.json"
        p.write_text(json.dumps({"pull_request": {"head": {"repo": {"full_name": head, "fork": fork}}, "base": {"repo": {"full_name": base}}}}))
        return str(p)

    def env(self, **kw):
        e = {"GITHUB_REPOSITORY": R, "GITHUB_SHA": SHA, "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/5/merge",
             "GITHUB_EVENT_PATH": self.event(), "GITHUB_WORKSPACE": str(self.ws), "RUNNER_TEMP": str(self.rt)}
        e.update(kw)
        return e

    def started(self, env):
        hooks.job_started(env, self.paths, euid=1000)

    def test_same_repo_pr_integration_ok(self):
        self.started(self.env())

    def test_push_to_main_ok_other_ref_refused(self):
        self.started(self.env(GITHUB_EVENT_NAME="push", GITHUB_REF="refs/heads/main"))
        with self.assertRaises(safety.SafetyError):
            self.started(self.env(GITHUB_EVENT_NAME="push", GITHUB_REF="refs/heads/evil"))

    def test_refusals(self):
        cases = [
            self.env(GITHUB_REPOSITORY="example-org/other"),
            self.env(GITHUB_SHA="main"),
            self.env(GITHUB_EVENT_NAME="pull_request_target"),
            self.env(GITHUB_EVENT_NAME="workflow_dispatch"),
            self.env(GITHUB_REF="refs/pull/5/head"),
            self.env(GITHUB_REF="refs/heads/feature"),
            self.env(GITHUB_EVENT_PATH=self.event(head="mallory/example-repo", fork=True)),
            self.env(GITHUB_EVENT_PATH=self.event(head="mallory/example-repo")),
            self.env(GITHUB_EVENT_PATH=self.event(fork=True)),
            self.env(GITHUB_EVENT_PATH=str(self.tmp / "missing.json")),
            self.env(GITHUB_WORKSPACE=str(self.tmp)),
        ]
        for e in cases:
            with self.assertRaises(safety.SafetyError, msg=str(e)):
                self.started(e)

    def test_root_refused_and_unmanaged_workdir_refused(self):
        with self.assertRaises(safety.SafetyError):
            hooks.job_started(self.env(), self.paths, euid=0)
        (self.paths.work_dir / safety.MARKER).unlink()
        with self.assertRaises(safety.SafetyError):
            self.started(self.env())

    def test_completed_cleans_only_managed_workspace_and_secrets(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "precious").write_text("1")
        (self.ws / "f").write_text("x")
        (self.ws / "d").mkdir()
        (self.ws / "d" / "g").write_text("y")
        (self.ws / "link").symlink_to(outside)
        (self.rt / "submodule-ssh").mkdir()
        (self.rt / "submodule-ssh" / "submodule_a_deploy_key").write_text("KEY")
        (self.rt / "keep.txt").write_text("k")
        res = hooks.job_completed(self.env(), self.paths)
        self.assertEqual(list(self.ws.iterdir()), [])
        self.assertTrue((outside / "precious").exists())
        self.assertFalse((self.rt / "submodule-ssh" / "submodule_a_deploy_key").exists())
        self.assertTrue((self.rt / "keep.txt").exists())
        self.assertEqual(res["secret_files_removed"], 1)

    def test_completed_refuses_workspace_outside_or_unmarked(self):
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "f").write_text("x")
        with self.assertRaises(safety.SafetyError):
            hooks.job_completed(self.env(GITHUB_WORKSPACE=str(victim)), self.paths)
        (self.paths.work_dir / safety.MARKER).unlink()
        with self.assertRaises(safety.SafetyError):
            hooks.job_completed(self.env(), self.paths)
        self.assertTrue((victim / "f").exists())
