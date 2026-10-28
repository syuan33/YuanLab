# Threat model - local-ci-runner

**Headline limitation:** a self-hosted runner executes repository code (verify entrypoint, dependencies, **and the workflow
file**) as the runner's macOS user. It is not isolated like a GitHub-hosted VM. Nothing here has been run on a real runner,
and local results are not your repository's required merge gate.

## Mandatory production precondition
Run the runner in a **dedicated standard (non-admin) macOS user or a disposable VM** that holds no personal SSH keys, browser
sessions, cloud-drive data or private credentials, other than the minimum repository-scoped runner credential and any read-only
deploy keys (held as Actions secrets, written to `$RUNNER_TEMP` for one step). `doctor`, `register --apply` and `start` check:
non-root, non-admin, absence of well-known personal-data paths (existence only, contents never read), and a human attestation.
These checks are heuristics and cannot prove a machine is clean. The system-created login keychain is intentionally excluded from the
personal-data indicators (macOS creates it at the GUI login the LaunchAgent needs, so it is not evidence of anything) and is never read, listed or unlocked;
that it holds no personal or unrelated credentials rests on the operator's attestation. The LaunchAgent also requires an active GUI session for the dedicated
user, which keeps a logged-in desktop session on that account - another reason it must be dedicated.

## Threats and controls
| # | Threat | Control | Residual risk |
|---|---|---|---|
| 1 | Wrong/forked repository runs | **protected local allow-list** (0600 config, 0700 dir, owner + checksum + schema checks, exclusive create at registration); runner `job-started` hook requires repo in allow-list, event in {pull_request, push}, `head.repo == base.repo == repo`, `fork == false`, integration ref `refs/pull/N/merge` (or an allowed push ref); fails the job (never skips it) | a user who can write the state dir can edit the config and recompute the checksum - the checksum detects accidents and naive edits, not a same-user attacker |
| 2 | Allow-list widened by workflow, PR or environment | nothing in hooks/engine reads env for policy; hook `--home` is baked into the installed hook script and mandatory; negative tests for env overrides, symlinks, modes, ownership, tampering | - |
| 3 | Verifying different content than GitHub's merge result | official `actions/checkout` of `GITHUB_SHA` + `HEAD == GITHUB_SHA` assertion | - |
| 4 | **Workflow tampering by a same-repo writer** | **OPEN.** Hooks bound which repo/event runs, not what an allowed PR's workflow does | anyone with branch write access can run code as the runner user; restrict write access; dedicated account/VM limits blast radius |
| 5 | Persistent state poisoning (work dir, tool caches, `~/.npm`, `~/.m2`, ...) | `job-completed` empties the checkout and credential-shaped files; `clean --apply`; fresh checkout per job | user-level caches persist; purge by hand or rebuild the VM |
| 6 | Secret leakage | per-step `$RUNNER_TEMP` directory with `trap` cleanup + hook cleanup; env allow-list for the standalone `run`; log/report redaction | redaction is pattern-based; repo code can read the account |
| 7 | Registration token exposure | child env only (`ACTIONS_RUNNER_INPUT_TOKEN`), never argv/disk/logs; admin gate; token variable name is not an allow-list input | visible to same-user processes while `config.sh` runs; env mechanism unvalidated against a real runner |
| 8 | Tampered runner download, or a pinned runner replacing itself | pinned version + SHA-256, official-URL-prefix check, hash before extract, validated tar; **`--disableupdate` at registration** (no runtime self-update); `doctor`/`start`/`register` fail closed unless the installed runner's `--version` equals the pin | hash must be re-checked against official release notes (`verify-pin`); a same-user attacker could reconfigure or replace the runner (the version check is a consistency control, not a boundary); **stale-runner risk:** a non-updating runner left too far behind GitHub's latest release may be refused jobs (rule not verified here) - upgrade through the reviewed stop/uninstall/install/re-register path |
| 9 | MITM / wrong host key | fail-closed Ed25519 fingerprint check (workflow example) and pinned known_hosts (standalone run) | key rotation needs a reviewed change |
| 10 | Root / privilege | LaunchAgent, no `UserName`; every mutating command and hook refuses euid 0; non-admin required | - |
| 11 | Cleanup deletes the wrong files | marker + uid + realpath + no-symlink checks; hook cleans only inside the marker-owned work dir; launcher removed only if byte-identical to manifest | - |
| 12 | `stop` kills unrelated processes | only `launchctl bootout gui/<uid>/<label>` | standalone `run` kills only its own process group |
| 13 | Skipped check mistaken for a pass | single `verify` context; hook fails instead of skipping | branch-protection semantics must be tested on a disposable repo |
| 14 | False assurance | reports carry a "not your required merge gate" disclaimer | humans must read it |

## Not addressed
VM/kernel isolation beyond the dedicated account, egress filtering, dependency supply chain, GitHub-side compromise.
Self-hosted runners should not serve public repositories that accept fork PRs.

## GitHub admin actions (not performable by this tool)
Registration/removal tokens; enabling self-hosted runners and fork-workflow policy; secrets; branch protection and required checks.
