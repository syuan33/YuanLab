#!/usr/bin/env python3
"""Idempotently pre-build the Matplotlib font cache for the dedicated, non-admin CI account.

Run it with the managed (toolcache) Python as that account, with a clean environment:
    env -i HOME=<ci home> PATH=... <managed python> -I - [--apply] < preseed_matplotlib_font_cache.py

--require-matplotlib turns "Matplotlib not installed yet" into a failure (exit 3) instead of a skip.
Dry-run (default) imports nothing from Matplotlib and writes nothing. Output is fixed key=value fields only: no paths,
no environment values.
"""
from __future__ import annotations

import json
import os
import stat
import sys

STAMP_NAME = ".local-ci-font-cache-stamp.json"
FONTLIST_GLOB_PREFIX = "fontlist-v"
FONTLIST_GLOB_SUFFIX = ".json"
SCRUB_ENV = ("MPLCONFIGDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME")


class Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def default_cache_dir(home, platform):
    if platform.startswith(("linux", "freebsd")):
        return os.path.join(home, ".cache", "matplotlib")
    return os.path.join(home, ".matplotlib")


def _real_versions():
    import platform
    try:
        from importlib import metadata
        mpl = metadata.version("matplotlib")
    except Exception:
        mpl = None
    return {"python": platform.python_version(), "matplotlib": mpl}


def _real_builder(cache_dir):
    import matplotlib
    if os.path.realpath(matplotlib.get_cachedir()) != os.path.realpath(cache_dir):
        raise Refused("cache_dir_mismatch")
    if "matplotlib.font_manager" in sys.modules:
        raise RuntimeError("font_manager already imported")
    import matplotlib.font_manager  # noqa: F401  (import builds fontlist-v*.json when absent)


def _fontlists(cache_dir):
    return sorted(n for n in os.listdir(cache_dir)
                  if n.startswith(FONTLIST_GLOB_PREFIX) and n.endswith(FONTLIST_GLOB_SUFFIX))


def _check_entry(path, uid):
    """Every cache entry we may read, chmod or delete must be a private-able, single-link regular file we own."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise Refused("cache_entry_is_symlink")
    if not stat.S_ISREG(st.st_mode):
        raise Refused("cache_entry_not_regular")
    if st.st_uid != uid:
        raise Refused("cache_entry_not_owned")
    if st.st_nlink != 1:
        raise Refused("cache_entry_hardlinked")


def _check_cache_dir(cache_dir, uid):
    try:
        st = os.lstat(cache_dir)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        raise Refused("cache_dir_is_symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise Refused("cache_dir_not_directory")
    if st.st_uid != uid:
        raise Refused("cache_dir_not_owned")
    for name in _fontlists(cache_dir) + [STAMP_NAME]:
        _check_entry(os.path.join(cache_dir, name), uid)
    return True


def _real_fontlist_version():
    """Cache-format version Matplotlib expects (FontManager.__version__), read from source without importing it."""
    try:
        import importlib.util
        import re
        spec = importlib.util.find_spec("matplotlib")
        base = os.path.dirname(spec.origin)
        with open(os.path.join(base, "font_manager.py")) as fh:
            m = re.search(r"^\s+__version__\s*=\s*(\d+)\s*$", fh.read(), re.M)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _usable_fontlist(path, expected):
    """A font list Matplotlib would accept without rebuilding: right format version and at least one TrueType entry."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("_version"), int):
        return False
    if expected is not None and data["_version"] != expected:
        return False
    tt = data.get("ttflist")
    return isinstance(tt, list) and len(tt) > 0


def _fontlists_ready(cache_dir, expected):
    names = _fontlists(cache_dir)
    if expected is not None:
        wanted = "%s%d%s" % (FONTLIST_GLOB_PREFIX, expected, FONTLIST_GLOB_SUFFIX)
        return wanted in names and _usable_fontlist(os.path.join(cache_dir, wanted), expected)
    return bool(names) and all(_usable_fontlist(os.path.join(cache_dir, n), None) for n in names)


