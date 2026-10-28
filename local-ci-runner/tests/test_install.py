import hashlib
import io
import json
import plistlib
import tarfile
import os

from local_ci_runner import launchd, safety
from local_ci_runner.config import Paths
from local_ci_runner.runner_install import (InstallError, Installer, Pin, download_verified, installed_version, safe_extract,
                                              verify_installed_version)
from tests.fixtures import TmpCase

TOKEN = "AFAKEREGISTRATIONTOKEN0000000000"  # synthetic


def make_tarball(path, members):
    with tarfile.open(path, "w:gz") as t:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = 0o755
            t.addfile(ti, io.BytesIO(data))


def snapshot(root):
    return sorted(str(p) for p in root.rglob("*"))


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, env=None, cwd=None):
        self.calls.append((list(argv), dict(env or {}), cwd))
        return 0


class Base(TmpCase):
    def setUp(self):
        super().setUp()
        self.paths = Paths(self.tmp / "home", launch_agents_dir=self.tmp / "LaunchAgents", bin_dir=self.tmp / "bin")
        self.tar = self.tmp / "r.tar.gz"
        make_tarball(self.tar, {"run.sh": b"#!/bin/sh\n", "config.sh": b"#!/bin/sh\n"})
        self.sha = hashlib.sha256(self.tar.read_bytes()).hexdigest()
        self.pin = Pin("9.9.9", "actions-runner-osx-arm64-9.9.9.tar.gz", "https://example.invalid/r.tar.gz", self.sha)
        self.fetch = lambda url, dest: dest.write_bytes(self.tar.read_bytes())
        self.rec = Recorder()

    def inst(self, pin=None, euid=1000, gate=None, probe=None):
        return Installer(self.paths, pin or self.pin, self.rec, self.fetch, euid=euid, gate=gate,
                         version_probe=probe or (lambda d: (pin or self.pin).version))


class Download(Base):
    def test_checksum_match(self):
        dest = self.tmp / "d.tgz"
        download_verified(self.pin.url, dest, self.sha, self.fetch)
        self.assertTrue(dest.exists())

    def test_checksum_mismatch_deletes_and_raises(self):
        dest = self.tmp / "d.tgz"
        with self.assertRaises(InstallError):
            download_verified(self.pin.url, dest, "0" * 64, self.fetch)
        self.assertFalse(dest.exists())

    def test_unpinned_refused(self):
        for bad in (None, "", "TODO", "0" * 63):
            with self.assertRaises(InstallError):
                self.inst(Pin("1", "a", "u", bad)).apply_install()

    def test_tar_traversal_and_links_refused(self):
        bad = self.tmp / "bad.tgz"
        make_tarball(bad, {"../evil": b"x"})
        with self.assertRaises(InstallError):
            safe_extract(bad, self.tmp / "out")
        self.assertFalse((self.tmp / "evil").exists())
        with tarfile.open(bad, "w:gz") as t:
            ti = tarfile.TarInfo("l")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            t.addfile(ti)
        with self.assertRaises(InstallError):
            safe_extract(bad, self.tmp / "out2")


class Plans(Base):
    def test_dry_run_has_no_side_effects(self):
        before = snapshot(self.tmp)
        i = self.inst()
        for plan in (i.plan_install(), i.plan_register("example-org/example-repo"), i.plan_uninstall()):
            self.assertTrue(plan)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(self.rec.calls, [])

    def test_install_apply_writes_runner_and_plist_only(self):
        self.inst().apply_install()
        self.assertTrue((self.paths.runner_dir / "run.sh").exists())
        self.assertTrue(self.paths.plist.exists())
        self.assertEqual(self.rec.calls, [])  # install never starts the service
        self.assertTrue(self.paths.launcher.exists())
        self.assertTrue(safety.is_owned(self.paths.work_dir, self.paths.home))
        env = (self.paths.runner_dir / ".env").read_text()
        self.assertIn("ACTIONS_RUNNER_HOOK_JOB_STARTED=" + str(self.paths.hooks_dir / "job-started.sh"), env)
        self.assertIn(str(self.paths.bin_dir), env)
        pl = plistlib.loads(self.paths.plist.read_bytes())
        self.assertNotIn("UserName", pl)  # LaunchAgent runs as the logged-in user
        self.assertEqual(pl["Label"], launchd.LABEL)

    def test_refuses_root_for_every_apply(self):
        i = self.inst(euid=0)
        for fn in (i.apply_install, lambda: i.apply_register("example-org/example-repo", TOKEN, True), i.apply_uninstall, i.apply_start, i.apply_stop):
            with self.assertRaises(Exception):
                fn()

    def test_plist_is_user_launchagent(self):
        pl = launchd.render_plist(self.paths)
        self.assertTrue(pl["ProgramArguments"][0].endswith("run.sh"))
        self.assertEqual(launchd.bootout_cmd(501), ["launchctl", "bootout", f"gui/501/{launchd.LABEL}"])


