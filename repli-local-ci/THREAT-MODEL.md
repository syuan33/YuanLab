# Threat model - repli-local-ci

**Headline limitation:** a self-hosted runner executes repository code (`dev/verify.sh`, its dependencies, and the
workflow file) as your macOS user. It is *not* isolated like a GitHub-hosted VM. Everything below reduces risk; none
of it makes untrusted code safe. Only run code you would run in your own terminal.

## Assets
Your user account and home directory, SSH keys / agent, other repos, browser sessions, LAN access, the runner credential,
GitHub tokens in the environment, the integrity of the "verify passed" signal.

## Actors
- Trusted: you; same-repo branch writers of `syuan33/repli`.
- Untrusted: fork authors, anyone who can open PRs from forks, compromised dependencies, network attackers (DNS/MITM).

## Threats and controls
| # | Threat | Control (where) | Residual risk |
|---|---|---|---|
| 1 | Fork/external PR code runs on the Mac | allow-list `syuan33/repli` (`safety.validate_repo`); head repo must equal repo (`validate_head_repo`); example workflow `if:` guard | A skipped job satisfies a required check - not "verified". Public repo + "run workflows from fork PRs" setting must stay restrictive (admin) |
| 2 | Branch moves mid-run / TOCTOU | full 40-hex SHA required; fetched by SHA; `HEAD == sha` asserted; `--branch` resolved once | none known |
| 3 | Workflow tampering by a same-repo writer | none in this tool (runner executes PR-head workflow) | **Open.** Restrict branch write access; consider environments/required reviewers; ADR option B avoids it |
| 4 | Dirty/reused workspace poisoning | `os.mkdir` fresh dir per run, refuses existing; checkout dir must be empty and clean | persistent caches in `$HOME` (Maven, npm, ...) are shared and can be poisoned by earlier code |
| 5 | Secret leakage to logs/reports | redactor on every log line (tokens, auth headers, `*_TOKEN=`, URL creds, PEM blocks); env allow-list for verify (no `GITHUB_*`, no ssh agent); never reads `.env`/keys | Redaction is pattern-based, not a guarantee; repo code can read files in your home |
| 6 | Token in argv/history/fixtures | registration token only via env of the child (`ACTIONS_RUNNER_INPUT_TOKEN`) from `REPLI_RUNNER_TOKEN`/stdin; tested | token is visible to same-user processes via the environment during `config.sh` |
| 7 | Tampered runner download | pinned version, official URL prefix enforced, SHA-256 verified before extraction, tar path/link validation, https only | the hash must be copied by a human from the official release notes (`runner-pin.json` ships with `null`, install refuses) |
| 8 | MITM of git over SSH | pinned GitHub ed25519 host key, `StrictHostKeyChecking=yes`, own known_hosts, mismatch fails closed | key rotation by GitHub requires a reviewed code change |
| 9 | Wrong repo / malicious submodule | origin URL must parse to `github.com/<repo>`; every `.gitmodules` URL must be a github ssh/https URL for an allow-listed submodule (default: none) | submodule allow-list is code/config, review it |
| 10 | Running as root / privilege | LaunchAgent in `gui/<uid>`, no `UserName`, every mutating command refuses euid 0 | - |
| 11 | Cleanup deletes wrong files | `safe_remove` requires: real dir (no symlink), strictly inside root, same uid, marker file whose recorded path equals the dir; symlinks inside are unlinked, not followed | - |
| 12 | `stop` kills unrelated processes | only `launchctl bootout gui/<uid>/<our label>`; verify kills only its own process group | processes that `setsid` out of the group survive |
| 13 | Resource exhaustion / queue abuse | concurrency 1, dedupe by (repo, sha), newer head of same ref supersedes, timeout (default 1h) | no CPU/disk quotas |
| 14 | Stale lock after crash | dead-pid detection reclaims the slot | PID reuse could delay reclaim (fails safe: slot stays busy) |
| 15 | False assurance | every report carries the "not the canonical merge gate" disclaimer | humans must read it |

## Out of scope / not addressed
Kernel/hypervisor isolation, dedicated user or VM (strongly recommended: run the runner under a dedicated standard macOS
user or a Tart/UTM VM), network egress filtering, supply-chain attacks on dependencies of REPLI.

## GitHub admin actions (cannot be done by this tool)
Creating the runner registration/removal token; enabling/disabling self-hosted runners and fork-workflow policies; editing
branch protection / required checks; merging the REPLI workflow change.
