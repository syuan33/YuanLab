# RUNBOOK - switching REPLI `verify` between GitHub-hosted and local

Nothing here has been executed against REPLI or a real runner. Any step touching REPLI or GitHub settings needs the owner's
explicit approval.

## Gates before first use (all manual)
1. Independent code review of this tool. 2. Full dry run on a **disposable** runner (VM / throwaway repo, never first on REPLI).
3. REPLI workflow/governance change (`examples/repli-ci-macos.patch`, adapted by REPLI maintainers). 4. Your explicit approval to register.

## One-time setup (dedicated standard macOS user or disposable VM - mandatory)
The account must hold no personal SSH keys, browser sessions, cloud-drive data or private credentials.
```
repli-local-ci install --dry-run
repli-local-ci attest-dedicated-user --apply
repli-local-ci install --apply          # then ensure ~/.local/bin is on PATH (profile) and re-login
repli-local-ci doctor                   # must say READY
# repo admin creates a registration token (Settings > Actions > Runners > New self-hosted runner), then:
REPLI_RUNNER_TOKEN=... repli-local-ci register --apply --i-have-admin-approval
repli-local-ci start && repli-local-ci status
```
Repo-admin only: runner token, secrets (`FASTAPI_SUBMODULE_SSH_KEY`, `RN_APP_API_SUBMODULE_SSH_KEY` - read-only deploy keys),
fork-workflow settings, branch protection, merging the workflow change.

## Monthly minutes nearly exhausted -> go local
1. `status`: installed, registered, launchd loaded; `doctor` READY; Mac awake and online.
2. On a same-repo test branch, confirm `verify` runs on the Mac, the job-started hook passes, and
   `~/.repli-local-ci/reports/ci-*/report.json` shows `integration_sha == GITHUB_SHA`.
3. Merge the REPLI change flipping `runs-on` (REPLI governance, admin). Verify on a **disposable repo first** that the
   same-named `verify-fork-refusal` job makes a fork PR fail the required check rather than pass as "skipped".

## Switch back to hosted
Revert the `runs-on`/job structure in REPLI (reviewed change); `repli-local-ci stop`; optionally `uninstall --dry-run` then `--apply`
and deregister the runner (admin).

## Pause / resume / upgrade
`stop` / `start`. Upgrade runner: reviewed PR updating `runner-pin.json` (version, asset, url, sha256 verified against the official
release), `verify-pin` green, then `stop`, `uninstall --apply`, `install --apply`, re-register with a new token.

## Cache and workspace hygiene (persistent-state risk)
- After each job the hook empties the checkout. Between jobs, with the runner idle: `repli-local-ci clean --apply`
  (refuses while `Runner.Worker` runs; empties the managed `work/`, including `_tool`).
- `clean` lists but does **not** delete user caches. After any suspicious PR, or monthly, delete `~/.npm ~/.m2 ~/.gradle ~/.cache
  ~/Library/Caches` of the dedicated account by hand - or rebuild the VM from a clean snapshot (preferred).
- The same-repo workflow-tampering risk (THREAT-MODEL #3) is **open**; restrict who can push branches to REPLI.

## Troubleshooting
- `doctor` NOT READY: each FAIL line names the missing prerequisite; optional `warn` lines never block.
- Job fails in `job-started`: read `~/.repli-local-ci/logs/hooks.log` (repo/event/ref only; no environment is logged).
- `refused: ... not allow-listed` / `40-char`: `run` accepts only `syuan33/repli` and full SHAs.
- Host-key errors: never bypass; fingerprint `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`.
- Linux-only steps: `doctor --workflow FILE` reports blockers; fix them, do not skip them.