class Register(Base):
    def setUp(self):
        super().setUp()
        self.inst().apply_install()

    def test_requires_confirmation_and_token(self):
        i = self.inst()
        with self.assertRaises(InstallError):
            i.apply_register("example-org/example-repo", TOKEN, confirm_admin_approved=False)
        with self.assertRaises(InstallError):
            i.apply_register("example-org/example-repo", "", confirm_admin_approved=True)
        with self.assertRaises(Exception):
            i.apply_register("not a repo", TOKEN, confirm_admin_approved=True)
        self.assertEqual(self.rec.calls, [])

    def test_token_only_in_env_never_argv_or_plan(self):
        i = self.inst()
        i.apply_register("example-org/example-repo", TOKEN, confirm_admin_approved=True)
        argv, env, _ = self.rec.calls[0]
        self.assertNotIn(TOKEN, " ".join(argv))
        self.assertEqual(env["ACTIONS_RUNNER_INPUT_TOKEN"], TOKEN)
        self.assertIn("local-ci-runner", " ".join(argv))
        self.assertNotIn(TOKEN, " ".join(i.plan_register("example-org/example-repo")))
        self.assertNotIn(TOKEN, json.dumps(os.listdir(self.paths.home)))
        self.assertNotIn(TOKEN, self.paths.config_file.read_text())

    def test_registration_writes_protected_allowlist(self):
        from local_ci_runner import allowlist
        self.inst().apply_register("example-org/example-repo", TOKEN, confirm_admin_approved=True)
        self.assertEqual(self.paths.config_file.stat().st_mode & 0o777, 0o600)
        cfg = allowlist.load(self.paths)
        self.assertEqual(cfg.allowed_repos, frozenset({"example-org/example-repo"}))

    def test_dry_run_and_refusals_write_no_allowlist(self):
        i = self.inst()
        i.plan_register("example-org/example-repo")
        with self.assertRaises(InstallError):
            i.apply_register("example-org/example-repo", TOKEN, confirm_admin_approved=False)
        self.assertFalse(self.paths.config_file.exists())

    def test_existing_allowlist_is_never_overwritten(self):
        self.paths.config_file.write_text("{}")
        with self.assertRaises(InstallError):
            self.inst().apply_register("example-org/example-repo", TOKEN, confirm_admin_approved=True)
        self.assertEqual(self.paths.config_file.read_text(), "{}")
        self.assertEqual(self.rec.calls, [])

    def test_failed_config_sh_removes_the_allowlist_it_created(self):
        class Fail(Recorder):
            def __call__(self, argv, env=None, cwd=None):
                super().__call__(argv, env, cwd)
                return 1
        i = Installer(self.paths, self.pin, Fail(), self.fetch, euid=1000, version_probe=lambda d: self.pin.version)
        with self.assertRaises(InstallError):
            i.apply_register("example-org/example-repo", TOKEN, confirm_admin_approved=True)
        self.assertFalse(self.paths.config_file.exists())


