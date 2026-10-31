# local-ci-runner

Run a repository's required CI check on your own Apple Silicon Mac through the **official GitHub Actions self-hosted runner**,
wrapped by a small stdlib-only Python manager. Repository-neutral: every example uses synthetic names
(`example-org/example-repo`, `SUBMODULE_A`, `SECRET_A`).

| Owned by the official runner / GitHub | Owned by `local-ci-runner` |
|---|---|
| job delivery, the check identity, re-run button | pinned + checksum-verified runner install |
| `actions/checkout` of GitHub's PR **integration (merge) commit** (`GITHUB_SHA`) | registration gate that creates the protected allow-list |
| artifacts, secrets, timeouts, cancellation | launchd LaunchAgent lifecycle, `doctor` readiness |
| | runner hooks (allow-list enforcement + cleanup), local reports |

> **Warning.** A self-hosted runner executes repository code on your machine; it is **not** a throw-away hosted VM. A
> **dedicated standard macOS user or disposable VM is a mandatory production precondition.** Never attach it to a public
> repository that accepts fork PRs. The runner installation and Python package/toolcache preseed have been exercised on a dedicated
> non-admin Apple Silicon account; the complete restartable script has also passed its no-change dry-run. None of that makes a local
> result a repository's required merge gate.

Docs: [ADR 0001](docs/adr/0001-official-runner-vs-custom-poller.md) · [THREAT-MODEL.md](THREAT-MODEL.md) · [RUNBOOK.md](RUNBOOK.md)

## Protected allow-list
The repository allow-list exists in exactly one place: `~/.local-ci-runner/config.json` (mode 0600, directory 0700, owned by you,
schema + checksum validated, exactly one `OWNER/REPO`). It is created exclusively by `register --apply`. Workflows, PR content
and environment variables cannot widen it; the installed hooks take their state directory from a baked-in `--home`, never from the
environment. If the file is missing, a symlink, mis-permissioned, foreign-owned or edited, every hook and `run` fails closed.
(The checksum catches accidental/naive edits; a same-user attacker can recompute it - see the threat model.)

## Dedicated account precondition
The runner account must hold no personal SSH keys, browser sessions, cloud-drive data or private credentials beyond the
repository-scoped runner credential and any read-only deploy keys. `doctor` fails (and `register --apply` / `start` refuse) when the
user is root or admin, well-known strong personal-data indicators exist (personal SSH private keys, `~/.aws`, gcloud/gh/Docker/kube credentials, `~/.netrc`, Chrome/Firefox user profiles, iCloud/Mobile Documents and cloud-drive folders; existence only, contents are never read), or `attest-dedicated-user --apply`
has not been run on that account. macOS creates `~/Library/Safari`, `~/Library/Cookies` and the login keychain by itself at the first GUI login (which this tool requires), so the existence of those system-created locations is deliberately *not* treated as evidence of personal data; they are never opened, listed or read, and the operator attests (`attest-dedicated-user`) that the account has no personal browser session, cookies or credentials. The macOS login keychain (`~/Library/Keychains/login.keychain-db`) is created by the system at the account's first GUI login, which this tool requires, so an empty/system-created login keychain is **expected** and its existence is deliberately *not* treated as evidence of personal credentials. The tool never reads, lists, unlocks or inspects keychain contents; the operator attests (`attest-dedicated-user`) that it holds no personal or unrelated credentials.

## Install (on the dedicated account)
```
git clone <this repository> && cd <repo>/local-ci-runner
# first: log in to the dedicated user's macOS desktop (GUI session) - the LaunchAgent loads into gui/<uid>
bin/local-ci-runner install --dry-run            # default; prints the plan
bin/local-ci-runner attest-dedicated-user --apply
bin/local-ci-runner install --apply              # pinned runner, ~/.local/bin launcher, hooks, LaunchAgent (not started)
export PATH="$HOME/.local/bin:$PATH"
local-ci-runner doctor                           # must print READY (exit 0)
LOCAL_CI_RUNNER_TOKEN=... local-ci-runner register --apply --repo OWNER/REPO --i-have-admin-approval   # MANUAL GATE
local-ci-runner start
```
**`start` needs an active GUI login session for the dedicated user** (the LaunchAgent is loaded into `gui/<uid>`). If there is none, `start` fails with
"the dedicated user must be logged in" instead of a bare launchctl error. Keep that session logged in (fast-user-switching away is fine; logging out stops the service).

