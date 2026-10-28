"""The runner pin is part of the immutable managed application payload. After install the source checkout is not needed, and
nothing falls back to it or to any ambient path."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from unittest import mock

from local_ci_runner import cli, safety, selfinstall
from local_ci_runner.config import Paths
from local_ci_runner.runner_install import InstallError, Installer, Pin
from tests.fixtures import TmpCase

ROOT = Path(__file__).resolve().parents[1]
VERSION = "9.9.9"  # deliberately different from the repository's real pin so any fallback to the source pin is visible
ASSET = f"actions-runner-osx-arm64-{VERSION}.tar.gz"
URL = f"https://github.com/actions/runner/releases/download/v{VERSION}/{ASSET}"


def make_runner_tarball(path, version=VERSION):
    with tarfile.open(path, "w:gz") as t:
        for name, data in {"run.sh": b"#!/bin/sh\n", "config.sh": b"#!/bin/sh\n",
                           "bin/Runner.Listener": f"#!/bin/sh\n[ \"$1\" = \"--version\" ] && echo {version}\n".encode()}.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = 0o755
            t.addfile(ti, io.BytesIO(data))


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, env=None, cwd=None):
        self.calls.append(list(argv))
        return 0


class Managed(TmpCase):
    """Install into a temp state dir from a throw-away 'checkout', then delete that checkout."""

    def setUp(self):
        super().setUp()
        self.paths = Paths(self.tmp / "state", launch_agents_dir=self.tmp / "LA", bin_dir=self.tmp / ".local" / "bin")
        self.checkout = self.tmp / "checkout"
        self.checkout.mkdir()
        tgz = self.tmp / "r.tgz"
        make_runner_tarball(tgz)
        self.sha = hashlib.sha256(tgz.read_bytes()).hexdigest()
        self.pin = Pin(VERSION, ASSET, URL, self.sha)
        self.pin_src = self.checkout / "runner-pin.json"
        self.pin_src.write_text(json.dumps({"version": VERSION, "asset": ASSET, "url": URL, "sha256": self.sha}, indent=2))
        self.rec = Recorder()
        self.fetch = lambda url, dest: dest.write_bytes(tgz.read_bytes())

    def installer(self, **kw):
        kw.setdefault("pin_file", self.pin_src)
        return Installer(self.paths, self.pin, self.rec, self.fetch, euid=1000, **kw)

    def install_and_remove_checkout(self):
        self.installer().apply_install()
        shutil.rmtree(self.checkout)
        self.assertFalse(self.pin_src.exists())

    def run_managed(self, *args):
        env = {"PATH": f"{self.paths.bin_dir}:{os.environ['PATH']}", "HOME": str(self.tmp), "LOCAL_CI_RUNNER_HOME": str(self.paths.home)}
        return subprocess.run([str(self.paths.launcher), *args], cwd=self.tmp, env=env, capture_output=True, text=True, timeout=120)

    def reinstall_fresh(self):
        shutil.rmtree(self.paths.home, ignore_errors=True)
        shutil.rmtree(self.paths.bin_dir, ignore_errors=True)
        self.checkout.mkdir(exist_ok=True)
        self.pin_src.write_text(json.dumps({"version": VERSION, "asset": ASSET, "url": URL, "sha256": self.sha}, indent=2))
        self.install_and_remove_checkout()


class PinPackaging(Managed):
    def test_pin_is_installed_with_manifest_sha_and_is_read_only(self):
        raw = self.pin_src.read_bytes()
        self.installer().apply_install()
        self.assertEqual(self.paths.pin_file.read_bytes(), raw)  # the exact reviewed bytes
        self.assertEqual(self.paths.pin_file, self.paths.app_dir / "runner-pin.json")  # where the installed CLI looks
        self.assertEqual(os.stat(self.paths.pin_file).st_mode & 0o777, 0o444)
        m = json.loads(self.paths.manifest.read_text())
        self.assertEqual(m["pin_file"], str(self.paths.pin_file))
        self.assertEqual(m["pin_sha256"], hashlib.sha256(raw).hexdigest())
        selfinstall.verify_installed_pin(self.paths, self.paths.pin_file)

    def test_plan_mentions_the_pin_payload(self):
        self.assertTrue(any("runner-pin.json" in l and str(self.paths.pin_file) in l for l in self.installer().plan_install()))

    def test_install_refuses_bad_source_pins_before_any_side_effect(self):
        good = json.loads(self.pin_src.read_text())
        real = self.tmp / "real-pin.json"
        real.write_text(self.pin_src.read_text())
        link = self.tmp / "link-pin.json"
        link.symlink_to(real)
        variants = {
            "missing": self.tmp / "nope.json",
            "symlink": link,
        }
        for name, content in {"not json": "{oops", "list": "[]", "empty": "", "non-official url": json.dumps(dict(good, url="https://example.invalid/x.tgz")),
                              "sha differs": json.dumps(dict(good, sha256="0" * 64)), "version differs": json.dumps(dict(good, version="9.9.8")),
                              "asset differs": json.dumps(dict(good, asset="other.tgz"))}.items():
            f = self.tmp / f"{name.replace(' ', '_')}.json"
            f.write_text(content)
            variants[name] = f
        for name, path in variants.items():
            with self.assertRaises(InstallError, msg=name):
                self.installer(pin_file=path).apply_install()
            self.assertFalse(self.paths.home.exists(), name)  # nothing created
            self.assertFalse(self.paths.app_dir.exists(), name)
        self.assertEqual(self.rec.calls, [])
        with self.assertRaises(InstallError):
            self.installer(pin_file=None).apply_install()

    def test_uninstall_removes_pin_only_with_the_ownership_checked_app(self):
        i = self.installer()
        i.apply_install()
        (self.paths.app_dir / safety.MARKER).unlink()  # lost ownership marker => refuse, pin stays
        with self.assertRaises(safety.SafetyError):
            i.apply_uninstall()
        self.assertTrue(self.paths.pin_file.exists())
        safety.write_marker(self.paths.app_dir)
        i.apply_uninstall()
        self.assertFalse(self.paths.pin_file.exists() or self.paths.app_dir.exists() or self.paths.manifest.exists())


class ManagedLauncher(Managed):
    def test_managed_doctor_loads_installed_pin_and_compares_installed_runner_version(self):
        self.install_and_remove_checkout()  # source checkout (and its pin) are gone
        r = self.run_managed("doctor")
        self.assertIn(f"[ok  ] runner pin: v{VERSION} sha256 populated", r.stdout)  # the installed pin, NOT the repository's real pin
        self.assertIn(f"[ok  ] runner version: {VERSION} == pinned", r.stdout)
        self.assertIn("[ok  ] manager executable", r.stdout)
        self.assertNotIn("[FAIL] runner pin", r.stdout)
        self.assertNotIn("2.338", r.stdout)

    def test_previous_failure_mode_is_now_a_clean_fail_closed_error(self):
        """First real install: the installed CLI looked for <home>/app/runner-pin.json, which did not exist."""
        self.install_and_remove_checkout()
        self.paths.pin_file.unlink()
        r = self.run_managed("doctor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("[FAIL] runner pin", r.stdout)
        self.assertIn("runner-pin.json", r.stdout)

    def test_managed_dry_runs_use_the_installed_pin(self):
        self.install_and_remove_checkout()
        r = self.run_managed("install", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"verify SHA-256 == {self.sha}", r.stdout)
        for args in (["register", "--repo", "example-org/example-repo", "--dry-run"], ["start", "--dry-run"], ["uninstall", "--dry-run"]):
            r = self.run_managed(*args)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertIn("DRY RUN", r.stdout)

    def test_in_process_commands_all_resolve_the_same_installed_pin(self):
        self.install_and_remove_checkout()
        seen = []

        class Spy(Installer):
            def __init__(self, paths, pin, *a, **kw):
                seen.append((pin.version, pin.sha256, kw.get("pin_file")))
                super().__init__(paths, pin, *a, **kw)

        mod = str(self.paths.app_dir / "pkg" / "local_ci_runner" / "cli.py")
        checked = []
        with mock.patch.dict(os.environ, {"LOCAL_CI_RUNNER_HOME": str(self.paths.home)}), mock.patch.object(cli, "MODULE_FILE", mod), \
                contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "Installer", Spy), \
                mock.patch.object(cli.pincheck, "check_pin", side_effect=lambda pin: checked.append((pin.version, pin.sha256)) or ([], [])):
            for args in (["install", "--dry-run"], ["register", "--repo", "example-org/example-repo", "--dry-run"], ["start", "--dry-run"],
                         ["stop", "--dry-run"], ["uninstall", "--dry-run"]):
                self.assertEqual(cli.main(args), 0, args)
            self.assertEqual(cli.main(["verify-pin"]), 0)
            gate_err = None
            try:
                cli._gate(self.paths)()
            except InstallError as e:
                gate_err = str(e)
        self.assertEqual(set(seen), {(VERSION, self.sha, self.paths.pin_file)})
        self.assertEqual(checked, [(VERSION, self.sha)])
        self.assertNotIn("runner pin", gate_err or "")  # other doctor failures (host) may exist; the pin must not be one of them

    def test_resolve_never_falls_back_to_the_source_pin_in_managed_mode(self):
        self.install_and_remove_checkout()
        mod = self.paths.app_dir / "pkg" / "local_ci_runner" / "cli.py"
        ambient = ROOT / "runner-pin.json"
        self.assertTrue(ambient.exists())
        self.paths.pin_file.unlink()
        pin, err = selfinstall.resolve_pin(self.paths, mod, ambient)
        self.assertEqual(pin, self.paths.pin_file)  # still the installed location ...
        self.assertTrue(err)  # ... and an error, not the ambient file
        # source-checkout layout keeps using its own pin
        src_mod = ROOT / "src" / "local_ci_runner" / "cli.py"
        self.assertEqual(selfinstall.resolve_pin(self.paths, src_mod, ambient), (ambient, None))


class TamperedInstalledPin(Managed):
    def tamper_missing(self):
        self.paths.pin_file.unlink()

    def tamper_symlink(self):
        other = self.tmp / "other-valid-pin.json"
        other.write_bytes(self.paths.pin_file.read_bytes())
        self.paths.pin_file.unlink()
        self.paths.pin_file.symlink_to(other)  # same bytes, still refused

    def tamper_modified(self):
        d = json.loads(self.paths.pin_file.read_text())
        d["sha256"] = "f" * 64  # a different but perfectly valid pin
        os.chmod(self.paths.pin_file, 0o644)
        self.paths.pin_file.write_text(json.dumps(d, indent=2))

    def tamper_whitespace_only(self):
        os.chmod(self.paths.pin_file, 0o644)
        self.paths.pin_file.write_text(self.paths.pin_file.read_text() + "\n")  # still valid JSON; hash differs

    def tamper_manifest_sha(self):
        m = json.loads(self.paths.manifest.read_text())
        m["pin_sha256"] = "0" * 64
        self.paths.manifest.write_text(json.dumps(m))

    def tamper_manifest_missing(self):
        self.paths.manifest.unlink()

    def tamper_manifest_without_pin_fields(self):
        m = json.loads(self.paths.manifest.read_text())
        m.pop("pin_sha256")
        self.paths.manifest.write_text(json.dumps(m))

    def tamper_manifest_other_path(self):
        m = json.loads(self.paths.manifest.read_text())
        m["pin_file"] = str(self.tmp / "elsewhere.json")
        self.paths.manifest.write_text(json.dumps(m))

    def tamper_app_marker_removed(self):
        (self.paths.app_dir / safety.MARKER).unlink()

    TAMPERS = ["missing", "symlink", "modified", "whitespace_only", "manifest_sha", "manifest_missing", "manifest_without_pin_fields",
               "manifest_other_path", "app_marker_removed"]

    def test_every_tamper_fails_closed_for_doctor_register_start_and_verify_pin(self):
        for name in self.TAMPERS:
            self.reinstall_fresh()
            getattr(self, f"tamper_{name}")()
            r = self.run_managed("doctor")
            self.assertEqual(r.returncode, 1, name)
            self.assertIn("[FAIL] runner pin", r.stdout, name)
            self.assertNotIn(f"[ok  ] runner pin", r.stdout, name)
            for args in (["register", "--repo", "example-org/example-repo", "--dry-run"], ["start", "--dry-run"],
                         ["install", "--dry-run"], ["uninstall", "--dry-run"], ["verify-pin"]):
                r = self.run_managed(*args)
                self.assertEqual(r.returncode, 2, (name, args, r.stdout, r.stderr))
                self.assertIn("refused:", r.stderr, (name, args))

    def test_gate_for_register_and_start_refuses_on_a_tampered_pin(self):
        self.install_and_remove_checkout()
        self.tamper_modified()
        mod = str(self.paths.app_dir / "pkg" / "local_ci_runner" / "cli.py")
        with mock.patch.dict(os.environ, {"LOCAL_CI_RUNNER_HOME": str(self.paths.home)}), mock.patch.object(cli, "MODULE_FILE", mod):
            with self.assertRaises(InstallError) as c:
                cli._gate(self.paths)()
        self.assertIn("runner pin", str(c.exception))