class StartStopUninstall(Base):
    def test_start_requires_registration(self):
        i = self.inst()
        i.apply_install()
        with self.assertRaises(InstallError):
            i.apply_start()
        (self.paths.runner_dir / ".runner").write_text("{}")
        i.apply_start()
        self.assertEqual(self.rec.calls[-1][0][:2], ["launchctl", "bootstrap"])

    def test_stop_only_boots_out_own_label(self):
        i = self.inst()
        i.apply_install()
        i.apply_stop()
        argv = self.rec.calls[-1][0]
        self.assertEqual(argv[:2], ["launchctl", "bootout"])
        self.assertTrue(argv[2].endswith("/" + launchd.LABEL))
        self.assertNotIn("kill", " ".join(argv))

    def test_uninstall_removes_only_owned(self):
        i = self.inst()
        i.apply_install()
        other = self.paths.launch_agents / "com.someone.else.plist"
        other.write_bytes(b"x")
        stranger = self.paths.home / "keepme"
        stranger.write_text("x")
        i.apply_uninstall()
        self.assertFalse(self.paths.plist.exists())
        self.assertFalse(self.paths.runner_dir.exists())
        self.assertTrue(other.exists() and stranger.exists())
        self.assertFalse(self.paths.launcher.exists() or self.paths.app_dir.exists() or self.paths.work_dir.exists())
        self.assertFalse(self.paths.config_file.exists())

    def test_uninstall_leaves_modified_launcher(self):
        i = self.inst()
        i.apply_install()
        self.paths.launcher.write_text("#!/bin/sh\necho mine\n")
        notes = i.apply_uninstall()
        self.assertTrue(self.paths.launcher.exists())
        self.assertTrue(any("left" in n for n in notes))
        self.assertFalse(self.paths.app_dir.exists())

    def test_install_refuses_to_overwrite_foreign_launcher(self):
        self.paths.bin_dir.mkdir()
        self.paths.launcher.write_text("foreign")
        with self.assertRaises(Exception):
            self.inst().apply_install()
        self.assertEqual(self.paths.launcher.read_text(), "foreign")

    def test_uninstall_refuses_unmarked_work_or_app_dir(self):
        i = self.inst()
        i.apply_install()
        (self.paths.work_dir / safety.MARKER).unlink()
        with self.assertRaises(Exception):
            i.apply_uninstall()
        self.assertTrue(self.paths.runner_dir.exists() and self.paths.launcher.exists())

    def test_uninstall_refuses_unmarked_runner_dir(self):
        self.paths.runner_dir.mkdir(parents=True)
        (self.paths.runner_dir / "mine").write_text("x")
        with self.assertRaises(Exception):
            self.inst().apply_uninstall()
        self.assertTrue((self.paths.runner_dir / "mine").exists())


class Gate(Base):
    def setUp(self):
        super().setUp()
        self.inst().apply_install()
        (self.paths.runner_dir / ".runner").write_text("{}")

    def failing_gate(self):
        raise InstallError("not ready - doctor failures: dedicated user")

    def test_gate_blocks_register_and_start_without_side_effects(self):
        (self.paths.runner_dir / ".runner").unlink()
        i = self.inst(gate=self.failing_gate)
        with self.assertRaises(InstallError):
            i.apply_register("example-org/example-repo", TOKEN, True)
        (self.paths.runner_dir / ".runner").write_text("{}")
        with self.assertRaises(InstallError):
            i.apply_start()
        self.assertEqual(self.rec.calls, [])

    def test_register_uses_managed_work_dir_and_path(self):
        (self.paths.runner_dir / ".runner").unlink()
        self.inst(gate=lambda: None).apply_register("example-org/example-repo", TOKEN, True)
        argv, env, _ = self.rec.calls[0]
        self.assertEqual(argv[argv.index("--work") + 1], str(self.paths.work_dir))
        self.assertTrue(env["PATH"].startswith(str(self.paths.bin_dir)))

    def test_shipped_pin_is_populated_and_official(self):
        from pathlib import Path
        pin = Pin.load(Path(__file__).resolve().parents[1] / "runner-pin.json")
        self.assertTrue(pin.pinned)
        self.assertEqual((pin.version, pin.sha256), ("2.338.2", "25a065af409ffa317ab4d5a3b614dd15fc9c40f2172bc1b35a1b1258e919a388"))
        self.assertEqual(pin.url, "https://github.com/actions/runner/releases/download/v2.338.2/actions-runner-osx-arm64-2.338.2.tar.gz")


