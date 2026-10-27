# repli-local-ci

Run REPLI's canonical `dev/verify.sh -p standard -o <dir>` on your own Apple Silicon Mac through the **official GitHub
Actions self-hosted runner**, wrapped by a small stdlib-only Python manager that adds pinned install, a registration gate,
launchd lifecycle, exact-SHA isolated runs, queueing, redaction and reports. See
[ADR 0001](docs/adr/0001-official-runner-vs-custom-poller.md), [THREAT-MODEL.md](THREAT-MODEL.md), [RUNBOOK.md](RUNBOOK.md).

> **Warning:** a self-hosted runner executes repository code on your machine and is **not** equivalent to a GitHub-hosted
> VM. Local results are **not** the REPLI canonical merge gate until REPLI governance approves that.

Status: reviewed-by-nobody yet. Nothing here modifies REPLI, registers a runner, installs a daemon or touches secrets.
Not supported: Intel/Linux for the service (tests and `run` work on Linux for development).

## Requirements
macOS on Apple Silicon, `python3` >= 3.9, `git`, `ssh`; Homebrew `bash`, `gnu-sed`, `coreutils`, `grep`, plus whatever
`dev/verify.sh` needs (Node, Java, Python, clj-kondo). Run as a normal user, never root.

## Use
```
bin/repli-local-ci doctor [--workflow path/to/workflow.yml]
bin/repli-local-ci install   [--dry-run | --apply]
bin/repli-local-ci register  [--dry-run | --apply --i-have-admin-approval [--token-stdin]]
bin/repli-local-ci start | stop | status        # start/stop accept --dry-run
bin/repli-local-ci run --repo syuan33/repli --sha <40-hex> [--base-sha <40-hex>] [--ref <branch>] [--timeout S] [--wait S]
bin/repli-local-ci run --repo syuan33/repli --branch <owner-branch>   # resolved once, then pinned
bin/repli-local-ci cancel --repo syuan33/repli --sha <40-hex>
bin/repli-local-ci uninstall [--dry-run | --apply]
```
`install`, `register`, `uninstall` are **dry-run unless `--apply`**. `install --apply` refuses until `runner-pin.json` has
a real SHA-256 (copy it from the official actions/runner release notes for that exact asset in a reviewed PR).
`register --apply` needs a repo-admin registration token in `REPLI_RUNNER_TOKEN` (or stdin); it is passed to
`config.sh` through its environment only.

Each `run` writes `~/.repli-local-ci/reports/<run-id>/{report.json,summary.txt,verify.log}` with repository, head/base SHA,
start/end, command, exit code, heuristic test summary, tool versions and SHA-256 of log and result files. Exit codes:
0 passed, 1 failed, 2 error/refused, 124 timeout, 130 cancelled, 75 superseded/busy/duplicate.

Lifecycle: pause = `stop`, resume = `start`, upgrade = see RUNBOOK, uninstall = `uninstall --apply` (+ deregister on GitHub).

## Tests
```
cd repli-local-ci
PYTHONPATH=src:. python3 -m unittest discover -s tests -t . -v     # synthetic fixtures only
./scripts/smoke-local.sh                                           # synthetic end-to-end run; no registration
```
CI tests never contact REPLI, register a runner or run a GitHub job. Runner registration stays a manual gate.

## GitHub-hosted vs macOS self-hosted
| | ubuntu-24.04 hosted | your Mac |
|---|---|---|
| Isolation | fresh VM per job | your user account, persistent disk |
| Shell/userland | bash 5, GNU coreutils/sed/grep/date | `/bin/bash` 3.2, BSD tools (install GNU via Homebrew, put first on PATH) |
| Packages | `apt`, `sudo` | Homebrew, no sudo |
| Node / Java / Python | image-provided | host-provided; pin versions yourself |
| clj-kondo | install step | `brew install borkdude/brew/clj-kondo` |
| Containers/services | supported | not on macOS runners |
| Private submodules | `GITHUB_TOKEN`/deploy key in checkout | ssh with your agent/deploy key; must be in the submodule allow-list (default none) |

`doctor --workflow` flags Linux-only constructs (apt, sudo, `/home/runner`, containers, GNU-only flags); fix them, don't skip them.

## Layout
`src/repli_local_ci/` package · `tests/` · `examples/repli-verify-workflow.patch` (illustrative, not applied) ·
`runner-pin.json` · `scripts/smoke-local.sh`.
