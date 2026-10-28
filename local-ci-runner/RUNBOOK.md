# RUNBOOK - moving a required check between GitHub-hosted and local

Nothing here has been executed against a real runner or repository. Steps touching GitHub settings need the owner's explicit approval.

## Gates before first use (all manual)
1. Independent code review of this tool. 2. Full dry run on a **disposable** runner (VM or throwaway repository).
3. A reviewed workflow change in the target repository (see `examples/example-workflow.yml`, synthetic). 4. Your explicit approval to register.

## One-time setup (dedicated standard macOS user or disposable VM - mandatory)
Log in to the dedicated user's macOS desktop first and keep that GUI session active: the LaunchAgent loads into `gui/<uid>`, and `start` fails
with "the dedicated user must be logged in" otherwise. The empty login keychain macOS creates at that first login is expected; it is never read
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
- `start`: "no active GUI login session ... the dedicated user must be logged in" - log in to that user's desktop (or fast-user-switch to it), then retry. "launchctl bootstrap failed" has the same most-likely cause, or the service is already loaded (`status`, `stop`).
- `doctor`/`start`: "installed runner version X != pinned Y" means the runner and `runner-pin.json` disagree - fix the pin through review or reinstall; never edit around it.
- Host-key errors: never bypass; fingerprint `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`.
