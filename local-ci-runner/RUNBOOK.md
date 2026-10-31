# RUNBOOK - moving a required check between GitHub-hosted and local

The runner installation and Python package/toolcache preseed have been exercised on a dedicated non-admin Apple Silicon account; the
complete restartable script has also passed its no-change dry-run. This is not a production or merge-gate claim. Steps touching
GitHub settings still need the owner's explicit approval.

## Gates before first use (all manual)
1. Independent code review of this tool. 2. Full dry run on a **disposable** runner (VM or throwaway repository).
3. A reviewed workflow change in the target repository (see `examples/example-workflow.yml`, synthetic). 4. Your explicit approval to register.

## Choose the deployment route before doing anything stateful

### Route A: direct deployment on the CI Mac

Use this route when a human is logged in as the dedicated standard macOS runner account. The reviewed source may be checked out
temporarily in that account; installation copies the reviewed manager into managed state, so the checkout is not a runtime dependency.
Run the one-time setup below directly in that account.

### Route B: deployment driven by M1/Orca

Use this route when the agent/executor and the dedicated runner account are different OS security principals, even if both accounts
are on the same physical Mac. A common topology is:

```text
M1/Orca executor (for example 192.168.x.y)
  |  Home-private SSH alias + dedicated identity + pinned host key
  |  BatchMode SSH; no secrets in worktree
  v
dedicated standard macOS runner account
  |  ~/.local-ci-runner/{app,runner,work,reports}
  v
one explicitly allow-listed GitHub repository
```

The repository worktree may contain reviewed source and synthetic evidence, but never the executor's `~/.ssh`, routing file,
registration token or LAN details. Do not copy, symlink or bind-mount private Home configuration into an Orca worktree. If the Orca
executor cannot read its own Home-private control-plane configuration, stop and repair the Orca host/user permission boundary.

Before generating a token, M1 must perform a fail-closed private bootstrap:

1. Confirm it is running as the intended executor and using that executor's Home.
2. Read the SSH alias from an operator-private source outside the worktree. If a routing file is used, a minimal synthetic shape is
   `{:ci-ssh-alias "ci-runner-private"}`; this is an orchestration convention, not tool configuration.
3. Confirm that the alias is defined in **this executor's** OpenSSH configuration. Do not reuse an alias copied from another host,
   Issue, PR or checkpoint.
4. Require strict host-key checking with a separately verified, dedicated `known_hosts` record. Never accept a key interactively,
   scan the LAN for a replacement host, or disable verification.
5. Run a non-interactive BatchMode SSH no-op. Only after it succeeds may M1 recheck the approved source SHA and runner/workflow state.
6. Generate the short-lived repository registration token immediately before registration and stream it through stdin to the remote
   `register --token-stdin` process. Never put it in argv, shell history, a file, chat or logs.
7. Run `doctor`, registration, `doctor`/`status`, then `start` on the dedicated account. Verify the GitHub runner is online and idle
   with the expected labels before enabling any workflow.
8. Publish only sanitized evidence: exact reviewed SHA, boolean gate outcomes, runner online/idle, labels, workflow/run ID and check
   conclusion. Keep alias, IP address, usernames, Home paths, key paths and host keys private.

### Diagnosing the private bootstrap without leaking it

Report boolean outcomes rather than configuration values:

| Observation | Meaning | Correct action |
|---|---|---|
| routing file unreadable | wrong executor Home or host-level permission problem | repair the executor account; do not copy it into the worktree |
| alias cannot be parsed | malformed private routing metadata | fix the Home-private file; do not infer a host |
| alias parses but OpenSSH does not load it | stale alias, missing Include, or Host-name mismatch | repair this executor's OpenSSH configuration |
| OpenSSH loads alias but BatchMode fails | network, identity, authorization or pinned-host-key failure | stop before token creation and diagnose through a trusted channel |
| BatchMode succeeds but `command -v local-ci-runner` fails | non-interactive SSH did not load the interactive PATH | invoke `$HOME/.local/bin/local-ci-runner` explicitly; if that managed launcher is absent, stop |

An illustrative incident is an executor whose private routing file still names an alias from a different Mac. The routing file can be
read and parsed, but this executor has no matching OpenSSH `Host` entry. Replacing it with this executor's already verified dedicated
alias makes the bootstrap pass. Public documentation should record only “Home-private SSH alias mismatch corrected”; use addresses
such as `192.168.x.y` and synthetic aliases in examples, never the real values.

## One-time setup (dedicated standard macOS user or disposable VM - mandatory)
Log in to the dedicated user's macOS desktop first and keep that GUI session active: the LaunchAgent loads into `gui/<uid>`, and `start` fails
with "the dedicated user must be logged in" otherwise. The empty login keychain and the `~/Library/Safari` / `~/Library/Cookies` directories macOS creates at that first login are expected; they are never read
or counted as personal data. Attest (below) that it holds no personal or unrelated credentials.
```
local-ci-runner install --dry-run
local-ci-runner attest-dedicated-user --apply
local-ci-runner install --apply            # then put ~/.local/bin on PATH and re-login
local-ci-runner doctor                     # must say READY
# a repository admin creates a registration token (Settings > Actions > Runners > New self-hosted runner), then:
LOCAL_CI_RUNNER_TOKEN=... local-ci-runner register --apply --repo OWNER/REPO --i-have-admin-approval
local-ci-runner start && local-ci-runner status
```
`register --apply` is the step that creates the protected allow-list (`~/.local-ci-runner/config.json`, mode 0600). Until it has run,
every hook denies every job. Repo-admin only: runner token, Actions secrets, fork-workflow settings, branch protection.