The manager **and the exact reviewed `runner-pin.json`** are copied to `~/.local-ci-runner/app` (the pin read-only, its SHA-256 recorded in
`~/.local-ci-runner/manifest.json`), and a launcher is written to `~/.local/bin/local-ci-runner`; workflows and hooks use
it, not the source checkout. The installed CLI (launcher, `doctor`, `register`, `start`, `verify-pin`) uses **only** that installed pin and verifies it against the manifest on every
run - a missing, symlinked, modified or mismatched pin is a hard failure, and there is no fallback to a source checkout or any other path - so the source checkout can be
deleted after installing. `install --apply` refuses (before any side effect) if the source pin is missing, a symlink, malformed or different from the pin used for the download.
Uninstall removes the pin only as part of the ownership-checked app removal. Uninstall removes the launcher only if it is byte-identical to the one in the manifest.

## Preseed the macOS language toolchain

`actions/setup-node` and `actions/setup-java` can populate their user-writable caches without administrator privileges. The official
macOS package used by `actions/setup-python`, however, invokes Apple's system installer the first time a Python version is absent.
A non-interactive runner under a standard account cannot answer that administrator prompt, so a job may stop at `Set up Python` even
after Node and Java succeeded.

The included `scripts/preseed-macos-ci-toolchain.sh` handles that one host-level exception without making the runner account an
administrator. Run it from a separate macOS administrator account while the runner is stopped:

```bash
# No persistent change: validates the account, runner state and pinned public assets.
./scripts/preseed-macos-ci-toolchain.sh --ci-user <dedicated-ci-user>

# Explicit human gate: prompts locally for the administrator password.
./scripts/preseed-macos-ci-toolchain.sh --ci-user <dedicated-ci-user> --apply
```

The script pins and verifies the official Python `3.11.9-9947079978` Apple Silicon release, installs only the framework,
applications/certificate helper and pip, and creates the exact setup-python cache entry under the managed runner work directory.
It disables the package's shell-profile updater, `/usr/local/bin` compatibility shims and offline documentation. Only the Python 3.11
framework is transferred to the dedicated account; the account remains non-admin and receives no sudo rule.

With `--apply` it also pre-builds the Matplotlib font cache for the managed Python (private `0700` cache, rebuilt only when stale: version change, missing/corrupt stamp or unusable font list; skipped with a notice until Matplotlib is installed). See the RUNBOOK for details.

Node 22 and Java 21 are verified when their caches already exist. On a fresh host, their absence is reported as deferred—not as a
failure—because the pinned `setup-node` and `setup-java` workflow actions will populate them without privilege. clj-kondo is downloaded
to a temporary directory, checksum-verified and executed once, then removed. ClojureScript/shadow-cljs remains a locked project
dependency installed by `npm ci`; do not add a global Clojure or shadow-cljs installation just to make this preflight green.

The operation is intentionally restartable. If Python installation succeeds but a later Node/Java/clj-kondo check fails, run the same
`--apply` command again after correcting the check: the completed exact Python toolcache is verified and the package reinstall is
skipped. Success ends with `TOOLCHAIN PRESEED PASSED`. The script never registers/starts the runner, creates a GitHub token, enables a
workflow, or runs repository code.

## Deployment topology: local operator versus a remote M1/Orca executor

Running these commands yourself while logged in as the dedicated runner account is not the same deployment shape as asking an
agent running in a worktree to deploy the runner. Keep these three boundaries separate:

| Boundary | Example location | What belongs there |
|---|---|---|
| M1/orchestrator control plane | an operator account on `192.168.x.y` | a private SSH alias, its identity key and pinned `known_hosts`; optional private routing metadata |
| repository worktree | an Orca/Git worktree | reviewed source, tests and non-secret evidence only |
| dedicated runner account | a standard macOS account on the CI Mac | the installed manager, official runner, protected allow-list, LaunchAgent, work directory and reports |

