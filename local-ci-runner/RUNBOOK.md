# RUNBOOK - moving a required check between GitHub-hosted and local

Nothing here has been executed against a real runner or repository. Steps touching GitHub settings need the owner's explicit approval.

## Gates before first use (all manual)
1. Independent code review of this tool. 2. Full dry run on a **disposable** runner (VM or throwaway repository).
3. A reviewed workflow change in the target repository (see `examples/example-workflow.yml`, synthetic). 4. Your explicit approval to register.

## One-time setup (dedicated standard macOS user or disposable VM - mandatory)
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
`stop` / `start`. Upgrade the runner: reviewed change to `runner-pin.json` (version, asset, url, sha256 verified against the official
release), `verify-pin` green, then `stop`, `uninstall --apply`, `install --apply`, re-register with a new token.

## Cache and workspace hygiene
- The hook empties the checkout after each job. With the runner idle: `local-ci-runner clean --apply` (refuses while a job runs).
- `clean` lists but does not delete user caches (`~/.npm ~/.m2 ~/.gradle ~/.cache ~/Library/Caches`): delete them by hand after a
  suspicious PR or monthly, or rebuild the VM from a clean snapshot (preferred).
- The same-repo workflow-tampering risk (THREAT-MODEL #4) is open; restrict who can push branches.

## Troubleshooting
- `doctor` NOT READY: each FAIL names the missing prerequisite; `warn` lines never block.
- Job fails in `job-started`: see `~/.local-ci-runner/logs/hooks.log` (repo/event/ref only; no environment is logged). "not registered" means the protected config is missing.
- `refused: ... allow-list config`: the config must be a regular 0600 file in a 0700 directory you own, with a valid checksum. Re-register instead of editing it.
- Host-key errors: never bypass; fingerprint `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`.
