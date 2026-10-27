# RUNBOOK - switching REPLI `verify` between GitHub-hosted and local

Nothing here has been executed against REPLI. Every step touching REPLI or GitHub settings needs the owner's explicit approval.

## Preconditions (once, all manual gates)
1. Independent code review of this PR. 2. Dry run on a **disposable** runner/VM/user (never first on your daily account).
3. REPLI workflow/governance change merged (see `examples/repli-verify-workflow.patch`). 4. You approve registration.
Repo-admin-only: creating registration/removal tokens, runner settings, fork-PR workflow policy, branch protection.

## Normal operation
```
repli-local-ci doctor          # read-only
repli-local-ci status
repli-local-ci install --dry-run ; repli-local-ci install --apply     # downloads pinned+verified runner, writes LaunchAgent
REPLI_RUNNER_TOKEN=... repli-local-ci register --apply --i-have-admin-approval   # token from GitHub admin UI, one time
repli-local-ci start
```

## Monthly minutes are nearly exhausted -> go local
1. `repli-local-ci status` shows installed + registered + loaded; `doctor` has no FAIL.
2. Trigger one `workflow_dispatch`/draft PR on a same-repo branch and confirm the `verify` check runs on the Mac and the report
   under `~/.repli-local-ci/reports/<run>/` matches the PR head SHA.
3. Merge the REPLI change that flips `runs-on` to `[self-hosted, macOS, ARM64, repli-local-ci]` (REPLI governance, repo admin).
4. Keep the Mac awake and online (`caffeinate`/Energy settings); if it is offline, PR checks stay pending.

## Switch back to GitHub-hosted
1. REPLI change reverting `runs-on: ubuntu-24.04` (or add a repository variable-driven `runs-on`, if REPLI governance approves it).
2. `repli-local-ci stop` (boots out only our launchd label and asks in-flight runs to cancel).
3. Optional: `uninstall --dry-run`, then `--apply`; deregister the runner in GitHub (Settings > Actions > Runners) - admin.

## Pause / resume
`repli-local-ci stop` / `start`. Queued jobs stay queued; superseded heads are dropped automatically.

## Upgrade runner
Update `runner-pin.json` (version, asset, url, **sha256 copied from the official release notes**) via reviewed PR, then
`stop`, `uninstall --apply`, `install --apply`; re-registration needs a new admin token.

## Troubleshooting
- `refused: ... not allow-listed` - only `syuan33/repli`. `refused: ... 40-char` - pass a full SHA.
- `host key` errors - never bypass; compare with GitHub's published fingerprint `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`.
- status `busy` / waiting - one job at a time; `status` lists the queue; `cancel --repo ... --sha ...`.
- Stuck lock after crash - dead PIDs are reclaimed automatically on the next claim.
- Linux-only workflow steps - `doctor --workflow <file>`; fix the step, do not skip it.