def _read_stamp(cache_dir):
    try:
        with open(os.path.join(cache_dir, STAMP_NAME)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _modes_private(cache_dir):
    if stat.S_IMODE(os.lstat(cache_dir).st_mode) != 0o700:
        return False
    for name in _fontlists(cache_dir) + [STAMP_NAME]:
        path = os.path.join(cache_dir, name)
        if os.path.lexists(path) and stat.S_IMODE(os.lstat(path).st_mode) != 0o600:
            return False
    return True


def _tighten(cache_dir):
    os.chmod(cache_dir, 0o700)
    for name in _fontlists(cache_dir) + [STAMP_NAME]:
        path = os.path.join(cache_dir, name)
        if os.path.lexists(path) and not os.path.islink(path):
            os.chmod(path, 0o600)


def _plan(cache_dir, exists, versions, expected):
    if not exists:
        return "build"
    stamp = _read_stamp(cache_dir)
    fresh = (stamp is not None and stamp.get("python") == versions["python"]
             and stamp.get("matplotlib") == versions["matplotlib"] and _fontlists_ready(cache_dir, expected))
    if fresh:
        return "none" if _modes_private(cache_dir) else "fix_mode"
    return "rebuild" if (stamp is not None or _fontlists(cache_dir)) else "build"


def _write_stamp(cache_dir, versions):
    # Unique, exclusively-created temp name: never reuse (or truncate) a pre-existing file or hardlink.
    tmp = os.path.join(cache_dir, "%s.%s.tmp" % (STAMP_NAME, os.urandom(8).hex()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"python": versions["python"], "matplotlib": versions["matplotlib"]}, fh, sort_keys=True)
        os.replace(tmp, os.path.join(cache_dir, STAMP_NAME))
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def main(argv=None, *, fontlist_version=None, home=None, uid=None, platform=None, versions=None, builder=None, stdout=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = sys.stdout if stdout is None else stdout
    home = os.path.expanduser("~") if home is None else home
    uid = os.getuid() if uid is None else uid
    platform = sys.platform if platform is None else platform
    versions = _real_versions if versions is None else versions
    builder = _real_builder if builder is None else builder
    fontlist_version = _real_fontlist_version if fontlist_version is None else fontlist_version

    def emit(**fields):
        out.write(" ".join("%s=%s" % kv for kv in fields.items()) + "\n")

    unknown = [a for a in argv if a not in ("--apply", "--require-matplotlib")]
    if unknown:
        emit(status="refused", reason="unknown_argument")
        return 2
    apply = "--apply" in argv
    require = "--require-matplotlib" in argv
    mode = "apply" if apply else "dry-run"

    v = versions()
    if not v.get("matplotlib"):
        if require:
            emit(mode=mode, status="failed", reason="matplotlib_absent")
            return 3
        emit(mode=mode, status="skipped_matplotlib_absent", action="none")
        return 0

    cache_dir = default_cache_dir(home, platform)
    try:
        exists = _check_cache_dir(cache_dir, uid)
        expected = fontlist_version()
        action = _plan(cache_dir, exists, v, expected)
        if not apply or action == "none":
            emit(mode=mode, status="ok" if apply else "planned", action=action)
            return 0
        for key in SCRUB_ENV:
            os.environ.pop(key, None)
        if action == "fix_mode":
            _tighten(cache_dir)
            emit(mode=mode, status="ok", action=action)
            return 0
        if not exists:
            os.makedirs(cache_dir, mode=0o700)
        os.chmod(cache_dir, 0o700)
        stamp = os.path.join(cache_dir, STAMP_NAME)
        if os.path.lexists(stamp):
            os.unlink(stamp)
        for name in _fontlists(cache_dir):
            os.unlink(os.path.join(cache_dir, name))
        try:
            builder(cache_dir)
        except Refused:
            raise
        except Exception:
            emit(mode=mode, status="failed", action=action, reason="build_error")
            return 1
        names = _fontlists(cache_dir)
        if not names:
            emit(mode=mode, status="failed", action=action, reason="no_fontlist_produced")
            return 1
        for name in names:
            path = os.path.join(cache_dir, name)
            _check_entry(path, uid)
            os.chmod(path, 0o600)
        if not _fontlists_ready(cache_dir, expected):
            emit(mode=mode, status="failed", action=action, reason="fontlist_invalid")
            return 1
        _write_stamp(cache_dir, v)
        emit(mode=mode, status="ok", action=action)
        return 0
    except Refused as exc:
        emit(mode=mode, status="refused", reason=exc.reason)
        return 2
    except OSError:
        emit(mode=mode, status="failed", reason="io_error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
