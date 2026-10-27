import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from local_ci_runner import safety
from local_ci_runner.config import Paths
from local_ci_runner.engine import Engine
from local_ci_runner.gitops import resolve_ref
from tests.fixtures import GIT_ENV, Remote, TmpCase, SYNTH_REPO, synth_config

FAKE_TOKEN = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"


class EngineBase(TmpCase):
    def engine(self, remote, **kw):
        self.paths = Paths(self.tmp / "home", launch_agents_dir=self.tmp / "LA")
        kw.setdefault("strict_identity", False)
        kw.setdefault("config", synth_config())
        kw.setdefault("euid", 1000)  # the CI sandbox itself may be root; root refusal is tested in test_safety
        return Engine(self.paths, remote_factory=lambda repo: remote.url, sleep=lambda s: time.sleep(0.01), **kw)


class Happy(EngineBase):
    def test_pass_report_fields(self):
        r = Remote(self.tmp)
        sha = resolve_ref(r.url, "main")
        base = "b" * 40
        rep = self.engine(r).run(SYNTH_REPO, sha, base_sha=base, ref="main")
        self.assertEqual(rep["status"], "passed")
        self.assertEqual(rep["exit_code"], 0)
        self.assertEqual(rep["repository"], SYNTH_REPO)
        self.assertEqual(rep["head_sha"], sha)
        self.assertEqual(rep["base_sha"], base)
        self.assertEqual(rep["command"][1], "--out")
        self.assertTrue(rep["command"][2].endswith("/results"))
        self.assertTrue(rep["command"][0].endswith("ci/verify.sh"))
        self.assertIn("started_at", rep)
        self.assertIn("ended_at", rep)
        self.assertEqual(len(rep["log_sha256"]), 64)
        self.assertIn("python3", rep["tool_versions"])
        self.assertTrue(rep["test_summary"]["lines"])
        self.assertIn("not your repository's required merge gate", rep["disclaimer"])
        d = Path(rep["report_dir"])
        self.assertEqual(json.loads((d / "report.json").read_text())["head_sha"], sha)
        self.assertIn(sha, (d / "summary.txt").read_text())
        self.assertFalse(Path(rep["workspace"]).exists())  # cleaned

    def test_verify_invoked_with_isolated_results_dir_and_cwd(self):
        r = Remote(self.tmp, 'echo "ARGS:$*"; echo "PWD:$PWD"; test -f ci/verify.sh')
        sha = resolve_ref(r.url, "main")
        rep = self.engine(r).run(SYNTH_REPO, sha, keep_workspace=True)
        log = Path(rep["log_path"]).read_text()
        ws = Path(rep["workspace"])
        self.assertIn(f"ARGS:--out {ws}/results", log)
        self.assertIn(f"PWD:{ws}/src", log)
        safety.safe_remove(ws, self.paths.runs)

    def test_failure_exit_code(self):
        r = Remote(self.tmp, "echo 'FAILED 2 tests'; exit 3")
        rep = self.engine(r).run(SYNTH_REPO, resolve_ref(r.url, "main"))
        self.assertEqual((rep["status"], rep["exit_code"]), ("failed", 3))

    def test_branch_moving_mid_run_does_not_change_target(self):
        r = Remote(self.tmp, "sleep 1; git rev-parse HEAD")
        sha = resolve_ref(r.url, "main")
        out = {}
        t = threading.Thread(target=lambda: out.update(self.engine(r).run(SYNTH_REPO, sha)))
        t.start()
        time.sleep(0.4)
        r.commit_verify("echo moved")
        t.join()
        self.assertEqual(out["head_sha"], sha)
        self.assertIn(sha, Path(out["log_path"]).read_text())

    def test_secrets_redacted_in_log_and_hash_matches(self):
        r = Remote(self.tmp, f'echo "token={FAKE_TOKEN}"; echo "GITHUB_TOKEN=abc"; echo {FAKE_TOKEN}')
        rep = self.engine(r).run(SYNTH_REPO, resolve_ref(r.url, "main"))
        log = Path(rep["log_path"]).read_bytes()
        self.assertNotIn(FAKE_TOKEN.encode(), log)
        self.assertNotIn(b"GITHUB_TOKEN=abc", log)
        import hashlib
        self.assertEqual(hashlib.sha256(log).hexdigest(), rep["log_sha256"])
        self.assertNotIn(FAKE_TOKEN, Path(rep["report_dir"], "report.json").read_text())

    def test_parent_secrets_not_in_child_env(self):
        r = Remote(self.tmp, 'echo "GH=${GITHUB_TOKEN:-unset} SSH=${SSH_AUTH_SOCK:-unset} CI=$CI"')
        os.environ["GITHUB_TOKEN"] = "sentinel"
        self.addCleanup(os.environ.pop, "GITHUB_TOKEN", None)
        rep = self.engine(r).run(SYNTH_REPO, resolve_ref(r.url, "main"))
        self.assertIn("GH=unset SSH=unset CI=true", Path(rep["log_path"]).read_text())