class DisableUpdate(Base):
    """The pinned, checksum-verified runner must never auto-update: registration always passes --disableupdate."""

    def setUp(self):
        super().setUp()
        self.inst().apply_install()

    def expected_argv(self, i):
        return [str(self.paths.runner_dir / "config.sh"), "--unattended", "--url", "https://github.com/example-org/example-repo",
                "--labels", "local-ci-runner", "--name", i._runner_name(), "--work", str(self.paths.work_dir), "--disableupdate"]

    def test_exact_registration_argv_includes_disableupdate_and_never_the_token(self):
        i = self.inst()
        i.apply_register("example-org/example-repo", TOKEN, confirm_admin_approved=True)
        argv, env, cwd = self.rec.calls[0]
        self.assertEqual(argv, self.expected_argv(i))
        self.assertIn("--disableupdate", argv)
        self.assertNotIn(TOKEN, " ".join(argv))
        self.assertEqual(env["ACTIONS_RUNNER_INPUT_TOKEN"], TOKEN)  # token only via the child's environment
        self.assertNotIn("--token", argv)

    def test_displayed_plan_is_the_same_command(self):
        i = self.inst()
        plan = i.plan_register("example-org/example-repo")
        self.assertIn("run " + " ".join(self.expected_argv(i)), plan)
        self.assertTrue(any("--disableupdate" in line for line in plan))
        self.assertNotIn(TOKEN, " ".join(plan))


def make_fake_runner(runner_dir, body, mode=0o755):
    exe = runner_dir / "bin" / "Runner.Listener"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n" + body + "\n")
    exe.chmod(mode)
    return exe


class InstalledVersion(Base):
    """Uses a real (synthetic) executable standing in for the official `bin/Runner.Listener --version`; no network."""

    def probe(self, body, mode=0o755):
        make_fake_runner(self.paths.runner_dir, body, mode)
        return installed_version(self.paths.runner_dir)

    def test_match(self):
        self.paths.runner_dir.mkdir(parents=True)
        self.assertEqual(self.probe('[ "$1" = "--version" ] && echo 9.9.9'), "9.9.9")
        self.assertEqual(verify_installed_version(self.paths.runner_dir, self.pin), "9.9.9")

    def test_mismatch(self):
        self.paths.runner_dir.mkdir(parents=True)
        make_fake_runner(self.paths.runner_dir, "echo 9.9.10")
        with self.assertRaises(InstallError) as c:
            verify_installed_version(self.paths.runner_dir, self.pin)
        self.assertIn("!=", str(c.exception))

    def test_missing_or_unusable_executable(self):
        self.paths.runner_dir.mkdir(parents=True)
        with self.assertRaises(InstallError):
            installed_version(self.paths.runner_dir)  # no bin/Runner.Listener
        exe = make_fake_runner(self.paths.runner_dir, "echo 9.9.9", mode=0o644)
        with self.assertRaises(InstallError):
            installed_version(self.paths.runner_dir)  # not executable
        exe.unlink()
        real = self.tmp / "real"
        real.write_text("#!/bin/sh\necho 9.9.9\n")
        real.chmod(0o755)
        exe.symlink_to(real)
        with self.assertRaises(InstallError):
            installed_version(self.paths.runner_dir)  # symlink

    def test_malformed_output(self):
        self.paths.runner_dir.mkdir(parents=True)
        for body in ["", "echo", "echo garbage", "echo 9.9", "echo v9.9.9", "echo 9.9.9-rc1", "echo '9.9.9 extra'", "echo 9.9.9; echo 9.9.9",
                     "echo 9.9.9; exit 1", "exit 3", "echo ' ' "]:
            make_fake_runner(self.paths.runner_dir, body)
            with self.assertRaises(InstallError, msg=body):
                installed_version(self.paths.runner_dir)


class ScriptedExec:
    """Executor returning scripted exit codes keyed by the launchctl sub-command."""

    def __init__(self, rcs):
        self.rcs, self.calls = rcs, []

    def __call__(self, argv, env=None, cwd=None):
        self.calls.append(list(argv))
        return self.rcs.get(argv[1], 0) if argv[0] == "launchctl" else 0


