# ADR 0001: Official self-hosted runner + management layer (A) vs custom PR poller (B)

Status: Proposed, revised after independent review (REPAIR) · Date: 2026-10-27

## Context
REPLI's `verify` job runs on GitHub-hosted `ubuntu-24.04` and consumes monthly Actions minutes. We want the
same check to run on a local Apple Silicon Mac, safely and reproducibly, without touching the REPLI repository.

## Options
**A. Official GitHub Actions self-hosted runner**, plus a thin manager (`repli-local-ci`) for pinned install,
registration gate, launchd lifecycle, exact-SHA isolated execution, queue, redaction, reports and cleanup.

**B. Custom poller** that lists PRs, fetches an exact SHA, runs verify and publishes a commit status / check.

## Comparison
| Concern | A: official runner | B: custom poller |
|---|---|---|
| Check name `verify`, PR integration, re-run button, logs in UI | native | must be re-implemented (Checks API needs a GitHub App or PAT with write scope) |
| Credentials held on the Mac | one short-lived runner credential, repo-scoped; registration token used once, never stored by us | long-lived PAT/App key with `checks:write`/`statuses:write` **plus** read access to private code - larger blast radius |
| Job protocol, cancellation, timeouts, concurrency, auto-update | maintained by GitHub | ours to build and keep correct |
| Fork-PR handling | enforced by workflow `if:` and by `run` (head repo must equal repo) | enforced by us only |
| Code executed from PR | workflow file from the PR head is executed (see threat model); hooks narrow but do not remove this | only `dev/verify.sh` of the pinned SHA; workflow edits cannot run code |
| Needs REPLI workflow change | **yes** (`runs-on`, governed) | no |
| Needs GitHub admin | yes (runner token) | yes (App/PAT) |
| Minutes consumed | none while jobs run on self-hosted | none |

## Decision
Choose **A**. No blocking obstacle was found: the only cost is a governed REPLI workflow change, which is required
for any approach that is meant to replace the hosted check. Not re-implementing the runner/check protocol removes the
largest source of custom security-sensitive code (token handling, status publishing, webhook/poll races).

**Revision 2 (review repair).** Revision 1 had the workflow call `repli-local-ci run`, which fetched
`pull_request.head.sha` over SSH. That is wrong: it replaces GitHub's PR integration (merge) commit with head-only content and
duplicates checkout, secrets handling and identity that the official runner already owns. Now the **official runner owns job
delivery, check identity and the checkout of `GITHUB_SHA`**; the manager owns pinned install, registration gate, launchd
lifecycle, readiness (`doctor`), runner hooks (same-repo enforcement outside the workflow, cleanup), and local reporting.
`run` survives only as a standalone manual command and is not used by the proposed workflow.

Additional decisions: (1) a dedicated standard macOS user or disposable VM is a mandatory production precondition, enforced
by `doctor`/`register`/`start`; (2) the manager is installed to `~/.local/bin` independent of the checkout; (3) the runner version
and SHA-256 are pinned in-repo and compared against official metadata only by an explicit command/CI job; (4) fork refusal is
enforced twice (workflow `if:` + runner hook) and a same-named hosted job fails fork PRs so "skipped" cannot pass a required check.

## Consequences / limitations
- A self-hosted runner executes repository code on the user's Mac. It is **not** equivalent to a throw-away
  GitHub-hosted VM (no VM boundary, persistent disk, user's home and network reachable). See THREAT-MODEL.md.
- The workflow file at the PR head decides what the runner executes; same-repo branch writers can change it. The
  mitigation is restricting who can push branches to REPLI, not this tool.
- macOS differs from ubuntu-24.04 (BSD userland, bash 3.2, no apt, no containers); `doctor --workflow` flags known
  incompatibilities and nothing is silently skipped.
- Runner auto-update stays enabled (GitHub refuses very old runners); the pinned version is the *install* baseline.
- Not validated: no real runner has been registered or run; hosted-macOS CI only exercises the synthetic tests.
- Local results are not the REPLI canonical merge gate until REPLI governance says so.

## Revisit B if
REPLI refuses any workflow change, or if running the PR-head workflow on the Mac is judged unacceptable. B would then
need a GitHub App with minimal permissions and a separate ADR.
