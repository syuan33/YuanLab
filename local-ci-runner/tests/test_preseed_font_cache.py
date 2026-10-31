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
STEP = SCRIPTS / "preseed_font_cache_step.sh"
GOOD_FONTLIST = {"_version": 390, "default_size": 10.0, "ttflist": [{"fname": "synthetic/Sans.ttf", "name": "Synthetic Sans"}], "afmlist": []}
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
        (Path(cache_dir) / "fontlist-v390.json").write_text(json.dumps(GOOD_FONTLIST))

    def run_helper(self, *argv, versions="default", builder="default", uid=None, fontlist_version=lambda: 390):
        out = io.StringIO()
        rc = self.h.main(
            list(argv),
            home=str(self.home),
            uid=os.getuid() if uid is None else uid,
            platform="darwin",
            fontlist_version=fontlist_version,
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

    def test_F17_stamp_write_failure_leaves_no_temp_and_no_valid_stamp(self):
        real_replace = os.replace
        def fail(src, dst):
            raise OSError("synthetic replace failure")
        os.replace = fail
        self.addCleanup(lambda: setattr(os, "replace", real_replace))
        rc, out = self.run_helper("--apply")
        os.replace = real_replace
        self.assertEqual(rc, 1)
        self.assertIn("reason=io_error", out)
        self.assertEqual([n for n in os.listdir(self.cache) if ".tmp" in n], [])
        self.assertFalse((self.cache / ".local-ci-font-cache-stamp.json").exists())

    def test_F18_require_flag_turns_absent_matplotlib_into_a_failure(self):
        absent = lambda: {"python": "3.11.9", "matplotlib": None}
        before = snapshot(self.home)
        rc, out = self.run_helper("--apply", "--require-matplotlib", versions=absent)
        self.assertEqual(rc, 3)
        self.assertIn("status=failed", out)
        self.assertIn("reason=matplotlib_absent", out)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(self.run_helper("--apply", versions=absent)[0], 0)


class RepairRound4Test(Base):
    def test_F32_malformed_font_entries_are_not_usable(self):
        self.run_helper("--apply")
        fontlist = self.cache / "fontlist-v390.json"
        for bad in ([None], [{}], [{"name": "x"}], [{"fname": "x.ttf"}], [{"fname": "", "name": "x"}], [1], [GOOD_FONTLIST["ttflist"][0], None]):
            fontlist.write_text(json.dumps(dict(GOOD_FONTLIST, ttflist=bad)))
            n = len(self.builds)
            rc, out = self.run_helper("--apply")
            self.assertEqual((rc, "action=rebuild" in out, len(self.builds)), (0, True, n + 1), (bad, out))

    def test_F33_builder_writing_malformed_entries_is_not_stamped(self):
        def bad(cache_dir):
            (Path(cache_dir) / "fontlist-v390.json").write_text(json.dumps(dict(GOOD_FONTLIST, ttflist=[None])))
        rc, out = self.run_helper("--apply", builder=bad)
        self.assertEqual(rc, 1)
        self.assertIn("reason=fontlist_invalid", out)
        self.assertFalse((self.cache / ".local-ci-font-cache-stamp.json").exists())

    def test_F34_unknown_cache_format_version_fails_closed_without_writes(self):
        self.run_helper("--apply")
        before = snapshot(self.home)
        n = len(self.builds)
        for argv in (("--apply",), ()):
            rc, out = self.run_helper(*argv, fontlist_version=lambda: None)
            self.assertEqual(rc, 0)
            self.assertIn("status=skipped_font_cache_version_unknown", out)
            self.assertNotIn("status=ok", out)
            self.assertNotIn("status=planned", out)
        self.assertEqual(len(self.builds), n)
        self.assertEqual(snapshot(self.home), before)

    def test_F35_unknown_cache_format_version_is_a_failure_when_required(self):
        rc, out = self.run_helper("--apply", "--require-matplotlib", fontlist_version=lambda: None)
        self.assertEqual(rc, 3)
        self.assertIn("status=failed", out)
        self.assertIn("reason=font_cache_version_unknown", out)
        self.assertFalse(self.cache.exists())

    def test_F36_real_version_lookup_reads_matplotlib_source_without_importing(self):
        import sys
        pkg = self.tmp / "site" / "matplotlib"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("raise RuntimeError('must not be imported')\n")
        (pkg / "font_manager.py").write_text("class FontManager:\n    __version__ = 777\n")
        sys.path.insert(0, str(self.tmp / "site"))
        self.addCleanup(lambda: sys.path.remove(str(self.tmp / "site")))
        sys.modules.pop("matplotlib", None)
        self.assertEqual(self.h._real_fontlist_version(), 777)
        self.assertNotIn("matplotlib", sys.modules)
        (pkg / "font_manager.py").write_text("class FontManager:\n    version = 1\n")
        self.assertIsNone(self.h._real_fontlist_version())


class RepairRound3Test(Base):
    def _applied(self):
        self.run_helper("--apply")
        return self.cache / "fontlist-v390.json"

    def test_F25_unusable_fontlist_objects_are_rebuilt(self):
        fontlist = self._applied()
        for bad in ({}, {"_version": 390, "ttflist": []}, {"_version": 330, "ttflist": [{"n": 1}]}, {"ttflist": [{"n": 1}]},
                    {"_version": 390, "ttflist": "x"}):
            fontlist.write_text(json.dumps(bad))
            n = len(self.builds)
            rc, out = self.run_helper("--apply")
            self.assertEqual((rc, "action=rebuild" in out, len(self.builds)), (0, True, n + 1), (bad, out))

    def test_F26_only_a_stale_version_fontlist_is_rebuilt_to_the_expected_one(self):
        fontlist = self._applied()
        fontlist.unlink()
        (self.cache / "fontlist-v330.json").write_text(json.dumps(dict(GOOD_FONTLIST, _version=330)))
        n = len(self.builds)
        rc, out = self.run_helper("--apply")
        self.assertEqual((rc, "action=rebuild" in out, len(self.builds)), (0, True, n + 1))
        self.assertTrue(fontlist.exists())

    def test_F27_builder_output_must_be_usable_for_the_expected_version(self):
        def bad(cache_dir):
            (Path(cache_dir) / "fontlist-v390.json").write_text("{}")
        rc, out = self.run_helper("--apply", builder=bad)
        self.assertEqual(rc, 1)
        self.assertIn("reason=fontlist_invalid", out)
        self.assertFalse((self.cache / ".local-ci-font-cache-stamp.json").exists())

    def test_F28_oserror_outside_the_builder_is_io_error(self):
        real = os.chmod
        def boom(*a, **k):
            raise PermissionError("synthetic")
        os.chmod = boom
        self.addCleanup(lambda: setattr(os, "chmod", real))
        try:
            rc, out = self.run_helper("--apply")
        finally:
            os.chmod = real
        self.assertEqual(rc, 1)
        self.assertIn("reason=io_error", out)
        self.assertNotIn("Traceback", out)

    def test_F29_unknown_argument_is_refused(self):
        rc, out = self.run_helper("--bogus")
        self.assertEqual(rc, 2)
        self.assertIn("reason=unknown_argument", out)

    def test_F30_real_builder_refuses_a_cache_dir_that_matplotlib_would_not_use(self):
        import sys, types
        fake = types.ModuleType("matplotlib")
        fake.get_cachedir = lambda: str(self.tmp / "elsewhere")
        saved = {k: sys.modules.get(k) for k in ("matplotlib", "matplotlib.font_manager")}
        sys.modules["matplotlib"] = fake
        sys.modules.pop("matplotlib.font_manager", None)
        def restore():
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.addCleanup(restore)
        with self.assertRaises(self.h.Refused) as cm:
            self.h._real_builder(str(self.cache))
        self.assertEqual(cm.exception.reason, "cache_dir_mismatch")

    def test_F31_stdin_entrypoint_runs_isolated_with_clean_env_and_fixed_output(self):
        import sys
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        r = subprocess.run([sys.executable, "-I", "-"], input=HELPER.read_text(), env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r"^mode=dry-run status=(planned|skipped_matplotlib_absent) ")
        self.assertNotIn(str(self.home), r.stdout + r.stderr)
        self.assertEqual(os.listdir(self.home), [])  # dry-run wrote nothing


class StepHarnessTest(TmpCase):
    """Executes the real shell step with a stub helper: rc handling, status parsing, warning, strict forwarding."""

    def run_step(self, helper_body, require):
        helper = self.tmp / "stub_helper.py"
        helper.write_text(helper_body)
        script = (
            'set -euo pipefail\n'
            'die() { printf "DIE:%s\\n" "$*"; exit 9; }\n'
            'source "$STEP"\n'
            'FONT_CACHE_HELPER="$HELPER"; REQUIRE_FONT_CACHE=@REQ@\n'
            'FONT_CACHE_RUN=("$PY" -I -)\n'
            'font_cache_step\n'
            'printf "STATE:%s\\n" "$FONT_CACHE_STATE"\n').replace("@REQ@", "1" if require else "0")
        import sys
        r = subprocess.run(["bash", "-c", script], env={"STEP": str(STEP), "HELPER": str(helper), "PY": sys.executable,
                           "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
        return r

    ARGV_STUB = ('import sys\n'
                 'a = sys.argv[1:]\n'
                 'print("status=" + ("strict" if "--require-matplotlib" in a else "lax") + ("_apply" if "--apply" in a else "_noapply"))\n')

    def test_H01_helper_gets_apply_and_no_strict_flag_by_default(self):
        r = self.run_step(self.ARGV_STUB, False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("STATE:lax_apply", r.stdout)

    def test_H02_strict_flag_is_forwarded_only_when_required(self):
        r = self.run_step(self.ARGV_STUB, True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("STATE:strict_apply", r.stdout)

    def test_H06_version_unknown_skip_also_warns(self):
        r = self.run_step('print("mode=apply status=skipped_font_cache_version_unknown action=none")\n', False)
        self.assertEqual(r.returncode, 0)
        self.assertIn("STATE:skipped_font_cache_version_unknown", r.stdout)
        self.assertIn("WARNING: Matplotlib font cache NOT pre-built", r.stderr)

    def test_H03_skip_warns_on_stderr_and_continues_when_not_required(self):
        r = self.run_step('print("mode=apply status=skipped_matplotlib_absent action=none")\n', False)
        self.assertEqual(r.returncode, 0)
        self.assertIn("STATE:skipped_matplotlib_absent", r.stdout)
        self.assertIn("WARNING: Matplotlib font cache NOT pre-built", r.stderr)

    def test_H04_helper_failure_dies_with_its_fixed_fields(self):
        r = self.run_step('print("status=failed reason=matplotlib_absent")\nraise SystemExit(3)\n', True)
        self.assertEqual(r.returncode, 9)
        self.assertIn("DIE:", r.stdout)
        self.assertIn("reason=matplotlib_absent", r.stdout)

    def test_H05_missing_status_dies(self):
        r = self.run_step('print("garbage")\n', False)
        self.assertEqual(r.returncode, 9)
        self.assertIn("no status", r.stdout)


class RepairRound2Test(Base):
    def _applied(self):
        self.run_helper("--apply")
        return self.cache / ".local-ci-font-cache-stamp.json", self.cache / "fontlist-v390.json"

    def test_F19_truncated_fontlist_with_matching_stamp_is_rebuilt(self):
        stamp, fontlist = self._applied()
        fontlist.write_text('{"ttflist": [')
        n = len(self.builds)
        rc, out = self.run_helper("--apply")
        self.assertEqual(rc, 0)
        self.assertIn("action=rebuild", out)
        self.assertEqual(len(self.builds), n + 1)

    def test_F20_stamp_directory_is_refused_not_a_traceback(self):
        stamp, _ = self._applied()
        stamp.unlink()
        stamp.mkdir()
        rc, out = self.run_helper("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("reason=cache_entry_not_regular", out)

    def test_F21_fifo_stamp_is_refused_without_blocking(self):
        stamp, _ = self._applied()
        stamp.unlink()
        os.mkfifo(stamp)
        rc, out = self.run_helper("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("reason=cache_entry_not_regular", out)

    def test_F22_hardlinked_stamp_or_fontlist_is_refused_and_target_modes_untouched(self):
        stamp, fontlist = self._applied()
        for entry in (stamp, fontlist):
            victim = self.tmp / ("victim-" + entry.name)
            victim.write_text("keep")
            os.chmod(victim, 0o644)
            os.link(victim, self.cache / (entry.name + ".link"))
            # replace the real entry with a hardlink to the victim
            entry.unlink()
            os.link(victim, entry)
            os.chmod(self.cache, 0o777)
            rc, out = self.run_helper("--apply")
            self.assertEqual(rc, 2, out)
            self.assertIn("reason=cache_entry_hardlinked", out)
            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o644)
            self.assertEqual(victim.read_text(), "keep")
            entry.unlink()
            (self.cache / (entry.name + ".link")).unlink()
            self.run_helper("--apply")

    def test_F23_non_regular_fontlist_and_non_directory_cache_are_refused(self):
        _, fontlist = self._applied()
        fontlist.unlink()
        fontlist.mkdir()
        self.assertIn("reason=cache_entry_not_regular", self.run_helper("--apply")[1])
        fontlist.rmdir()
        import shutil
        shutil.rmtree(self.cache)
        self.cache.write_text("not a dir")
        rc, out = self.run_helper("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("reason=cache_dir_not_directory", out)

    def test_F24_unexpected_oserror_is_reported_as_fixed_fields_not_a_traceback(self):
        def oserr(cache_dir):
            raise PermissionError("synthetic")
        rc, out = self.run_helper("--apply", builder=oserr)
        self.assertEqual(rc, 1)
        self.assertIn("status=failed", out)
        self.assertNotIn("Traceback", out)


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
        saved = dict(os.environ)
        os.environ["SECRET_TOKEN"] = SECRET
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for argv in ((), ("--apply",)):
            rc, out = self.run_helper(*argv)
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
        rc, _ = self.run_helper("--apply")
        self.assertEqual(rc, 0)
        self.assertTrue((self.cache / "fontlist-v390.json").exists())
        self.assertEqual(list(elsewhere.iterdir()), [])
        for seen in self.env_seen:
            for k in env:
                self.assertNotIn(k, seen)

    def test_F15_linux_default_cache_dir_is_xdg_style(self):
        out = io.StringIO()
        rc = self.h.main(["--apply"], home=str(self.home), uid=os.getuid(), platform="linux", fontlist_version=lambda: 390,
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
        self.assertIn("preseed_font_cache_step.sh", self.src)
        self.assertIn('sudo -u "$CI_USER" /usr/bin/env -i', self.src)

    def test_S03_dry_run_plan_mentions_font_cache_and_does_not_invoke_helper_before_apply_gate(self):
        self.assertIn("Matplotlib font cache", self.src)
        gate = self.src.index('if [ "$APPLY" -ne 1 ]')
        self.assertEqual(self.src.count("\nfont_cache_step\n"), 1)
        self.assertGreater(self.src.index("\nfont_cache_step\n"), gate)
        self.assertEqual(STEP.read_text().count('<"$FONT_CACHE_HELPER"'), 1)

    def test_S06_require_font_cache_flag_and_loud_skip_notice(self):
        self.assertIn("--require-font-cache", self.src)
        step = STEP.read_text()
        self.assertIn("--require-matplotlib", step)
        self.assertIn("WARNING: Matplotlib font cache NOT pre-built", step)
        self.assertIn("skipped_font_cache_version_unknown", step)
        self.assertIn("--require-font-cache requires --apply", self.src)

    def test_S04_manifest_records_font_cache_status(self):
        self.assertIn("matplotlib_font_cache=", self.src)

    def test_S05_pinned_values_and_boundaries_unchanged(self):
        for needle in ('PYTHON_VERSION="3.11.9"', "runner_started=false", "workflow_changed=false", "non-admin"):
            self.assertIn(needle, self.src)
        self.assertNotIn("local-ci-runner start", self.src)
