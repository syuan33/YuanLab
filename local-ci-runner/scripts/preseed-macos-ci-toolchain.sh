#!/bin/bash
# Preseed the exact Python used by the local GitHub Actions runner, then
# validate the existing Node/Java/ClojureScript prerequisites.
#
# This script deliberately does NOT register/start a runner, enable a workflow,
# install repository dependencies, or grant the CI account administrator rights.

set -euo pipefail
IFS=$'\n\t'
export LANG=C
export LC_ALL=C

PYTHON_VERSION="3.11.9"
PYTHON_RELEASE="3.11.9-9947079978"
PYTHON_ARCHIVE="python-3.11.9-darwin-arm64.tar.gz"
PYTHON_ARCHIVE_SHA256="8039644e495c8afb213d3ecb04e9cc3739bb4ddac258432a205cf463997bbeff"
PYTHON_SETUP_SHA256="63cba755c85d97868745faf9a15ce0487ce1d886a8c21dd20fff4acf75c358f8"
PYTHON_PKG_SHA256="b6cfdee2571ca56ee895043ca1e7110fb78a878cee3eb0c21accb2de34d24b55"
PYTHON_RELEASE_BASE="https://github.com/actions/python-versions/releases/download/${PYTHON_RELEASE}"

CLJ_KONDO_VERSION="2026.01.19"
CLJ_KONDO_SHA256="911f3803be075d59172cb92f0d39aea077fc7550dcdc4e8c946d61552fa51a12"
CLJ_KONDO_URL="https://github.com/clj-kondo/clj-kondo/releases/download/v${CLJ_KONDO_VERSION}/clj-kondo-${CLJ_KONDO_VERSION}-macos-aarch64.zip"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FONT_CACHE_HELPER="${SCRIPT_DIR}/preseed_matplotlib_font_cache.py"

CI_USER=""
APPLY=0
REQUIRE_FONT_CACHE=0
TMP_DIR=""

usage() {
  cat <<'EOF'
Usage:
  preseed-macos-ci-toolchain.sh --ci-user USER [--apply] [--require-font-cache]

Without --apply, the script downloads and verifies the pinned assets and prints
the planned persistent changes. With --apply, macOS asks for an administrator
password and installs Python 3.11.9 for the dedicated non-admin CI account.

--require-font-cache makes the run fail unless the Matplotlib font cache was actually pre-built (Matplotlib must already
be installed for the managed Python; use it for the final pre-run preseed, after the repository dependencies exist).

The script never starts/registers the runner or enables a GitHub workflow.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
    case "$TMP_DIR" in
      /private/tmp/local-ci-toolchain.*|/tmp/local-ci-toolchain.*)
        rm -rf -- "$TMP_DIR"
        ;;
      *)
        printf 'WARNING: refusing to clean unexpected temporary path\n' >&2
        ;;
    esac
  fi
}
trap cleanup EXIT HUP INT TERM

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ci-user)
      [ "$#" -ge 2 ] || die '--ci-user requires a value'
      CI_USER="$2"
      shift 2
      ;;
    --apply)
      APPLY=1
      shift
      ;;
    --require-font-cache)
      REQUIRE_FONT_CACHE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[ -n "$CI_USER" ] || die '--ci-user is required'
[ "$(uname -s)" = "Darwin" ] || die 'this script supports macOS only'
[ "$(uname -m)" = "arm64" ] || die 'this script supports Apple Silicon only'

for tool in curl dscl dseditgroup id mktemp ps python3 shasum sudo tar unzip; do
  command -v "$tool" >/dev/null 2>&1 || die "required tool not found: $tool"
done

[ -f "$FONT_CACHE_HELPER" ] || die 'Matplotlib font-cache helper is missing next to this script'
id "$CI_USER" >/dev/null 2>&1 || die 'dedicated CI account does not exist'
CI_UID="$(id -u "$CI_USER")"
CI_GROUP="$(id -gn "$CI_USER")"