## One-time Python preseed for a non-admin runner

### Why this is separate

The official Apple Silicon Python package used by `actions/setup-python` calls the macOS system installer on its first cache miss.
That operation needs an administrator, while the persistent runner must stay a standard user. Do not solve this by adding the runner
to the `admin` group, granting passwordless sudo, running the service as an administrator, or weakening the workflow.

Node and Java use user-writable setup-action caches and normally need no administrator intervention. shadow-cljs/ClojureScript is
installed from the repository lockfile by `npm ci`; clj-kondo is a checksum-verified temporary workflow download. They should not be
installed globally as a workaround.

### Human/admin steps

1. Stop the runner and confirm no `Runner.Listener` or `Runner.Worker` process remains.
2. From a separate administrator account, use the exact reviewed tool checkout. Do not copy private SSH configuration or tokens into it.
3. Run the script without `--apply`; review the pinned versions, hashes and planned paths.
4. Run it again with `--apply` and enter the administrator password only into the local macOS prompt:

   ```bash
   ./scripts/preseed-macos-ci-toolchain.sh --ci-user <dedicated-ci-user>
   ./scripts/preseed-macos-ci-toolchain.sh --ci-user <dedicated-ci-user> --apply
   ```

5. Require the final `TOOLCHAIN PRESEED PASSED` result. This step does not authorize starting the runner or enabling a workflow.

### What the script does

- refuses non-macOS/non-arm64 hosts, an absent or admin CI account, an active runner, or an unexpected managed installation;
- downloads the exact official Python 3.11.9 release and verifies the archive, member set, setup script and package hashes before install;
- suppresses the Python package's shell-profile updater, command-line shims and documentation; installs framework, certificate helper and pip;
- gives the dedicated account ownership of only the pinned Python 3.11 framework and creates its setup-python cache marker;
- validates Python, SSL and pip as the dedicated account;
- verifies existing Node 22/Java 21 caches, or explicitly defers their first population to pinned setup actions;
- checksum-verifies clj-kondo in a temporary directory and records a mode-0600, non-secret preseed manifest;
- pre-builds the Matplotlib font cache for the managed Python (see below);
- never starts/registers the runner, requests a GitHub token, changes a workflow, installs repository dependencies or runs repository code.

### Matplotlib font cache

A cold Matplotlib font cache is the suspected cause of a service missing a short readiness window on its first start (a hypothesis from one
blocked canonical run, not yet measured on the CI account); pre-building it removes that variable.
`scripts/preseed_matplotlib_font_cache.py` is run by `--apply` as the dedicated non-admin account, with a cleared environment and the
managed Python in isolated mode (`env -i ... python -I -`, script read from stdin). It:

- is executed through `scripts/preseed_font_cache_step.sh` (rc handling, status parsing, the skip WARNING and `--require-font-cache`
  forwarding), which is exercised by tests with a stub helper;
- uses the platform default cache location under the account's home and ignores `MPLCONFIGDIR`/`XDG_*` overrides, so a job that sets
  such an override will not benefit;
- refuses (exit 2, nothing written) a symlinked cache directory, a non-directory, or any font-list/stamp entry that is a symlink, not a
  regular file, not owned by the account, or hardlinked (so mode repair can never change an unrelated file);
- treats the font list as fresh only when the file for the cache-format version Matplotlib expects (read from its source, without importing
  it) exists with that `_version` and a non-empty `ttflist`; anything else is stale and rebuilt, and reports unexpected I/O errors as
  `status=failed reason=io_error` instead of a traceback;
- creates the cache directory mode `0700` and font-list/stamp files mode `0600`, and restores those modes on a repeat run without
  rebuilding (`action=fix_mode`); the stamp is written through a uniquely named, exclusively created temporary file;
- records the Python and Matplotlib versions in a stamp file and rebuilds only when either changes (or the stamp is missing/corrupt);
  it deletes only `fontlist-v*.json` files and the stamp, and removes the stamp before building so a failed build is retried;
- is idempotent: a repeat run with an unchanged stamp does nothing;
- prints fixed `key=value` fields only (never paths or environment values); without `--apply` it imports no Matplotlib and writes nothing.

Matplotlib is installed by the repository's dependency step, not by this script. The helper looks only in the managed Python's own
environment (isolated mode: no user site, no `PYTHON*` variables); if the repository ever installs into a venv or `--user` site it will
report the package absent, which `--require-font-cache` turns into a hard failure rather than a silent skip. Threat model: the cache
directory is private to the one CI account, so checks are lstat-based hygiene against stale or misplaced entries, not a defence against a
concurrent attacker running as that same account.

