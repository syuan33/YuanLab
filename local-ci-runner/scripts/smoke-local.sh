#!/bin/sh
# Local smoke test: builds a fully synthetic repo in a temp dir and runs the real engine against it.
# No network, no real repository, no runner registration, no launchd. Safe to run on any workstation (not as root).
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
LOCAL_CI_RUNNER_SMOKE_TMP="$tmp" LOCAL_CI_RUNNER_HERE="$here" "${PYTHON:-python3}" -s - <<'PY'
import os, sys
sys.path[:0] = [os.environ["LOCAL_CI_RUNNER_HERE"] + "/src", os.environ["LOCAL_CI_RUNNER_HERE"]]
from pathlib import Path
from local_ci_runner.config import Paths
from local_ci_runner.engine import Engine
from local_ci_runner.gitops import resolve_ref
from tests.fixtures import Remote, synth_config
tmp = Path(os.environ["LOCAL_CI_RUNNER_SMOKE_TMP"])
remote = Remote(tmp, "echo 'Ran 1 tests'; echo OK")
sha = resolve_ref(remote.url, "main")
eng = Engine(Paths(tmp / "home", launch_agents_dir=tmp / "LA"), remote_factory=lambda r: remote.url, strict_identity=False, config=synth_config())
rep = eng.run("example-org/example-repo", sha)  # name is only an allow-list label; the remote is the synthetic local repo
print(rep["summary"])
sys.exit(0 if rep["status"] == "passed" else 1)
PY
echo "smoke test OK (synthetic only; nothing was registered or installed)"
