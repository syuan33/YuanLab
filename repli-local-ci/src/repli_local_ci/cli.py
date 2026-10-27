from __future__ import annotations

import argparse
import os
import platform
import signal
import subprocess
import sys
import threading
from pathlib import Path

from . import compat, gitops, launchd, safety
from .config import ALLOWED_REPOS, DEFAULT_TIMEOUT, Paths
from .engine import Engine
from .queue import JobQueue
from .runner_install import InstallError, Installer, Pin

PIN_FILE = Path(__file__).resolve().parents[2] / "runner-pin.json"
REPO = "syuan33/repli"
EXIT = {"passed": 0, "failed": 1, "error": 2, "timeout": 124, "cancelled": 130, "superseded": 75, "busy": 75, "duplicate": 75}


def _mode(a, name):
    if a.apply and a.dry_run:
        raise SystemExit(f"{name}: --dry-run and --apply are mutually exclusive")
    return bool(a.apply)


def _add_mode(sp, default_note="default"):
    sp.add_argument("--dry-run", action="store_true", help="print the plan only (the default)")
    sp.add_argument("--apply", action="store_true", help="actually perform the side effects")


def _installer(paths):
    return Installer(paths, Pin.load(PIN_FILE))


def _plan(title, lines):
    print(f"DRY RUN: {title} (no changes made; pass --apply to execute)")
    for l in lines:
        print(f"  - {l}")


def cmd_doctor(a, paths):
    bad = 0

    def row(ok, name, detail, hard=True):
        nonlocal bad
        tag = "ok  " if ok else ("FAIL" if hard else "warn")
        bad += (not ok and hard)
        print(f"[{tag}] {name}: {detail}")

    row(os.geteuid() != 0, "not root", f"euid={os.geteuid()}")
    mac = platform.system() == "Darwin" and platform.machine() == "arm64"
    row(mac, "platform", f"{platform.system()}/{platform.machine()} (supported: Darwin/arm64)", hard=False)
    row(sys.version_info >= (3, 9), "python>=3.9", sys.version.split()[0])
    try:
        pin = Pin.load(PIN_FILE)
        row(pin.pinned, "runner pin", f"v{pin.version} sha256={'set' if pin.pinned else 'NOT PINNED (install --apply will refuse)'}", hard=False)
    except Exception as e:
        row(False, "runner pin", str(e))
    if paths.known_hosts.exists():
        try:
            gitops.ensure_known_hosts(paths.known_hosts)
            row(True, "known_hosts", "matches pinned GitHub ed25519 key")
        except gitops.GitError as e:
            row(False, "known_hosts", str(e))
    if paths.home.exists():
        mode = paths.home.stat().st_mode & 0o777
        row(mode & 0o077 == 0, "state dir mode", f"{oct(mode)}")
    for pr in compat.probe_host():
        row(pr.status == "ok", f"tool {pr.name}", pr.detail, hard=False)
    if a.workflow:
        for f in compat.scan_workflow_text(Path(a.workflow).read_text()):
            print(f"[{f.severity:7}] {a.workflow}:{f.line} {f.id}: {f.message}")
            bad += f.severity == "blocker"
    return 1 if bad else 0


def cmd_install(a, paths):
    inst = _installer(paths)
    if not _mode(a, "install"):
        _plan("install", inst.plan_install())
        return 0
    inst.apply_install()
    print(f"installed runner into {paths.runner_dir}; service NOT started, runner NOT registered")
    return 0


def cmd_register(a, paths):
    inst = _installer(paths)
    if not _mode(a, "register"):
        _plan("register", inst.plan_register(a.repo))
        return 0
    token = sys.stdin.readline().strip() if a.token_stdin else os.environ.get("REPLI_RUNNER_TOKEN", "")
    inst.apply_register(a.repo, token, a.i_have_admin_approval)
    print("registered (token not stored by this tool)")
    return 0


def cmd_uninstall(a, paths):
    inst = _installer(paths)
    if not _mode(a, "uninstall"):
        _plan("uninstall", inst.plan_uninstall())
        return 0
    inst.apply_uninstall()
    print("uninstalled service + runner directory; deregister the runner on GitHub manually")
    return 0


def cmd_start(a, paths):
    inst = _installer(paths)
    if a.dry_run:
        _plan("start", inst.plan_start())
    else:
        inst.apply_start()
        print("started")
    return 0