On a brand-new account the helper therefore reports
`skipped_matplotlib_absent` plus a stderr WARNING (not a failure, but the cache is **not** pre-built); rerun `--apply
--require-font-cache` after the first dependency install and before the first service start. `--require-font-cache` turns the absent case into
a failure, so use it for the final pre-run preseed. The manifest records the result as
`matplotlib_font_cache=<status>`.

### Recovery and verification

If output shows that the system installer succeeded but a later prerequisite check failed, **do not uninstall Python**. Correct the
named check and rerun the same `--apply` command. A valid complete Python 3.11.9 cache is detected, the package installation is skipped,
and validation continues. This behavior was needed in a real setup when Python completed but an administrator process could not
traverse the dedicated account's private toolcache; the fix was to perform cache inspection as the dedicated account, not to loosen
Home permissions.

After success, use the managed launcher to run `doctor` and `status` as the dedicated account. Only a separately approved integration
gate may start the runner and enable a workflow. Stop the runner again after the bounded test and publish only sanitized versions,
boolean outcomes, exact GitHub SHAs/run IDs and check conclusions—never local usernames, paths, aliases, addresses or keys.

## Go local when Actions minutes run low
1. `status`: installed, registered, loaded; `doctor` READY; Mac awake and online.
2. On a same-repo test branch confirm the check runs on the Mac, the job-started hook passes, and
   `~/.local-ci-runner/reports/ci-*/report.json` shows `integration_sha == GITHUB_SHA`.
3. Merge the reviewed workflow change flipping `runs-on`. On a disposable repository first, confirm that a fork PR makes the
   single `verify` check **fail** (hook refusal), not skip.

## Switch back to hosted
Revert `runs-on` in the target repository; `local-ci-runner stop`; optionally `uninstall --dry-run`, then `--apply`, and deregister the runner (admin).

## Pause / resume / upgrade
`stop` / `start`.

**Upgrade the runner (never automatic - registration passes `--disableupdate`).** 1) Open a reviewed PR changing `runner-pin.json` (version, asset,
url, sha256 verified against the official release notes). 2) `verify-pin` and the full suite green. 3) On the runner account: `stop`,
`uninstall --apply`, `install --apply`, `doctor` (checks the installed version equals the pin), re-register with a new admin token, `start`.
`start`/`doctor` refuse a runner whose `--version` differs from the pin.

**Stale-runner consequence.** GitHub expects self-hosted runners to stay reasonably current: a runner that has fallen too far behind the latest official release may be refused jobs (commonly described as roughly 30 days after a new release - confirm the current rule in GitHub's runner documentation, it was not verified here). With `--disableupdate` the runner will not fix this by itself, so an unreviewed-but-stale runner is an availability risk that you trade for a runner binary that never changes without review. Review and roll pin updates promptly after official releases (the weekly `pin-check` CI job and `verify-pin` note when a newer
release exists). If jobs stop being assigned, compare the runner version with the latest release before debugging anything else.

## Cache and workspace hygiene
- The hook empties the checkout after each job. With the runner idle: `local-ci-runner clean --apply` (refuses while a job runs).
- `clean` lists but does not delete user caches (`~/.npm ~/.m2 ~/.gradle ~/.cache ~/Library/Caches`): delete them by hand after a
  suspicious PR or monthly, or rebuild the VM from a clean snapshot (preferred).
- The same-repo workflow-tampering risk (THREAT-MODEL #4) is open; restrict who can push branches.

## Troubleshooting
- `doctor` NOT READY: each FAIL names the missing prerequisite; `warn` lines never block.
- Job fails in `job-started`: see `~/.local-ci-runner/logs/hooks.log` (repo/event/ref only; no environment is logged). "not registered" means the protected config is missing.
- `refused: ... allow-list config`: the config must be a regular 0600 file in a 0700 directory you own, with a valid checksum. Re-register instead of editing it.
- `doctor`: "[FAIL] runner pin ... installed pin ... modified/missing/does not belong" - the managed pin no longer matches the install manifest. Do not edit it: `stop`, `uninstall --apply`, then `install --apply` again from the reviewed checkout.
- `start`: "no active GUI login session ... the dedicated user must be logged in" - log in to that user's desktop (or fast-user-switch to it), then retry. "launchctl bootstrap failed" has the same most-likely cause, or the service is already loaded (`status`, `stop`).
- `doctor`/`start`: "installed runner version X != pinned Y" means the runner and `runner-pin.json` disagree - fix the pin through review or reinstall; never edit around it.
- Workflow fails at `Set up Python` with `sudo: a terminal is required`: stop/disable the bounded integration run, keep the runner
  account non-admin, and use the reviewed preseed script from a separate administrator account. Then verify `doctor`/`status` before
  authorizing a new exact-head attempt.
- Preseed says the Python installer succeeded and then fails on a toolcache lookup: Python is a persistent completed step, not a
  transaction rollback. Rerun the corrected script; it verifies the exact completion marker and skips reinstalling the package.
- Host-key errors: never bypass; fingerprint `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`.