For a direct local install, the operator is already logged in as the dedicated runner account, so `install`, `doctor`, `register`
and `start` all run in the same account. For an M1/Orca-controlled install, M1 normally runs as a different OS user and may be on a
different Mac. It must first use its own Home-private SSH configuration to reach the dedicated runner account, then run the same
commands remotely. The worktree is only the source/evidence plane; it is not a credential shuttle.

For example, an orchestrator on `192.168.x.y` might have an SSH alias named `ci-runner-private` that resolves to a dedicated CI Mac.
The literal alias, LAN address, local usernames, identity path and host key are operator-private values. They must not be committed,
copied into a worktree, included in an Issue/PR/checkpoint, or inferred from another machine's alias. An alias is local to the OS user
whose OpenSSH configuration defines it; an alias that works on an administrator's Mac need not exist in an Orca executor's Home.

Some orchestrators keep only the alias name in a Home-private routing file, for example:

```clojure
{:ci-ssh-alias "ci-runner-private"}
```

That file is an **orchestration convention, not a `local-ci-runner` requirement**. If used, keep it outside the repository with mode
0600 in a mode-0700 directory. Before requesting a registration token, the orchestrator should confirm, without printing private
values: current executor Home, routing file readable, alias parsed, OpenSSH Host entry loaded, pinned-host-key BatchMode SSH succeeds.
Any failure stops before token creation. Do not fix Home-access problems by copying, symlinking or bind-mounting `~/.ssh` or other
private routing files into the worktree; instead repair the executor's host-level Home permissions/configuration.

Non-interactive SSH shells often do not read the dedicated account's interactive shell profile, so `command -v local-ci-runner`
may fail even after a correct managed install. This alone is not evidence that the installation is missing. The installer contract
writes the stable launcher to `$HOME/.local/bin/local-ci-runner`; remote automation should invoke that launcher explicitly (or set the
documented minimal PATH for that one remote command), then let `doctor` verify its manifest and installed manager. Do not search other
accounts or guess additional installation directories. If the managed launcher itself is absent or non-executable, stop before token
creation and return to the installation gate.

A repository registration token is short-lived control-plane data. Generate it immediately before use and stream it to
`register --token-stdin`; do not place it in argv, a file, chat, worktree, checkpoint or CI log. Public evidence should contain only
repository-neutral outcomes such as exact source SHA, `doctor`/`status` result, runner online/idle state, required labels, workflow
run ID and check conclusion.

Runner pin: `runner-pin.json` = v2.338.2, SHA-256 `25a065af…19a388`. Versions are never chosen at runtime; `verify-pin` (also a CI job) fails
if the pin stops matching official release metadata.

**No runner auto-update.** `register` always passes `--disableupdate` to the official `config.sh` (the displayed plan and the real call use the
same argv), so the checksum-verified runner can never replace itself. `doctor` and `start` (and `register`) additionally run the installed
runner's own version command (`bin/Runner.Listener --version`, no network) and **fail closed** unless it prints exactly the pinned version -
a mismatch, a missing/non-executable runner or malformed output all mean NOT READY / refuse.

