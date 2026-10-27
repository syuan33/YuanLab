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
> repository that accepts fork PRs. This tool has not been run against a real runner. Local results are not your required merge gate.

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
user is root or admin, well-known personal-data paths exist (existence only; contents are never read), or `attest-dedicated-user --apply`
has not been run on that account.

## Install (on the dedicated account)
```
git clone <this repository> && cd <repo>/local-ci-runner
bin/local-ci-runner install --dry-run            # default; prints the plan
bin/local-ci-runner attest-dedicated-user --apply
bin/local-ci-runner install --apply              # pinned runner, ~/.local/bin launcher, hooks, LaunchAgent (not started)
export PATH="$HOME/.local/bin:$PATH"
local-ci-runner doctor                           # must print READY (exit 0)
LOCAL_CI_RUNNER_TOKEN=... local-ci-runner register --apply --repo OWNER/REPO --i-have-admin-approval   # MANUAL GATE
local-ci-runner start
```
The manager is copied to `~/.local-ci-runner/app` and a launcher is written to `~/.local/bin/local-ci-runner`; workflows and hooks use
it, not the source checkout. Uninstall removes the launcher only if it is byte-identical to the one in the manifest.

Runner pin: `runner-pin.json` = v2.338.2, SHA-256 `25a065af…19a388`. Versions are never chosen at runtime; `verify-pin` (also a CI job) fails
if the pin stops matching official release metadata.

## Commands
```
doctor [--workflow FILE]         read-only; exit 1 if ANY mandatory prerequisite fails
install | uninstall              dry-run unless --apply
register --repo OWNER/REPO [--verify-command "ci/verify.sh -o {results_dir}"]
                                 dry-run unless --apply; --apply also needs --i-have-admin-approval and a token (env or --token-stdin)
attest-dedicated-user            dry-run unless --apply
start | stop [--dry-run] | status
clean [--apply]                  empty the managed work dir (refuses while a job runs)
report --results-dir D --job-status S     local report for the current CI job
hook job-started|job-completed --home DIR  called by the runner, not by hand
verify-pin                       compare runner-pin.json with official release metadata (network)
run --repo OWNER/REPO --sha <40-hex>       STANDALONE exact-SHA verification (not the CI path)
cancel --repo OWNER/REPO --sha <40-hex>
```
**`doctor` mandatory checks (any failure ⇒ exit 1):** Darwin/arm64; not root; non-admin; no personal-data paths + attestation;
Python ≥ 3.9; `git ≥ 2.32`, `ssh`, `ssh-keyscan`, `ssh-keygen`, `curl`, `unzip`, `shasum`, `awk`, `tr`; runner SHA-256 populated; managed
launcher on `PATH` and unmodified; no blocker findings in the scanned workflow (default: `examples/example-workflow.yml`);
allow-list config valid *when present*; pinned `known_hosts`; state-dir mode; runner-dir ownership. **Warnings never change the exit
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
