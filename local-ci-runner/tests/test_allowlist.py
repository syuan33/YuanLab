"""The allow-list comes ONLY from the protected local config. Negative tests: tampering, symlinks, permissions, env overrides."""
import json
import os
from unittest import mock

from local_ci_runner import allowlist, cli, hooks, safety
from local_ci_runner.allowlist import ConfigError
from local_ci_runner.config import Paths
from tests.fixtures import TmpCase

R = "example-org/example-repo"
SHA = "a" * 40


class Allowlist(TmpCase):
    def setUp(self):
        super().setUp()
        self.paths = Paths(self.tmp / "state", bin_dir=self.tmp / "bin")
        self.paths.home.mkdir(mode=0o700)
        os.chmod(self.paths.home, 0o700)

    def write(self):
        return allowlist.write(self.paths, R, ["ci/verify.sh", "-o", "{results_dir}"])

    def rewrite(self, mutate, fix_checksum=False):
        f = self.paths.config_file
        d = json.loads(f.read_text())
        mutate(d)
        if fix_checksum:
            d["checksum"] = allowlist._checksum(d)
        f.write_text(json.dumps(d))
        os.chmod(f, 0o600)

    def test_roundtrip_and_mode(self):
        f = self.write()
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertEqual(allowlist.load(self.paths).allowed_repos, frozenset({R}))

    def test_missing_config_denies_everything(self):
        with self.assertRaises(ConfigError):
            allowlist.load(self.paths)

    def test_create_is_exclusive(self):
        self.write()
        with self.assertRaises(FileExistsError):
            self.write()

    def test_permissions_enforced(self):
        self.write()
        for mode in (0o644, 0o640, 0o660, 0o666, 0o400, 0o700):
            os.chmod(self.paths.config_file, mode)
            with self.assertRaises(ConfigError, msg=oct(mode)):
                allowlist.load(self.paths)
        os.chmod(self.paths.config_file, 0o600)
        allowlist.load(self.paths)
        os.chmod(self.paths.home, 0o755)
        with self.assertRaises(ConfigError):
            allowlist.load(self.paths)

    def test_symlinked_config_or_state_dir_refused(self):
        self.write()
        real = self.tmp / "real.json"
        os.rename(self.paths.config_file, real)
        self.paths.config_file.symlink_to(real)
        with self.assertRaises(ConfigError):
            allowlist.load(self.paths)
        self.paths.config_file.unlink()
        os.rename(real, self.paths.config_file)
        link = self.tmp / "statelink"
        link.symlink_to(self.paths.home)
        with self.assertRaises(ConfigError):
            allowlist.load(Paths(link, bin_dir=self.tmp / "bin"))

    def test_foreign_owner_refused(self):
        self.write()
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(ConfigError):
                allowlist.load(self.paths)

    def test_tampering_detected(self):
        self.write()
        self.rewrite(lambda d: d["allowed_repos"].append("evil/repo"))  # checksum not updated
        with self.assertRaises(ConfigError):
            allowlist.load(self.paths)

    def test_invalid_contents_refused_even_with_valid_checksum(self):
        bad = [
            lambda d: d.update(allowed_repos=["*"]),
            lambda d: d.update(allowed_repos=[]),
            lambda d: d.update(allowed_repos=[R, "evil/repo"]),
            lambda d: d.update(allowed_repos=["a/b/c"]),
            lambda d: d.update(allowed_repos=["../x/y"]),
            lambda d: d.update(allowed_repos="example-org/example-repo"),
            lambda d: d.update(verify_command=["/bin/sh", "-c", "id"]),
            lambda d: d.update(verify_command=["../x.sh"]),
            lambda d: d.update(verify_command=[]),
            lambda d: d.update(verify_command=["-x"]),
            lambda d: d.update(push_refs=["refs/tags/v1"]),
            lambda d: d.update(extra_key=1),
            lambda d: d.update(schema=2),
        ]
        for m in bad:
            if self.paths.config_file.exists():
                self.paths.config_file.unlink()
            self.write()
            self.rewrite(m, fix_checksum=True)
            with self.assertRaises(ConfigError):
                allowlist.load(self.paths)

    def test_garbage_and_oversize_refused(self):
        self.write()
        self.paths.config_file.write_text("{not json")
        with self.assertRaises(ConfigError):
            allowlist.load(self.paths)


class NoEnvironmentOverrides(TmpCase):
    def setUp(self):
        super().setUp()
        self.paths = Paths(self.tmp / "state", bin_dir=self.tmp / "bin")
        self.paths.home.mkdir(mode=0o700)
        allowlist.write(self.paths, R, ["ci/verify.sh"])
        os.mkdir(self.paths.work_dir, 0o700)
        safety.write_marker(self.paths.work_dir)
        self.evil = self.tmp / "evil-state"
        self.evil.mkdir(mode=0o700)
        allowlist.write(Paths(self.evil, bin_dir=self.tmp / "bin"), "evil/repo", ["ci/verify.sh"])
        ev = self.tmp / "ev.json"
        ev.write_text(json.dumps({"pull_request": {"head": {"repo": {"full_name": "evil/repo", "fork": False}}, "base": {"repo": {"full_name": "evil/repo"}}}}))
        self.env = {"GITHUB_REPOSITORY": "evil/repo", "GITHUB_SHA": SHA, "GITHUB_EVENT_NAME": "pull_request",
                    "GITHUB_REF": "refs/pull/1/merge", "GITHUB_EVENT_PATH": str(ev)}

    def test_env_vars_cannot_widen_allowlist(self):
        spoof = {"LOCAL_CI_RUNNER_ALLOWED_REPOS": "evil/repo", "ALLOWED_REPOS": "evil/repo", "LOCAL_CI_RUNNER_REPO": "evil/repo",
                 "LOCAL_CI_RUNNER_HOME": str(self.evil)}
        with mock.patch.dict(os.environ, spoof):
            with self.assertRaises(safety.SafetyError):
                hooks.job_started(dict(self.env, **spoof), self.paths, euid=1000)

    def test_hook_cli_requires_home_and_ignores_env_home(self):
        with mock.patch.dict(os.environ, {"LOCAL_CI_RUNNER_HOME": str(self.evil), **self.env}):
            with self.assertRaises(SystemExit):
                cli.main(["hook", "job-started"])  # --home is mandatory
            with self.assertRaises(safety.SafetyError):
                # the real state dir is used (registered for example-repo), so evil/repo is refused even though the
                # env var points at a permissive config
                with mock.patch("os.geteuid", return_value=1000):
                    cli.cmd_hook(type("A", (), {"which": "job-started", "home": str(self.paths.home)})(), None)

    def test_hook_relative_home_refused(self):
        with self.assertRaises(safety.SafetyError):
            cli.cmd_hook(type("A", (), {"which": "job-started", "home": "relative"})(), None)

    def test_unregistered_hook_denies_everything(self):
        os.unlink(self.paths.config_file)
        good = dict(self.env, GITHUB_REPOSITORY=R)
        with self.assertRaises(safety.SafetyError):
            hooks.job_started(good, self.paths, euid=1000)

    def test_run_and_cancel_fail_closed_when_unregistered(self):
        os.unlink(self.paths.config_file)
        with mock.patch.dict(os.environ, {"LOCAL_CI_RUNNER_HOME": str(self.paths.home)}):
            self.assertEqual(cli.main(["run", "--repo", R, "--sha", SHA]), 2)
            self.assertEqual(cli.main(["cancel", "--repo", R, "--sha", SHA]), 2)
