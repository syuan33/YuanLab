"""Contract tests for the Matplotlib font-cache preseed helper. Fully synthetic: no real Matplotlib, no real home."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
from pathlib import Path

from tests.fixtures import TmpCase

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
HELPER = SCRIPTS / "preseed_matplotlib_font_cache.py"
PRESEED = SCRIPTS / "preseed-macos-ci-toolchain.sh"
SECRET = "SYNTHETIC-SECRET-VALUE-0000"  # synthetic sentinel, must never be echoed


def load_helper():
    spec = importlib.util.spec_from_file_location("preseed_matplotlib_font_cache", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def snapshot(root):
    return sorted((str(p.relative_to(root)), p.lstat().st_mode, p.lstat().st_mtime_ns) for p in root.rglob("*"))


class Base(TmpCase):
    def setUp(self):
        super().setUp()
        self.h = load_helper()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cache = self.home / ".matplotlib"
        self.versions = {"python": "3.11.9", "matplotlib": "3.9.0"}
        self.builds = []
        self.env_seen = []

    def builder(self, cache_dir):
        self.builds.append(str(cache_dir))
        self.env_seen.append(dict(os.environ))
        (Path(cache_dir) / "fontlist-v390.json").write_text("{}")

    def run_helper(self, *argv, environ=None, versions="default", builder="default", uid=None):
        out = io.StringIO()
        rc = self.h.main(
            list(argv),
            environ={} if environ is None else environ,
            home=str(self.home),
            uid=os.getuid() if uid is None else uid,
            platform="darwin",
            versions=(lambda: dict(self.versions)) if versions == "default" else versions,
            builder=self.builder if builder == "default" else builder,
            stdout=out,
        )
        return rc, out.getvalue()


class DryRunTest(Base):
    def test_F01_dry_run_cold_cache_has_no_side_effects(self):
        before = snapshot(self.home)
        rc, out = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertIn("mode=dry-run", out)
        self.assertIn("action=build", out)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.builds, [])

    def test_F02_dry_run_after_apply_reports_none_without_touching(self):
        self.assertEqual(self.run_helper("--apply")[0], 0)
        before = snapshot(self.home)
        rc, out = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertIn("action=none", out)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(len(self.builds), 1)


class ApplyTest(Base):
    def test_F03_apply_creates_private_cache_and_stamp(self):
        rc, out = self.run_helper("--apply")
        self.assertEqual(rc, 0)
        self.assertIn("action=build", out)
        self.assertIn("status=ok", out)
        self.assertTrue(self.cache.is_dir() and not self.cache.is_symlink())
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o700)
        self.assertEqual(self.cache.stat().st_uid, os.getuid())
        fontlist = self.cache / "fontlist-v390.json"
        self.assertTrue(fontlist.is_file() and not fontlist.is_symlink())
        self.assertEqual(stat.S_IMODE(fontlist.stat().st_mode), 0o600)
        self.assertEqual(fontlist.stat().st_uid, os.getuid())
        stamp = json.loads((self.cache / ".local-ci-font-cache-stamp.json").read_text())
        self.assertEqual(stamp["python"], "3.11.9")
        self.assertEqual(stamp["matplotlib"], "3.9.0")
        self.assertEqual(stat.S_IMODE((self.cache / ".local-ci-font-cache-stamp.json").stat().st_mode), 0o600)

    def test_F04_repeat_apply_is_idempotent(self):
        self.run_helper("--apply")
        before = snapshot(self.home)
        rc, out = self.run_helper("--apply")
        self.assertEqual(rc, 0)
        self.assertIn("action=none", out)
        self.assertEqual(len(self.builds), 1)
        self.assertEqual(snapshot(self.home), before)

    def test_F05_version_change_rebuilds_and_keeps_unrelated_files(self):
        self.run_helper("--apply")
        (self.cache / "matplotlibrc").write_text("# user file\n")
        (self.cache / "fontlist-v330.json").write_text("{}")
        for key, val in (("matplotlib", "3.10.0"), ("python", "3.12.1")):
            self.versions = {"python": "3.11.9", "matplotlib": "3.9.0"}
            self.run_helper("--apply")  # restore stamp for the base versions
            n = len(self.builds)
            self.versions = dict(self.versions, **{key: val})
            rc, out = self.run_helper("--apply")
            self.assertEqual(rc, 0)
            self.assertIn("action=rebuild", out)
            self.assertEqual(len(self.builds), n + 1)
            stamp = json.loads((self.cache / ".local-ci-font-cache-stamp.json").read_text())
            self.assertEqual(stamp[key], val)
        self.assertEqual((self.cache / "matplotlibrc").read_text(), "# user file\n")
        self.assertFalse((self.cache / "fontlist-v330.json").exists())

    def test_F14_corrupt_stamp_rebuilds(self):
        self.run_helper("--apply")
        (self.cache / ".local-ci-font-cache-stamp.json").write_text("{not json")
        rc, out = self.run_helper("--apply")
        self.assertEqual(rc, 0)
        self.assertIn("action=rebuild", out)
        json.loads((self.cache / ".local-ci-font-cache-stamp.json").read_text())

    def test_F08_loose_mode_dir_is_tightened(self):
        self.cache.mkdir()
        os.chmod(self.cache, 0o777)
        self.assertEqual(self.run_helper("--apply")[0], 0)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o700)


class RepairRound1Test(Base):
    def test_F16_loose_modes_with_valid_stamp_are_restored_without_rebuild(self):
        self.run_helper("--apply")
        fontlist = self.cache / "fontlist-v390.json"
        os.chmod(self.cache, 0o777)
        os.chmod(fontlist, 0o666)
        rc, out = self.run_helper()
        self.assertEqual(rc, 0)
        self.assertIn("action=fix_mode", out)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o777)  # dry-run must not touch
        self.assertEqual(stat.S_IMODE(fontlist.stat().st_mode), 0o666)
        n = len(self.builds)
        rc, out = self.run_helper("--apply")
        self.assertEqual(rc, 0)
        self.assertIn("action=fix_mode", out)
        self.assertEqual(len(self.builds), n)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(fontlist.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.cache / ".local-ci-font-cache-stamp.json").stat().st_mode), 0o600)
        self.assertIn("action=none", self.run_helper("--apply")[1])

    def test_F17_stamp_write_never_truncates_a_hardlinked_temp_and_leaves_no_temp(self):
        self.run_helper("--apply")
        victim = self.tmp / "victim.txt"
        victim.write_text("keep")
        os.link(victim, self.cache / ".local-ci-font-cache-stamp.json.tmp")
        self.versions = {"python": "3.11.9", "matplotlib": "3.10.0"}
        self.assertEqual(self.run_helper("--apply")[0], 0)
        self.assertEqual(victim.read_text(), "keep")
        leftovers = [n for n in os.listdir(self.cache) if ".tmp" in n and n != ".local-ci-font-cache-stamp.json.tmp"]
        self.assertEqual(leftovers, [])

    def test_F18_require_flag_turns_absent_matplotlib_into_a_failure(self):
        absent = lambda: {"python": "3.11.9", "matplotlib": None}
        before = snapshot(self.home)
        rc, out = self.run_helper("--apply", "--require-matplotlib", versions=absent)
        self.assertEqual(rc, 3)
        self.assertIn("status=failed", out)
        self.assertIn("reason=matplotlib_absent", out)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(self.run_helper("--apply", versions=absent)[0], 0)


class RefusalTest(Base):
    def test_F06_symlinked_cache_dir_is_refused(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        self.cache.symlink_to(target)
        before = snapshot(target)
        rc, out = self.run_helper("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("status=refused", out)
        self.assertIn("reason=cache_dir_is_symlink", out)
        self.assertEqual(self.builds, [])
        self.assertEqual(snapshot(target), before)

    def test_F07_symlinked_fontlist_is_refused_and_target_untouched(self):
        self.run_helper("--apply")
        victim = self.tmp / "victim.txt"
        victim.write_text("keep")
        (self.cache / "fontlist-v330.json").symlink_to(victim)
        (self.cache / ".local-ci-font-cache-stamp.json").unlink()
        rc, out = self.run_helper("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("reason=cache_entry_is_symlink", out)
        self.assertEqual(victim.read_text(), "keep")

    def test_F09_foreign_owner_is_refused(self):
        self.cache.mkdir()
        rc, out = self.run_helper("--apply", uid=os.getuid() + 1)
        self.assertEqual(rc, 2)
        self.assertIn("reason=cache_dir_not_owned", out)
        self.assertEqual(self.builds, [])

    def test_F10_failed_build_leaves_no_valid_stamp_and_retries(self):
        def boom(cache_dir):
            raise RuntimeError("boom")
        rc, out = self.run_helper("--apply", builder=boom)
        self.assertEqual(rc, 1)
        self.assertIn("status=failed", out)
        self.assertFalse((self.cache / ".local-ci-font-cache-stamp.json").exists())
        rc, out = self.run_helper("--apply")
        self.assertEqual(rc, 0)
        self.assertIn("action=build", out)

    def test_F10b_build_that_writes_no_fontlist_is_failure(self):
        rc, out = self.run_helper("--apply", builder=lambda cache_dir: None)
        self.assertEqual(rc, 1)
        self.assertIn("reason=no_fontlist_produced", out)
        self.assertFalse((self.cache / ".local-ci-font-cache-stamp.json").exists())

    def test_F11_matplotlib_absent_is_skipped_without_writes(self):
        before = snapshot(self.home)
        rc, out = self.run_helper("--apply", versions=lambda: {"python": "3.11.9", "matplotlib": None})
        self.assertEqual(rc, 0)
        self.assertIn("status=skipped_matplotlib_absent", out)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(self.builds, [])


class HygieneTest(Base):
    def test_F12_output_never_contains_home_or_env_values(self):
        env = {"SECRET_TOKEN": SECRET, "HOME": str(self.home)}
        for argv in ((), ("--apply",)):
            rc, out = self.run_helper(*argv, environ=env)
            self.assertEqual(rc, 0)
            self.assertNotIn(SECRET, out)
            self.assertNotIn(str(self.home), out)
            self.assertNotIn(str(self.tmp), out)

    def test_F13_cache_env_overrides_are_ignored_and_scrubbed(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        env = {"MPLCONFIGDIR": str(elsewhere), "XDG_CACHE_HOME": str(elsewhere), "XDG_CONFIG_HOME": str(elsewhere)}
        saved = dict(os.environ)
        os.environ.update(env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        rc, _ = self.run_helper("--apply", environ=env)
        self.assertEqual(rc, 0)
        self.assertTrue((self.cache / "fontlist-v390.json").exists())
        self.assertEqual(list(elsewhere.iterdir()), [])
        for seen in self.env_seen:
            for k in env:
                self.assertNotIn(k, seen)

    def test_F15_linux_default_cache_dir_is_xdg_style(self):
        out = io.StringIO()
        rc = self.h.main(["--apply"], environ={}, home=str(self.home), uid=os.getuid(), platform="linux",
                         versions=lambda: dict(self.versions), builder=self.builder, stdout=out)
        self.assertEqual(rc, 0)
        self.assertTrue((self.home / ".cache" / "matplotlib" / "fontlist-v390.json").exists())


class PreseedScriptContractTest(TmpCase):
    def setUp(self):
        super().setUp()
        self.src = PRESEED.read_text()

    def test_S01_bash_syntax_ok(self):
        r = subprocess.run(["bash", "-n", str(PRESEED)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_S02_script_runs_helper_as_ci_user_with_clean_env_and_isolated_python(self):
        self.assertIn("preseed_matplotlib_font_cache.py", self.src)
        self.assertIn("env -i", self.src)
        self.assertIn('"${PYTHON_TOOLCACHE_ARCH}/python" -I -', self.src)
        self.assertIn('sudo -u "$CI_USER" /usr/bin/env -i', self.src)

    def test_S03_dry_run_plan_mentions_font_cache_and_does_not_invoke_helper_before_apply_gate(self):
        self.assertIn("Matplotlib font cache", self.src)
        gate = self.src.index('if [ "$APPLY" -ne 1 ]')
        self.assertEqual(self.src.count('<"$FONT_CACHE_HELPER"'), 1)
        self.assertGreater(self.src.index('<"$FONT_CACHE_HELPER"'), gate)

    def test_S06_require_font_cache_flag_and_loud_skip_notice(self):
        self.assertIn("--require-font-cache", self.src)
        self.assertIn("--require-matplotlib", self.src)
        self.assertIn("WARNING: Matplotlib font cache NOT pre-built", self.src)

    def test_S04_manifest_records_font_cache_status(self):
        self.assertIn("matplotlib_font_cache=", self.src)

    def test_S05_pinned_values_and_boundaries_unchanged(self):
        for needle in ('PYTHON_VERSION="3.11.9"', "runner_started=false", "workflow_changed=false", "non-admin"):
            self.assertIn(needle, self.src)
        self.assertNotIn("local-ci-runner start", self.src)
