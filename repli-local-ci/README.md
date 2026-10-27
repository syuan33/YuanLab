# repli-local-ci

Run REPLI's `verify` job on your own Apple Silicon Mac through the **official GitHub Actions self-hosted runner**.

**Division of labour (architecture A, revised):**

| Owned by the official runner / GitHub | Owned by `repli-local-ci` (this tool) |
|---|---|
| job delivery, the `verify` check identity, re-run button | pinned + checksum-verified runner install |
| `actions/checkout` of GitHub's **PR integration (merge) commit** (`GITHUB_SHA`) | registration gate (human, repo-admin token) |
| artifacts, secrets, timeouts, cancellation | launchd LaunchAgent lifecycle (start/stop/status) |
| | readiness (`doctor`), job hooks (same-repo-only, cleanup), local reports |

The manager does **not** fetch the PR itself in CI. Verifying `pull_request.head.sha` through a separate SSH fetch would
verify different content than GitHub's merge commit, so that path is not used. (`run` remains as a *standalone* command
for manually verifying a known commit; the workflow never calls it.)

> **Warning.** A self-hosted runner executes repository code on your machine; it is **not** a throw-away GitHub-hosted VM.
> A **dedicated standard macOS user or disposable VM is a mandatory production precondition** (below).
> Local results are **not** the REPLI canonical merge gate. This tool has not been validated against a real runner or REPLI.

Docs: [ADR 0001](docs/adr/0001-official-runner-vs-custom-poller.md) · [THREAT-MODEL.md](THREAT-MODEL.md) · [RUNBOOK.md](RUNBOOK.md)

## Mandatory production precondition: dedicated account
The runner must live in a **dedicated standard (non-admin) macOS user, or a disposable VM**, that contains **no** personal SSH
keys, browser sessions, cloud-drive data or private credentials, other than the minimum repository-scoped runner credential
and the two read-only submodule deploy keys (which exist only as Actions secrets and are materialised in `$RUNNER_TEMP` for
one step). `doctor` fails (and `register --apply` / `start` refuse) when: the user is admin or root; known personal-data
paths exist (`~/.ssh/id_*`, `~/.aws`, `~/Library/Safari`, iCloud/Dropbox/Drive folders, browser profiles, `~/.netrc`, …;
existence is checked, contents are never read); or `attest-dedicated-user --apply` has not been run by a human on that account.

## Install (on the dedicated account)
```
git clone https://github.com/syuan33/yuanlab && cd yuanlab/repli-local-ci
bin/repli-local-ci install --dry-run          # default; shows the plan
bin/repli-local-ci attest-dedicated-user --apply
bin/repli-local-ci install --apply            # pinned runner + launcher in ~/.local/bin + hooks + LaunchAgent (not started)
export PATH="$HOME/.local/bin:$PATH"          # add to the account's shell profile
repli-local-ci doctor                         # must print READY (exit 0)
REPLI_RUNNER_TOKEN=... repli-local-ci register --apply --i-have-admin-approval   # admin token; MANUAL GATE
repli-local-ci start
```
`install --apply` copies the manager into `~/.repli-local-ci/app` and writes a launcher at `~/.local/bin/repli-local-ci`
(user-owned, stable). The workflow and the runner hooks use that launcher, **not** the source checkout, so the checkout may be
deleted afterwards. Uninstall removes the launcher only if it is byte-identical to the one recorded in the manifest.

Runner pin: `runner-pin.json` = v2.338.2 (`actions-runner-osx-arm64-2.338.2.tar.gz`,
SHA-256 `25a065af…19a388`). Versions are never chosen at runtime; `repli-local-ci verify-pin` (also a CI job) fails if the pin
stops matching the official release metadata, and notes when a newer release exists.

## Commands
```
doctor [--workflow FILE]            read-only; exit 1 if ANY mandatory prerequisite fails (see below)
install | register | uninstall      dry-run unless --apply (register also needs --i-have-admin-approval and a token)
attest-dedicated-user               dry-run unless --apply
start | stop [--dry-run] | status
clean [--apply]                     empty the managed runner work dir (refuses while a job runs)
report --results-dir D --job-status S   local report for the current CI job (used by the workflow)
hook job-started|job-completed      called by the runner, not by hand
verify-pin                          compare runner-pin.json with official release metadata (network)
run --repo syuan33/repli --sha <40-hex> | --branch <b>     STANDALONE exact-SHA verification (not the CI path)
cancel --repo ... --sha ...
```
**`doctor` mandatory checks (any failure ⇒ exit 1, "NOT READY")**: Darwin/arm64; not root; non-admin user; no personal-data
paths + dedicated-user attestation for this user; Python ≥ 3.9; `git ≥ 2.32`, `ssh`, `ssh-keyscan`, `ssh-keygen`, `curl`,
`unzip`, `shasum`, `awk`, `tr`; runner SHA-256 populated; managed launcher on `PATH` and unmodified; **no blocker findings in
the proposed workflow** (default: the bundled `examples/repli-ci-macos.proposed.yml`); pinned `known_hosts` intact; state dir
mode; runner dir ownership. **Optional warnings (never change the exit code)**: GNU tools, Homebrew bash ≥ 4, host
node/java/clj-kondo (the workflow provisions Node/Java/Python through `setup-*` actions and installs pinned clj-kondo itself).
Running `doctor` on Linux or an admin/personal account therefore exits 1 by design.