class Refusals(EngineBase):
    def test_disallowed_repo_and_fork_and_bad_sha(self):
        r = Remote(self.tmp)
        e = self.engine(r)
        sha = resolve_ref(r.url, "main")
        for kw in (dict(repo="example-org/other", sha=sha), dict(repo=SYNTH_REPO, sha="main"),
                   dict(repo=SYNTH_REPO, sha=sha, head_repo="mallory/example-repo")):
            with self.assertRaises(safety.SafetyError):
                e.run(**kw)
        self.assertFalse(self.paths.runs.exists() and any(self.paths.runs.iterdir()))

    def test_sha_not_in_remote_is_error_and_cleaned(self):
        r = Remote(self.tmp)
        rep = self.engine(r).run(SYNTH_REPO, "1" * 40)
        self.assertEqual(rep["status"], "error")
        self.assertEqual(list(self.paths.runs.iterdir()), [])

    def test_strict_identity_blocks_non_github_remote(self):
        r = Remote(self.tmp)
        rep = self.engine(r, strict_identity=True).run(SYNTH_REPO, resolve_ref(r.url, "main"))
        self.assertEqual(rep["status"], "error")
        self.assertIn("identity", rep["error"])

    def test_missing_verify_script_fails_closed(self):
        r = Remote(self.tmp)
        (r.work / "ci" / "verify.sh").unlink()
        from tests.fixtures import git
        git("add", "-A", cwd=r.work); git("commit", "-m", "rm", cwd=r.work); git("push", "origin", "HEAD:refs/heads/main", cwd=r.work)
        rep = self.engine(r).run(SYNTH_REPO, resolve_ref(r.url, "main"))
        self.assertEqual(rep["status"], "error")

    def test_submodule_not_allowlisted_refused(self):
        r = Remote(self.tmp)
        (r.work / ".gitmodules").write_text('[submodule "m"]\n\tpath = m\n\turl = git@github.com:example-org/priv.git\n')
        from tests.fixtures import git
        git("add", "-A", cwd=r.work); git("commit", "-m", "sm", cwd=r.work); git("push", "origin", "HEAD:refs/heads/main", cwd=r.work)
        rep = self.engine(r).run(SYNTH_REPO, resolve_ref(r.url, "main"))
        self.assertEqual(rep["status"], "error")
        self.assertIn("submodule", rep["error"])


class Lifecycle(EngineBase):
    def test_timeout_kills_process_group(self):
        r = Remote(self.tmp, "sleep 300 & wait")
        t0 = time.time()
        rep = self.engine(r).run(SYNTH_REPO, resolve_ref(r.url, "main"), timeout=1)
        self.assertEqual(rep["status"], "timeout")
        self.assertLess(time.time() - t0, 30)

    def test_cancel_kills_only_own_group(self):
        bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (bystander.kill(), bystander.wait()))
        r = Remote(self.tmp, "sleep 120")
        ev = threading.Event()
        threading.Timer(0.8, ev.set).start()
        rep = self.engine(r).run(SYNTH_REPO, resolve_ref(r.url, "main"), cancel_event=ev)
        self.assertEqual(rep["status"], "cancelled")
        self.assertIsNone(bystander.poll())  # unrelated process untouched

    def test_superseded_while_waiting(self):
        r = Remote(self.tmp)
        e = self.engine(r)
        sha1 = resolve_ref(r.url, "main")
        sha2 = r.commit_verify("echo new")
        # occupy the single slot with a live job
        j0, _ = e.queue.enqueue(SYNTH_REPO, "9" * 40, "other")
        e.queue.claim(j0["id"], pid=os.getpid())
        out = {}
        t = threading.Thread(target=lambda: out.update(e.run(SYNTH_REPO, sha1, ref="main", wait_seconds=20)))
        t.start()
        time.sleep(0.5)
        e.queue.enqueue(SYNTH_REPO, sha2, "main")  # newer head supersedes the waiting one
        t.join(10)
        self.assertEqual(out["status"], "superseded")

    def test_wait_timeout_when_slot_busy(self):
        r = Remote(self.tmp)
        e = self.engine(r)
        j0, _ = e.queue.enqueue(SYNTH_REPO, "9" * 40, "other")
        e.queue.claim(j0["id"], pid=os.getpid())
        rep = e.run(SYNTH_REPO, resolve_ref(r.url, "main"), wait_seconds=0)
        self.assertEqual(rep["status"], "busy")


class Reaping(EngineBase):
    def test_exception_in_cancel_check_still_kills_and_reaps(self):
        from local_ci_runner import verify
        r = Remote(self.tmp, f'echo $$ > {self.tmp}/pid; sleep 60')
        src = self.tmp / "src"
        src.mkdir()
        from local_ci_runner import gitops
        gitops.fetch_exact(src, r.url, resolve_ref(r.url, "main"))
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            if (self.tmp / "pid").exists():
                raise RuntimeError("boom")
            return False
        with self.assertRaises(RuntimeError):
            verify.run_verify(src, self.tmp / "res", self.tmp / "log", 60, boom, self.tmp)
        pid = int((self.tmp / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