if /usr/sbin/dseditgroup -o checkmember -m "$CI_USER" admin 2>/dev/null | grep -q 'yes'; then
  die 'the dedicated CI account must remain non-admin'
fi

CI_HOME="$(/usr/bin/dscl . -read "/Users/${CI_USER}" NFSHomeDirectory 2>/dev/null | sed 's/^NFSHomeDirectory:[[:space:]]*//')"
case "$CI_HOME" in
  /Users/*) ;;
  *) die 'dedicated CI account has an unexpected home directory' ;;
esac
[ -d "$CI_HOME" ] || die 'dedicated CI home directory does not exist'

RUNNER_PROCESSES="$(/bin/ps -U "$CI_UID" -o pid=,command= 2>/dev/null | /usr/bin/grep -E 'Runner\.(Listener|Worker)' || true)"
[ -z "$RUNNER_PROCESSES" ] || die 'runner is active; stop it before pre-seeding the toolchain'

STATE_DIR="${CI_HOME}/.local-ci-runner"
TOOLCACHE_ROOT="${STATE_DIR}/work/_tool"
PYTHON_FRAMEWORK="/Library/Frameworks/Python.framework/Versions/3.11"
PYTHON_APP="/Applications/Python 3.11"
PYTHON_TOOLCACHE_VERSION="${TOOLCACHE_ROOT}/Python/${PYTHON_VERSION}"
PYTHON_TOOLCACHE_ARCH="${PYTHON_TOOLCACHE_VERSION}/arm64"
MANIFEST="${STATE_DIR}/python-preseed-${PYTHON_VERSION}.txt"

TMP_DIR="$(mktemp -d /private/tmp/local-ci-toolchain.XXXXXX)"
ARCHIVE="${TMP_DIR}/${PYTHON_ARCHIVE}"
HASHES="${TMP_DIR}/hashes.sha256"
EXTRACTED="${TMP_DIR}/python"
CHOICES="${TMP_DIR}/python-choices.plist"

printf 'Downloading pinned public release assets...\n'
/usr/bin/curl --fail --location --silent --show-error \
  --output "$HASHES" "${PYTHON_RELEASE_BASE}/hashes.sha256"
/usr/bin/curl --fail --location --silent --show-error \
  --output "$ARCHIVE" "${PYTHON_RELEASE_BASE}/${PYTHON_ARCHIVE}"

ACTUAL_ARCHIVE_SHA256="$(/usr/bin/shasum -a 256 "$ARCHIVE" | /usr/bin/awk '{print $1}')"
[ "$ACTUAL_ARCHIVE_SHA256" = "$PYTHON_ARCHIVE_SHA256" ] || die 'Python archive checksum mismatch'
/usr/bin/awk -v wanted_sha="$PYTHON_ARCHIVE_SHA256" -v wanted_file="$PYTHON_ARCHIVE" '
  BEGIN { wanted_sha=toupper(wanted_sha) }
  toupper($1)==wanted_sha && $2==wanted_file { found=1 }
  END { exit found ? 0 : 1 }
' "$HASHES" || die 'official hashes.sha256 does not contain the pinned archive digest'

# Reject absolute paths, traversal, links, devices, and unexpected members before extraction.
/usr/bin/python3 - "$ARCHIVE" <<'PY_ARCHIVE_CHECK' || die 'unsafe or unexpected Python archive'
import pathlib
import sys
import tarfile

expected = {"setup.sh", "python-3.11.9-macos11.pkg", "build_output.txt"}
with tarfile.open(sys.argv[1], "r:gz") as archive:
    seen = set()
    for member in archive.getmembers():
        name = member.name[2:] if member.name.startswith("./") else member.name
        path = pathlib.PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(1)
        if member.isdir() and name in ("", "."):
            continue
        if not member.isfile() or name not in expected:
            raise SystemExit(1)
        seen.add(name)
    if seen != expected:
        raise SystemExit(1)
PY_ARCHIVE_CHECK

/bin/mkdir -p "$EXTRACTED"
/usr/bin/tar -xzf "$ARCHIVE" -C "$EXTRACTED" --no-same-owner
SETUP_SHA="$(/usr/bin/shasum -a 256 "${EXTRACTED}/setup.sh" | /usr/bin/awk '{print $1}')"
PKG_SHA="$(/usr/bin/shasum -a 256 "${EXTRACTED}/python-3.11.9-macos11.pkg" | /usr/bin/awk '{print $1}')"
[ "$SETUP_SHA" = "$PYTHON_SETUP_SHA256" ] || die 'Python setup.sh checksum mismatch'
[ "$PKG_SHA" = "$PYTHON_PKG_SHA256" ] || die 'Python pkg checksum mismatch'

# Install only what setup-python needs. In particular, do not alter any shell profile,
# add /usr/local/bin shims, or install the offline documentation bundle.
cat >"$CHOICES" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<array>
  <dict><key>choiceIdentifier</key><string>org.python.Python.PythonFramework-3.11</string><key>choiceAttribute</key><string>selected</string><key>attributeSetting</key><integer>1</integer></dict>
  <dict><key>choiceIdentifier</key><string>org.python.Python.PythonApplications-3.11</string><key>choiceAttribute</key><string>selected</string><key>attributeSetting</key><integer>1</integer></dict>
  <dict><key>choiceIdentifier</key><string>org.python.Python.PythonInstallPip-3.11</string><key>choiceAttribute</key><string>selected</string><key>attributeSetting</key><integer>1</integer></dict>
  <dict><key>choiceIdentifier</key><string>org.python.Python.PythonUnixTools-3.11</string><key>choiceAttribute</key><string>selected</string><key>attributeSetting</key><integer>0</integer></dict>
  <dict><key>choiceIdentifier</key><string>org.python.Python.PythonDocumentation-3.11</string><key>choiceAttribute</key><string>selected</string><key>attributeSetting</key><integer>0</integer></dict>
  <dict><key>choiceIdentifier</key><string>org.python.Python.PythonProfileChanges-3.11</string><key>choiceAttribute</key><string>selected</string><key>attributeSetting</key><integer>0</integer></dict>
</array>
</plist>
EOF
/usr/sbin/installer \
  -showChoicesAfterApplyingChangesXML "$CHOICES" \
  -pkg "${EXTRACTED}/python-3.11.9-macos11.pkg" \
  -target / >/dev/null \
  || die 'Python installer rejected the minimized choice set'

printf '\nVerified plan:\n'
printf '  - install official Python %s (%s)\n' "$PYTHON_VERSION" "$PYTHON_RELEASE"
printf '  - create setup-python toolcache entry under the managed runner work directory\n'
printf '  - give the dedicated non-admin account ownership of Python.framework 3.11 only\n'
printf '  - verify existing Node 22 and Java 21 caches\n'
printf '  - checksum and execute clj-kondo %s from a temporary directory only\n' "$CLJ_KONDO_VERSION"
printf '  - pre-build the Matplotlib font cache with the managed Python, only if Matplotlib is already installed there\n'
printf '    (private mode-0700 cache owned by the CI account; rebuilt only when the Python/Matplotlib version changes)\n'
printf '  - do NOT install Clojure/shadow-cljs globally (the repository uses npm ci)\n'
printf '  - do NOT start/register the runner or change any GitHub workflow\n'

if [ "$APPLY" -ne 1 ]; then
  printf '\nDRY RUN COMPLETE: rerun with --apply to make the persistent Python changes.\n'
  exit 0
fi

printf '\nApplying Python preseed; macOS may prompt for an administrator password...\n'
/usr/bin/sudo -v
/usr/bin/sudo -u "$CI_USER" /bin/test -d "$STATE_DIR" \
  || die 'managed local-ci-runner state directory is missing'
/usr/bin/sudo -u "$CI_USER" /bin/test -d "${STATE_DIR}/runner" \
  || die 'managed runner installation is missing'
/usr/bin/sudo -u "$CI_USER" /bin/test -d "${STATE_DIR}/work" \
  || die 'managed runner work directory is missing'

PYTHON_ALREADY_READY=0
if /usr/bin/sudo -u "$CI_USER" /bin/test -f "${PYTHON_TOOLCACHE_VERSION}/arm64.complete" && \
   /usr/bin/sudo -u "$CI_USER" /bin/test -x "${PYTHON_TOOLCACHE_ARCH}/python" && \
   [ "$(/usr/bin/sudo -u "$CI_USER" /usr/bin/env HOME="$CI_HOME" \
       "${PYTHON_TOOLCACHE_ARCH}/python" -c 'import platform; print(platform.python_version())')" = "$PYTHON_VERSION" ]; then
  PYTHON_ALREADY_READY=1
  printf 'Existing completed Python %s toolcache entry verified; package reinstall skipped.\n' "$PYTHON_VERSION"
fi

if [ "$PYTHON_ALREADY_READY" -ne 1 ]; then
  /usr/bin/sudo /usr/sbin/installer \
    -pkg "${EXTRACTED}/python-3.11.9-macos11.pkg" \
    -target / \
    -applyChoiceChangesXML "$CHOICES"

  [ -x "${PYTHON_FRAMEWORK}/bin/python3.11" ] || die 'Python framework installation did not produce python3.11'
  [ -d "$PYTHON_APP" ] || die 'Python applications directory is missing after installation'

  # The upstream pkg makes the framework group-writable by the admin group. The runner
  # intentionally is not an admin, so transfer only this pinned Python version to it.
  /usr/bin/sudo /usr/sbin/chown -R "${CI_USER}:${CI_GROUP}" "$PYTHON_FRAMEWORK"
fi

# Build the exact setup-python cache entry as the dedicated account. Refuse to delete
# anything except the exact pinned version directory.
/usr/bin/sudo -u "$CI_USER" /usr/bin/env \
  HOME="$CI_HOME" \
  TOOLCACHE_ROOT="$TOOLCACHE_ROOT" \
  PYTHON_FRAMEWORK="$PYTHON_FRAMEWORK" \
  PYTHON_APP="$PYTHON_APP" \
  PYTHON_TOOLCACHE_VERSION="$PYTHON_TOOLCACHE_VERSION" \
  PYTHON_TOOLCACHE_ARCH="$PYTHON_TOOLCACHE_ARCH" \
  /bin/bash <<'CI_USER_SETUP'
set -euo pipefail
IFS=$'\n\t'

case "$PYTHON_TOOLCACHE_VERSION" in
  "$HOME/.local-ci-runner/work/_tool/Python/3.11.9") ;;
  *) printf 'refusing unexpected toolcache path\n' >&2; exit 1 ;;
esac

if [ -e "${PYTHON_TOOLCACHE_VERSION}/arm64.complete" ]; then
  if [ -x "${PYTHON_TOOLCACHE_ARCH}/python" ] && \
     [ "$("${PYTHON_TOOLCACHE_ARCH}/python" -c 'import platform; print(platform.python_version())')" = "3.11.9" ]; then
    printf 'Existing complete Python toolcache entry is valid; refreshing pip/certificates.\n'
  else
    printf 'refusing inconsistent completed Python toolcache entry\n' >&2
    exit 1
  fi
else
  if [ -e "$PYTHON_TOOLCACHE_ARCH" ]; then
    rm -rf -- "$PYTHON_TOOLCACHE_ARCH"
  fi
  mkdir -p "$PYTHON_TOOLCACHE_ARCH"
  cd "$PYTHON_TOOLCACHE_ARCH"
  ln -s "${PYTHON_FRAMEWORK}/bin" bin
  ln -s "${PYTHON_FRAMEWORK}/include" include
  ln -s "${PYTHON_FRAMEWORK}/share" share
  ln -s "${PYTHON_FRAMEWORK}/lib" lib
  ln -s ./bin/python3.11 python
fi

cd "${PYTHON_FRAMEWORK}/bin"
[ -e python311 ] || ln -s python3.11 python311
[ -e python ] || ln -s python3.11 python

export PIP_ROOT_USER_ACTION=ignore
"${PYTHON_TOOLCACHE_ARCH}/python" -m ensurepip
"${PYTHON_TOOLCACHE_ARCH}/python" -m pip install \
  --upgrade --force-reinstall pip --disable-pip-version-check --no-warn-script-location
/bin/sh -e "${PYTHON_APP}/Install Certificates.command"
touch "${PYTHON_TOOLCACHE_VERSION}/arm64.complete"
CI_USER_SETUP

PYTHON_CHECK="$(/usr/bin/sudo -u "$CI_USER" /usr/bin/env HOME="$CI_HOME" \
  "${PYTHON_TOOLCACHE_ARCH}/python" -c 'import platform, ssl, pip; print(platform.python_version())')"
[ "$PYTHON_CHECK" = "$PYTHON_VERSION" ] || die 'Python toolcache validation failed'

# Matplotlib font cache: a cold cache makes the first backend start exceed its readiness window. The helper runs
# as the dedicated non-admin account with a clean environment and the managed Python in isolated mode; it never
# prints paths or environment values. Matplotlib is installed by the repository dependency step, so on a brand-new
# account this reports skipped_matplotlib_absent and must be rerun after the first dependency install.
FONT_CACHE_REQUIRE_ARG=""
[ "$REQUIRE_FONT_CACHE" -eq 1 ] && FONT_CACHE_REQUIRE_ARG="--require-matplotlib"
set +e
FONT_CACHE_OUTPUT="$(/usr/bin/sudo -u "$CI_USER" /usr/bin/env -i HOME="$CI_HOME" PATH=/usr/bin:/bin \
  "${PYTHON_TOOLCACHE_ARCH}/python" -I - --apply ${FONT_CACHE_REQUIRE_ARG:+"$FONT_CACHE_REQUIRE_ARG"} <"$FONT_CACHE_HELPER" 2>/dev/null)"
FONT_CACHE_RC=$?
set -e
[ "$FONT_CACHE_RC" -eq 0 ] || die "Matplotlib font-cache preseed failed: ${FONT_CACHE_OUTPUT:-no output}"
FONT_CACHE_STATE="$(printf '%s\n' "$FONT_CACHE_OUTPUT" | /usr/bin/sed -n 's/.*status=\([a-z_]*\).*/\1/p' | /usr/bin/head -n 1)"
[ -n "$FONT_CACHE_STATE" ] || die 'Matplotlib font-cache helper returned no status'
if [ "$FONT_CACHE_STATE" = "skipped_matplotlib_absent" ]; then
  printf 'WARNING: Matplotlib font cache NOT pre-built (Matplotlib is not installed for the managed Python yet).\n' >&2
  printf '         Rerun with --apply --require-font-cache after the repository dependencies are installed.\n' >&2
