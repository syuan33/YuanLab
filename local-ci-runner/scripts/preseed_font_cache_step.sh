#!/bin/bash
# Sourced by preseed-macos-ci-toolchain.sh (and by its tests); defines font_cache_step only, runs nothing.
#
# Inputs (set by the caller): FONT_CACHE_HELPER (path of preseed_matplotlib_font_cache.py), REQUIRE_FONT_CACHE (0|1),
# FONT_CACHE_RUN (array: the command prefix that runs the managed Python in isolated mode as the CI account, reading the
# helper from stdin), and a die() function. Output: FONT_CACHE_STATE (the helper's status field).
#
# The helper's stderr is discarded and only its fixed key=value stdout is ever shown: no paths, no environment values.

font_cache_step() {
  local require_args=() rc out
  if [ "$REQUIRE_FONT_CACHE" -eq 1 ]; then
    require_args=(--require-matplotlib)
  fi
  set +e
  out="$("${FONT_CACHE_RUN[@]}" --apply ${require_args[@]+"${require_args[@]}"} <"$FONT_CACHE_HELPER" 2>/dev/null)"
  rc=$?
  set -e
  [ "$rc" -eq 0 ] || die "Matplotlib font-cache preseed failed: ${out:-no output}"
  FONT_CACHE_STATE="$(printf '%s\n' "$out" | /usr/bin/sed -n 's/.*status=\([a-z_]*\).*/\1/p' | /usr/bin/head -n 1)"
  [ -n "$FONT_CACHE_STATE" ] || die 'Matplotlib font-cache helper returned no status'
  if [ "$FONT_CACHE_STATE" = "skipped_matplotlib_absent" ]; then
    printf 'WARNING: Matplotlib font cache NOT pre-built (Matplotlib is not installed for the managed Python yet).\n' >&2
    printf '         Rerun with --apply --require-font-cache after the repository dependencies are installed.\n' >&2
  fi
}
