from __future__ import annotations

import re
import shutil
import subprocess
from collections import namedtuple

Finding = namedtuple("Finding", "id severity line message")
Probe = namedtuple("Probe", "name status detail")

_RULES = [
    ("runs-on-ubuntu", "info", r"runs-on:\s*.*ubuntu", "runs-on targets a GitHub-hosted Ubuntu image; switch to the self-hosted label set (see examples/)."),
    ("apt-get", "blocker", r"\bapt(?:-get)?\s+(?:install|update|remove)", "apt is Linux-only; install the tool with Homebrew on the host instead and have `doctor` verify it."),
    ("dpkg", "blocker", r"\bdpkg\b", "dpkg is Linux-only."),
    ("sudo", "blocker", r"\bsudo\b", "the runner must never need root; remove sudo."),
    ("home-runner", "blocker", r"/home/runner", "hard-coded Linux runner path; use $HOME / ${{ runner.temp }}."),
    ("container", "blocker", r"^\s*(?:container|services):|docker://", "container jobs/services are not supported on macOS runners."),
    ("runner-os-linux", "warning", r"runner\.os\s*==\s*'Linux'", "Linux-only conditional: the step will be skipped on macOS (silently) - decide explicitly."),
    ("sed-i", "warning", r"\bsed\s+-i\b", "BSD sed needs `-i ''`; use gsed or `-i.bak`."),
    ("readlink-f", "warning", r"\breadlink\s+-f\b", "BSD readlink lacks -f on older macOS; use realpath/greadlink."),
    ("date-d", "warning", r"\bdate\s+-d\b", "BSD date lacks -d; use gdate."),
    ("stat-c", "warning", r"\bstat\s+-c\b", "BSD stat uses -f."),
    ("grep-P", "warning", r"\bgrep\s+-[a-zA-Z]*P", "BSD grep has no -P; use ggrep."),
    ("xargs-r", "warning", r"\bxargs\s+-r\b", "BSD xargs has no -r."),
    ("find-printf", "warning", r"\bfind\b.*-printf", "BSD find has no -printf."),
    ("shell-bash", "info", r"shell:\s*bash", "macOS /bin/bash is 3.2; `shell: bash` resolves via PATH - ensure Homebrew bash>=4 is first."),
]


def scan_workflow_text(text: str):
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for rid, sev, pat, msg in _RULES:
            if re.search(pat, line):
                out.append(Finding(rid, sev, n, msg))
    return out


def _bash_version():
    try:
        r = subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"], capture_output=True, text=True, timeout=10)
        a, b = r.stdout.split()
        return int(a), int(b)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def probe_host(which=shutil.which, bash_version=_bash_version):
    res = []
    bv = bash_version()
    res.append(Probe("bash>=4", "ok" if bv and bv[0] >= 4 else "missing", f"bash {bv[0]}.{bv[1]}" if bv else "bash not found"))
    for name, exe in [("gnu-sed", "gsed"), ("gnu-coreutils", "gdate"), ("gnu-grep", "ggrep"), ("node", "node"), ("java", "java"),
                      ("python3", "python3"), ("git", "git"), ("ssh", "ssh")]:
        p = which(exe)
        res.append(Probe(name, "ok" if p else "missing", p or f"{exe} not on PATH"))
    return res
