#!/bin/sh
# Local smoke test: builds a fully synthetic repo in a temp dir and runs the real engine against it.
# No network, no REPLI, no runner registration, no launchd. Safe to run on any workstation (not as root).
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
REPLI_LOCAL_CI_SMOKE_TMP="$tmp" REPLI_LOCAL_CI_HERE="$here" "${PYTHON:-python3}" -s - <<'PY'
import os, sys
sys.path[:0] = [os.environ["REPLI_LOCAL_CI_HERE"] + "/src", os.environ["REPLI_LOCAL_CI_HERE"]]
from pathlib import Path
from repli_local_ci.config import Paths
from repli_local_ci.engine import Engine
from repli_local_ci.gitops import resolve_ref
from tests.fixtures import Remote
tmp = Path(os.environ["REPLI_LOCAL_CI_SMOKE_TMP"])
remote = Remote(tmp, "echo 'Ran 1 tests'; echo OK")
sha = resolve_ref(remote.url, "main")
eng = Engine(Paths(tmp / "home", launch_agents_dir=tmp / "LA"), remote_factory=lambda r: remote.url, strict_identity=False)
rep = eng.run("syuan33/repli", sha)  # name is only an allow-list label; the remote is the synthetic local repo
print(rep["summary"])
sys.exit(0 if rep["status"] == "passed" else 1)
PY
echo "smoke test OK (synthetic only; nothing was registered or installed)"
