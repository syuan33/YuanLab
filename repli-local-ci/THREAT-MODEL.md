# Threat model - repli-local-ci (revision 2)

**Headline limitation:** a self-hosted runner executes repository code (`dev/verify.sh`, its dependencies, **and the workflow
file**) as the runner's macOS user. It is *not* isolated like a GitHub-hosted VM. Nothing here validates, or claims equivalence
with, the REPLI canonical gate, and nothing has been run on a real runner.

## Mandatory production precondition
The runner runs in a **dedicated standard (non-admin) macOS user or a disposable VM** holding no personal SSH keys, browser
sessions, cloud-drive data or private credentials, other than the minimum repository-scoped runner credential (and the two
read-only submodule deploy keys, held only as Actions secrets and written to `$RUNNER_TEMP` for one step). This is enforced
mechanically where possible (`doctor`, `register --apply`, `start`: non-admin, non-root, personal-data path existence, human
attestation) and is otherwise a procedural requirement. Existence checks are heuristic: they cannot prove a machine is clean.

## Assets / actors
Assets: the runner account and its disk, the runner credential, the two deploy keys while materialised, the integrity of the
"verify passed" signal. Trusted: the owner, same-repo branch writers of `syuan33/repli`. Untrusted: fork authors, compromised
dependencies, network attackers.

## Threats and controls
| # | Threat | Control | Residual risk |
|---|---|---|---|
| 1 | Fork/external PR code runs | job `if:` + **runner `job-started` hook** (repo allow-list, event allow-list, `head.repo == base.repo == repo`, `fork == false`, integration ref `refs/pull/N/merge`) - the hook is runner config, not editable by a PR | repository/org settings for fork workflows are admin-only; a skipped job satisfies required checks, so a same-name hosted job fails forks (branch-protection semantics untested) |
| 2 | Verifying different content than GitHub's merge result | official `actions/checkout` of `GITHUB_SHA` + `HEAD == GITHUB_SHA` assertion; no nested fetch of head SHA | none known |
| 3 | **Workflow tampering by a same-repo writer** (PR-head workflow runs on the Mac) | **OPEN.** Hooks bound *what repo/event* may run but cannot constrain what a permitted PR's workflow does | Anyone with branch write access can run arbitrary code as the runner user. Mitigate by restricting write access, the dedicated account/VM (limits blast radius), and treating the account as compromisable |
| 4 | Persistent state poisoning (workspace, `_work`, npm/pip/Maven/Gradle/tool caches, `~/.local`) | `job-completed` hook empties the checkout and credential-shaped files; fresh `actions/checkout`; `clean --apply` empties the managed work dir | user-level caches (`~/.npm`, `~/.m2`, `~/.gradle`, `~/.cache`, `Library/Caches`, runner `_tool`) persist and can be poisoned by earlier jobs; purge them by hand (RUNBOOK) or rebuild the VM |
| 5 | Secret leakage | submodule keys only in env of one step, written under `$RUNNER_TEMP`, `trap` cleanup + hook cleanup; nothing in `~/.ssh` or global git config; log/report redaction; no `.env`/key reads | redaction is pattern-based; repo code can read anything in the account |
| 6 | Registration token exposure | passed only via child env (`ACTIONS_RUNNER_INPUT_TOKEN`), never argv/disk/logs; admin gate | visible to same-user processes while `config.sh` runs; env-var mechanism not validated against a real runner |
| 7 | Tampered runner download | pinned version + SHA-256 in repo, official-URL-prefix check, hash before extract, validated tar | hash was independently derived by downloading the official asset and matches the value supplied at review; `verify-pin`/CI compares against release metadata (not runnable from the authoring sandbox) |
| 8 | MITM / wrong host key | workflow: fail-closed Ed25519 fingerprint check; manager: pinned known_hosts | GitHub key rotation needs a reviewed change |
| 9 | Wrong repo / submodule | repo allow-list; submodule allow-list (default none) for `run`; workflow uses fixed `insteadOf` URLs | - |
| 10 | Root / privilege | LaunchAgent, no `UserName`; all mutating commands and hooks refuse euid 0; non-admin required | - |
| 11 | Cleanup deletes wrong files | marker + uid + realpath + no-symlink checks; hook cleaning only inside marker-owned `work/`; launcher removed only if byte-identical to manifest | - |
| 12 | `stop` kills unrelated processes | only `launchctl bootout gui/<uid>/<label>`; no signals to other PIDs | `run` kills its own process group only; `setsid` escapees survive |
| 13 | Resource exhaustion / queue abuse | runner runs one job at a time; `run` queue is concurrency 1 with dedupe/supersede and a timeout | no CPU/disk quotas |
| 14 | False assurance | reports/summaries carry the "not the canonical merge gate" disclaimer | humans must read it |
| 15 | Manager unavailable / drifted at run time | step 1 `doctor` fails the job; launcher hash checked against manifest | - |

## Not addressed
VM/kernel isolation beyond the dedicated account, egress filtering, dependency supply chain, GitHub-side compromise.

## GitHub admin actions (not performable by this tool)
Registration/removal tokens; enabling self-hosted runners and fork-workflow policy; secrets; branch protection / required
checks; merging the REPLI workflow change.
