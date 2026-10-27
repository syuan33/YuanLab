import hashlib
import io
import json
import plistlib
import tarfile
import os

from repli_local_ci import launchd
from repli_local_ci.config import Paths
from repli_local_ci.runner_install import InstallError, Installer, Pin, download_verified, safe_extract
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
        self.paths = Paths(self.tmp / "home", launch_agents_dir=self.tmp / "LaunchAgents")
        self.tar = self.tmp / "r.tar.gz"
        make_tarball(self.tar, {"run.sh": b"#!/bin/sh\n", "config.sh": b"#!/bin/sh\n"})
        self.sha = hashlib.sha256(self.tar.read_bytes()).hexdigest()
        self.pin = Pin("9.9.9", "actions-runner-osx-arm64-9.9.9.tar.gz", "https://example.invalid/r.tar.gz", self.sha)
        self.fetch = lambda url, dest: dest.write_bytes(self.tar.read_bytes())
        self.rec = Recorder()

    def inst(self, pin=None, euid=1000):
        return Installer(self.paths, pin or self.pin, self.rec, self.fetch, euid=euid)


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
        for plan in (i.plan_install(), i.plan_register("syuan33/repli"), i.plan_uninstall()):
            self.assertTrue(plan)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(self.rec.calls, [])

    def test_install_apply_writes_runner_and_plist_only(self):
        self.inst().apply_install()
        self.assertTrue((self.paths.runner_dir / "run.sh").exists())
        self.assertTrue(self.paths.plist.exists())
        self.assertEqual(self.rec.calls, [])  # install never starts the service
        pl = plistlib.loads(self.paths.plist.read_bytes())
        self.assertNotIn("UserName", pl)  # LaunchAgent runs as the logged-in user
        self.assertEqual(pl["Label"], launchd.LABEL)

    def test_refuses_root_for_every_apply(self):
        i = self.inst(euid=0)
        for fn in (i.apply_install, lambda: i.apply_register("syuan33/repli", TOKEN, True), i.apply_uninstall, i.apply_start, i.apply_stop):
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
            i.apply_register("syuan33/repli", TOKEN, confirm_admin_approved=False)
        with self.assertRaises(InstallError):
            i.apply_register("syuan33/repli", "", confirm_admin_approved=True)
        with self.assertRaises(Exception):
            i.apply_register("other/repo", TOKEN, confirm_admin_approved=True)
        self.assertEqual(self.rec.calls, [])

    def test_token_only_in_env_never_argv_or_plan(self):
        i = self.inst()
        i.apply_register("syuan33/repli", TOKEN, confirm_admin_approved=True)
        argv, env, _ = self.rec.calls[0]
        self.assertNotIn(TOKEN, " ".join(argv))
        self.assertEqual(env["ACTIONS_RUNNER_INPUT_TOKEN"], TOKEN)
        self.assertIn("repli-local-ci", " ".join(argv))
        self.assertNotIn(TOKEN, " ".join(i.plan_register("syuan33/repli")))
        self.assertNotIn(TOKEN, json.dumps(os.listdir(self.paths.home)))


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

    def test_uninstall_refuses_unmarked_runner_dir(self):
        self.paths.runner_dir.mkdir(parents=True)
        (self.paths.runner_dir / "mine").write_text("x")
        with self.assertRaises(Exception):
            self.inst().apply_uninstall()
        self.assertTrue((self.paths.runner_dir / "mine").exists())