### Runner hooks (configured in the runner's `.env`, not in the workflow)
- `job-started`: refuses unless repo is `syuan33/repli`, event is `pull_request` on a `refs/pull/N/merge` integration ref with
  `head.repo == base.repo == repo` and `fork == false` (or `push` to `refs/heads/main`), `GITHUB_SHA` is a full SHA, not root,
  and the workspace lies inside the marker-owned work dir. A tampered workflow cannot remove this.
- `job-completed`: empties the checkout and deletes credential-shaped leftovers (deploy keys, ssh config, gitconfig,
  known_hosts) inside the managed work dir; never follows symlinks, never touches anything outside the marker-owned dir.

## The proposed REPLI workflow (not applied)
`examples/repli-ci-macos.proposed.yml` is the full proposed `ci.yml`; `examples/repli-ci-macos.patch` is the exact unified diff against
`syuan33/repli` main @ `2a3d4cb06d85537e213d45cc15dc8bd0579346e2` (blob `97430e93…`; `patch -p1` round-trip verified,
**REPLI was not modified**). Preserved: job/check name `verify`; integration-commit semantics (plain `actions/checkout`,
`persist-credentials: false`, plus an assertion `HEAD == GITHUB_SHA`); standard **and** hermetic integration profiles;
`verification-results` artifact upload; both private submodules with only their own read-only deploy key and fail-closed
GitHub host-key check; Node 22, Java 21, Python 3.11, pinned clj-kondo (macOS arm64 asset — its SHA-256
`911f3803…` was computed once by downloading the official asset and **must be cross-checked by a reviewer**); same-repo-only via
job `if:` **and** the runner hook. Changes: `runs-on`; submodule credentials/ssh/git config live in a per-step dir under
`$RUNNER_TEMP` (removed on exit) instead of `~/.ssh` and global git config; `shasum` instead of `sha256sum`; added readiness and report steps;
a hosted `verify-fork-refusal` job with the same check name that **fails** for fork PRs (a skipped job would satisfy a required check).

### GitHub-hosted vs your Mac
| | ubuntu-24.04 hosted | dedicated Mac user |
|---|---|---|
| Isolation | fresh VM per job | one account / VM, persistent disk + caches |
| Userland | GNU coreutils, bash 5 | BSD tools, bash 3.2 (`shell: bash` steps here are 3.2-safe) |
| Packages | apt/sudo | none needed: setup actions + curl/shasum/unzip |
| Node/Java/Python | `setup-*` | same actions (arm64 builds; `setup-python` 3.11 on a self-hosted Mac is **untested**) |
| clj-kondo | linux asset | `macos-aarch64` asset, own pinned hash |
| Private submodules | `~/.ssh` on the VM | per-step `$RUNNER_TEMP` ssh config, removed on exit + by hook |
| Containers | supported | not supported |

## Tests
```
cd repli-local-ci
PYTHONPATH=src:. python3 -X dev -W error -m unittest discover -s tests -t . -v
./scripts/smoke-local.sh        # PYTHON=/usr/bin/python3 on macOS
```
All tests are synthetic (no network, no runner, no REPLI). `tests/fixtures.StrictCase` makes a leaked process/file fail the
test (plain `-W error` does not, because ResourceWarning is raised in finalizers; `test_strict.py` proves the leak detector fails).
CI (`.github/workflows/repli-local-ci-tests.yml`): Linux Python 3.9 + 3.13, hosted `macos-14` arm64 with Apple `/usr/bin/python3`
(unit tests, smoke, dry-runs, `doctor` must say NOT READY), and `pin-check`.
**Minimum Python is 3.9** (Apple's Command Line Tools `python3`); verified on 3.9.25 (Linux) and by the macOS CI job.

## Still required before REPLI uses this
Independent code review; test on a disposable runner/VM; REPLI workflow/governance change (admin-reviewed); explicit user approval to register
and enable. Repo-admin only: creating registration/removal tokens, runner/fork-workflow settings, secrets, branch protection.