fi

# Validate the already-seeded host prerequisites. setup-node/setup-java will select
# their cache entries; the project-local shadow-cljs dependency remains owned by npm ci.
NODE_STATUS="deferred_to_setup-node"
NODE_BIN="$(/usr/bin/sudo -u "$CI_USER" /usr/bin/find -L "${TOOLCACHE_ROOT}/node" \
  -type f -path '*/bin/node' -perm -111 2>/dev/null | /usr/bin/head -n 1 || true)"
if [ -n "$NODE_BIN" ]; then
  NODE_VERSION="$(/usr/bin/sudo -u "$CI_USER" /usr/bin/env HOME="$CI_HOME" "$NODE_BIN" --version)"
  case "$NODE_VERSION" in
    v22.*) NODE_STATUS="verified_${NODE_VERSION}" ;;
    *) die 'existing Node toolcache is not major version 22' ;;
  esac
fi

JAVA_STATUS="deferred_to_setup-java"
JAVA_BIN="$(/usr/bin/sudo -u "$CI_USER" /usr/bin/find -L "$TOOLCACHE_ROOT" \
  -type f -path '*/bin/java' -perm -111 2>/dev/null | /usr/bin/head -n 1 || true)"
if [ -n "$JAVA_BIN" ]; then
  JAVA_MAJOR="$(/usr/bin/sudo -u "$CI_USER" /usr/bin/env HOME="$CI_HOME" "$JAVA_BIN" -version 2>&1 \
    | /usr/bin/awk -F '[\".]' '/version/ {print $2; exit}')"
  [ "$JAVA_MAJOR" = "21" ] || die 'existing Java toolcache is not major version 21'
  JAVA_STATUS="verified_major_21"