**Upgrades are explicit, reviewed pin changes:** change `runner-pin.json` (version, asset, url, sha256 checked against the official release)
in a reviewed PR, get `verify-pin` green, then `stop` → `uninstall --apply` → `install --apply` → re-register (new admin token) → `start`.
Stale-runner consequence: GitHub expects self-hosted runners to stay reasonably current: a runner that has fallen too far behind the latest official release may be refused jobs (commonly described as roughly 30 days after a new release - confirm the current rule in GitHub's runner documentation, it was not verified here). With `--disableupdate` the runner will not fix this by itself, so an unreviewed-but-stale runner is an availability risk that you trade for a runner binary that never changes without review.

## Commands
```
doctor [--workflow FILE]         read-only; exit 1 if ANY mandatory prerequisite fails
install | uninstall              dry-run unless --apply
register --repo OWNER/REPO [--verify-command "ci/verify.sh -o {results_dir}"]
                                 dry-run unless --apply; --apply also needs --i-have-admin-approval and a token (env or --token-stdin)
attest-dedicated-user            dry-run unless --apply
start | stop [--dry-run] | status        start also refuses unless the installed runner version equals the pin
clean [--apply]                  empty the managed work dir (refuses while a job runs)
report --results-dir D --job-status S     local report for the current CI job
hook job-started|job-completed --home DIR  called by the runner, not by hand
verify-pin                       compare runner-pin.json with official release metadata (network)
run --repo OWNER/REPO --sha <40-hex>       STANDALONE exact-SHA verification (not the CI path)
cancel --repo OWNER/REPO --sha <40-hex>
logs --follow                    read-only: validated, sanitized stage events of the current job (logs/<run-id>/<job-id>/timeline.log); `status` shows the latest
```
**`doctor` mandatory checks (any failure ⇒ exit 1):** Darwin/arm64; not root; non-admin; no personal-data paths + attestation;
Python ≥ 3.9; `git ≥ 2.32`, `ssh`, `ssh-keyscan`, `ssh-keygen`, `curl`, `unzip`, `shasum`, `awk`, `tr`; runner SHA-256 populated; managed
launcher on `PATH` and unmodified; no blocker findings in the scanned workflow (default: `examples/example-workflow.yml`);
installed runner version == `runner-pin.json` (when a runner is installed); allow-list config valid *when present*; pinned `known_hosts`; state-dir mode; runner-dir ownership. **Warnings never change the exit
code:** GNU tools, Homebrew bash ≥ 4, host node/java. Running `doctor` on Linux or a personal account exits 1 by design.

### Runner hooks (runner configuration, not workflow content)
- `job-started`: loads the protected config (fail closed), then requires: repo in allow-list; event `pull_request` on a `refs/pull/N/merge`
  ref with `head.repo == base.repo == repo` and `fork == false`, or `push` to a configured ref; full `GITHUB_SHA`; not root; workspace inside the
  marker-owned work dir. Refusal **fails** the job.
- `job-completed`: empties the checkout and deletes credential-shaped leftovers inside the managed work dir; never follows symlinks.

## Workflow shape
`examples/example-workflow.yml` (synthetic) has exactly **one job and one check context, `verify`**, no job-level `if:`, and no second
same-named job: a skipped required check would pass, so refusals must fail rather than skip. It shows: readiness step, plain
`actions/checkout` (`persist-credentials: false`) plus a `HEAD == GITHUB_SHA` assertion, an optional private-submodule step using a
per-step `$RUNNER_TEMP` directory (`SECRET_A`, `SUBMODULE_A`), the repository's own verify entrypoint, artifact upload and a local report. Third-party actions
are pinned to full commit SHAs. Nothing applies it for you.

### Hosted vs your Mac
| | hosted ubuntu | dedicated Mac user |
|---|---|---|
| Isolation | fresh VM per job | one account / VM, persistent disk + caches |
| Userland | GNU tools, bash 5 | BSD tools, bash 3.2 (steps here are 3.2-safe) |
| Packages | apt, privileged installs | none; use setup actions / curl + shasum / Homebrew |
| Containers | supported | not supported |
| Submodule keys | on the VM | per-step `$RUNNER_TEMP`, removed on exit and by the hook |

## Tests
```
cd local-ci-runner
PYTHONPATH=src:. python3 -X dev -W error -m unittest discover -s tests -t . -v
./scripts/smoke-local.sh        # PYTHON=/usr/bin/python3 on macOS
```
All tests are synthetic (no network, no runner, no real repository). `tests/fixtures.StrictCase` fails any test that leaks a process, file
or socket (plain `-W error` does not, because ResourceWarning is raised in finalizers); `test_strict.py` proves it. CI runs Linux Python 3.9 +
3.13, hosted `macos-14` arm64 with Apple's `/usr/bin/python3`, and `pin-check`. **Minimum Python is 3.9** (Apple Command Line Tools).

## Before real use
Independent review; test on a disposable runner/VM; a reviewed workflow change in the target repository; explicit approval to register and enable.