def cmd_stop(a, paths):
    inst = _installer(paths)
    if a.dry_run:
        _plan("stop", inst.plan_stop())
        return 0
    for j in JobQueue(paths.queue).list() if paths.queue.exists() else []:
        if j["state"] in ("queued", "running"):
            JobQueue(paths.queue).request_cancel(j["repo"], j["sha"])  # cooperative: run processes stop their own group
    inst.apply_stop()
    print("stopped (launchd label only; in-flight local runs were asked to cancel)")
    return 0


def cmd_status(a, paths):
    print(f"home        : {paths.home} ({'exists' if paths.home.exists() else 'absent'})")
    print(f"installed   : {paths.runner_dir.exists() and paths.plist.exists()}")
    print(f"registered  : {(paths.runner_dir / '.runner').exists()}")
    if platform.system() == "Darwin":
        r = subprocess.run(launchd.print_cmd(os.getuid()), capture_output=True, text=True)
        print(f"launchd     : {'loaded' if r.returncode == 0 else 'not loaded'}")
    else:
        print("launchd     : n/a on this platform")
    jobs = JobQueue(paths.queue).list() if paths.queue.exists() else []
    active = [j for j in jobs if j["state"] in ("queued", "running")]
    print(f"queue       : {len(active)} active")
    for j in active:
        print(f"  {j['id']} {j['state']:8} {j['repo']}@{j['sha'][:12]} ref={j.get('ref')}")
    return 0


def cmd_run(a, paths):
    safety.validate_repo(a.repo)
    eng = Engine(paths, ssh_key=os.environ.get("REPLI_LOCAL_CI_SSH_KEY"))
    sha = a.sha
    if a.branch:
        sha = gitops.resolve_ref(eng.remote_factory(a.repo), a.branch, env=gitops.git_env(paths.known_hosts))
        print(f"resolved {a.branch} -> {sha} (pinned for this run)")
    if not sha:
        raise SystemExit("run: --sha (or --branch) is required")
    ev = threading.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: ev.set())
    rep = eng.run(a.repo, sha, ref=a.branch or a.ref, head_repo=a.head_repo, base_sha=a.base_sha, timeout=a.timeout,
                  cancel_event=ev, keep_workspace=a.keep_workspace, wait_seconds=a.wait)
    print(rep.get("summary") or f"status: {rep['status']}")
    if rep.get("report_dir"):
        print(f"report: {rep['report_dir']}/report.json")
    return EXIT.get(rep["status"], 2)


def cmd_cancel(a, paths):
    safety.validate_repo(a.repo)
    safety.validate_sha(a.sha)
    hit = JobQueue(paths.queue).request_cancel(a.repo, a.sha) if paths.queue.exists() else False
    print("cancel requested" if hit else "no active job for that repo/sha")
    return 0 if hit else 1


def build_parser():
    p = argparse.ArgumentParser(prog="repli-local-ci", description="Safe local CI manager around the official GitHub self-hosted runner.")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="read-only environment checks"); d.add_argument("--workflow")
    for n, h in (("install", "download+verify runner, write LaunchAgent"), ("register", "register runner (admin token, manual gate)"),
                 ("uninstall", "remove service + runner dir")):
        sp = sub.add_parser(n, help=h); _add_mode(sp)
        if n == "register":
            sp.add_argument("--repo", default=REPO)
            sp.add_argument("--token-stdin", action="store_true")
            sp.add_argument("--i-have-admin-approval", action="store_true")
    for n in ("start", "stop"):
        sp = sub.add_parser(n, help=f"{n} the launchd service"); sp.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="show state")
    r = sub.add_parser("run", help="run dev/verify.sh for an exact SHA in a fresh workspace")
    r.add_argument("--repo", required=True); g = r.add_mutually_exclusive_group()
    g.add_argument("--sha"); g.add_argument("--branch", help="owner branch, resolved once and pinned")
    r.add_argument("--ref"); r.add_argument("--head-repo"); r.add_argument("--base-sha")
    r.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT); r.add_argument("--wait", type=int, default=0)
    r.add_argument("--keep-workspace", action="store_true")
    c = sub.add_parser("cancel", help="cancel a queued/running job"); c.add_argument("--repo", required=True); c.add_argument("--sha", required=True)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    paths = Paths.from_env()
    fn = globals()[f"cmd_{a.cmd}"]
    try:
        return fn(a, paths)
    except (safety.SafetyError, InstallError, gitops.GitError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