fi

KONDO_ZIP="${TMP_DIR}/clj-kondo.zip"
KONDO_DIR="${TMP_DIR}/clj-kondo"
/usr/bin/curl --fail --location --silent --show-error --output "$KONDO_ZIP" "$CLJ_KONDO_URL"
KONDO_SHA="$(/usr/bin/shasum -a 256 "$KONDO_ZIP" | /usr/bin/awk '{print $1}')"
[ "$KONDO_SHA" = "$CLJ_KONDO_SHA256" ] || die 'clj-kondo checksum mismatch'
/bin/mkdir -p "$KONDO_DIR"
/usr/bin/unzip -q "$KONDO_ZIP" -d "$KONDO_DIR"
[ -f "${KONDO_DIR}/clj-kondo" ] || die 'clj-kondo executable missing from archive'
/bin/chmod +x "${KONDO_DIR}/clj-kondo"
KONDO_VERSION_OUTPUT="$("${KONDO_DIR}/clj-kondo" --version)"
printf '%s\n' "$KONDO_VERSION_OUTPUT" | /usr/bin/grep -F "$CLJ_KONDO_VERSION" >/dev/null \
  || die 'clj-kondo version validation failed'

MANIFEST_TMP="${TMP_DIR}/python-preseed-manifest.txt"
cat >"$MANIFEST_TMP" <<EOF
python_version=${PYTHON_VERSION}
python_release=${PYTHON_RELEASE}
python_archive_sha256=${PYTHON_ARCHIVE_SHA256}
python_setup_sha256=${PYTHON_SETUP_SHA256}
python_pkg_sha256=${PYTHON_PKG_SHA256}
toolcache_marker=${PYTHON_TOOLCACHE_VERSION}/arm64.complete
node_status=${NODE_STATUS}
java_status=${JAVA_STATUS}
matplotlib_font_cache=${FONT_CACHE_STATE}
clj_kondo_version=${CLJ_KONDO_VERSION}
clj_kondo_sha256=${CLJ_KONDO_SHA256}
runner_started=false
workflow_changed=false
EOF
/usr/bin/sudo /usr/bin/install -o "$CI_USER" -g "$CI_GROUP" -m 0600 "$MANIFEST_TMP" "$MANIFEST"

printf '\nTOOLCHAIN PRESEED PASSED\n'
printf '  Python: %s (setup-python cache complete)\n' "$PYTHON_VERSION"
printf '  Matplotlib font cache: %s\n' "$FONT_CACHE_STATE"
printf '  Node:   %s\n' "$NODE_STATUS"
printf '  Java:   %s\n' "$JAVA_STATUS"
printf '  CLJS:   host preflight passed; project dependencies remain npm-ci managed\n'
printf '  Runner: stopped; this script did not start or register it\n'
printf '  GitHub: unchanged; this script did not enable or rerun a workflow\n'