class StartGuiSession(Base):
    """LaunchAgents load into gui/<uid>: start must fail clearly when the dedicated user has no GUI login session."""

    def setUp(self):
        super().setUp()
        self.inst().apply_install()
        (self.paths.runner_dir / ".runner").write_text("{}")

    def inst_with(self, exe, **kw):
        return Installer(self.paths, self.pin, exe, self.fetch, euid=1000, uid=501, version_probe=kw.pop("probe", lambda d: self.pin.version), **kw)

    def test_no_gui_domain_is_diagnosed_before_bootstrap(self):
        exe = ScriptedExec({"print": 113})
        with self.assertRaises(InstallError) as c:
            self.inst_with(exe).apply_start()
        msg = str(c.exception)
        self.assertIn("logged in", msg)
        self.assertIn("gui/501", msg)
        self.assertNotIn("bootstrap", [a[1] for a in exe.calls])

    def test_bootstrap_failure_names_the_login_requirement(self):
        exe = ScriptedExec({"bootstrap": 5})
        with self.assertRaises(InstallError) as c:
            self.inst_with(exe).apply_start()
        self.assertIn("logged in", str(c.exception))
        self.assertIn("exit 5", str(c.exception))
        self.assertEqual([a[1] for a in exe.calls], ["print", "bootstrap"])

    def test_success_path_and_plan(self):
        exe = ScriptedExec({})
        self.inst_with(exe).apply_start()
        self.assertEqual([a[1] for a in exe.calls], ["print", "bootstrap"])
        plan = " ".join(self.inst_with(exe).plan_start())
        self.assertIn("gui/501", plan)
        self.assertIn("GUI login session", plan)

    def test_other_failures_are_not_weakened_or_reworded(self):
        exe = ScriptedExec({"print": 113, "bootstrap": 5})  # session problems present too
        # not registered
        (self.paths.runner_dir / ".runner").unlink()
        with self.assertRaises(InstallError) as c:
            self.inst_with(exe).apply_start()
        self.assertIn("not registered", str(c.exception))
        (self.paths.runner_dir / ".runner").write_text("{}")
        # version mismatch wins over (and precedes) any launchctl call
        with self.assertRaises(InstallError) as c:
            self.inst_with(exe, probe=lambda d: "1.0.0").apply_start()
        self.assertIn("!=", str(c.exception))
        # failing readiness gate
        def gate():
            raise InstallError("not ready - doctor failures: dedicated user")
        with self.assertRaises(InstallError) as c:
            self.inst_with(exe, gate=gate).apply_start()
        self.assertIn("doctor failures", str(c.exception))
        # not installed
        self.paths.plist.unlink()
        with self.assertRaises(InstallError) as c:
            self.inst_with(exe).apply_start()
        self.assertIn("not installed", str(c.exception))
        self.assertEqual(exe.calls, [])  # none of those reached launchctl


class StartRegisterVersionGate(Base):
    def setUp(self):
        super().setUp()
        self.inst().apply_install()
        (self.paths.runner_dir / ".runner").write_text("{}")

    def test_start_refuses_on_mismatch_missing_or_malformed(self):
        for probe in (lambda d: "9.9.10", lambda d: (_ for _ in ()).throw(InstallError("missing")),
                      lambda d: (_ for _ in ()).throw(InstallError("malformed"))):
            with self.assertRaises(InstallError):
                self.inst(probe=probe).apply_start()
        self.assertEqual(self.rec.calls, [])  # launchctl never called
        self.inst().apply_start()
        self.assertEqual(self.rec.calls[-1][0][:2], ["launchctl", "bootstrap"])

    def test_register_refuses_when_installed_runner_is_not_the_pinned_one(self):
        (self.paths.runner_dir / ".runner").unlink()
        with self.assertRaises(InstallError):
            self.inst(probe=lambda d: "1.0.0").apply_register("example-org/example-repo", TOKEN, confirm_admin_approved=True)
        self.assertEqual(self.rec.calls, [])
        self.assertFalse(self.paths.config_file.exists())
