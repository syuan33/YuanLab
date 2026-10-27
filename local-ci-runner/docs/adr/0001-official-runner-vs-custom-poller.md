# ADR 0001: Official self-hosted runner + management layer (A) vs custom PR poller (B)

Status: Proposed (needs independent review)

## Context
A repository's required check currently runs on GitHub-hosted runners and consumes Actions minutes. We want the same job to
run on a local Apple Silicon Mac, safely and reproducibly, without re-implementing GitHub's job or check protocol.

## Options
**A. Official GitHub Actions self-hosted runner**, plus a thin manager (`local-ci-runner`) for pinned install, a registration
gate, launchd lifecycle, readiness checks, runner hooks, cleanup and local reports.
**B. Custom poller** that lists PRs, fetches a SHA, runs verification and publishes a commit status / check.

## Comparison
| Concern | A | B |
|---|---|---|
| Check identity, PR integration, re-run button, logs | native | must be re-implemented (needs an App or PAT with write scope) |
| Credentials on the Mac | one repo-scoped runner credential; registration token used once | long-lived write credential plus read access to code |
| Job protocol, cancellation, timeouts | maintained by GitHub | ours to build and keep correct |
| Checkout of the PR integration (merge) commit | `actions/checkout` of `GITHUB_SHA` | would verify head-only content unless re-implemented |
| Code executed from a PR | the PR-head workflow file runs (hooks narrow, do not remove, this) | only the verify entrypoint of a pinned SHA |
| Needs workflow change in the target repo | yes | no |

## Decision
Choose **A**. The official runner owns job delivery, the check identity and the checkout of `GITHUB_SHA`. The manager owns:
pinned and checksum-verified runner install, the registration gate, launchd lifecycle, `doctor` readiness, runner hooks
(allow-list enforcement outside the workflow, cleanup), and local reports. `run` remains only as a *standalone* manual command
and is not used by any workflow.

Further decisions:
1. The repository allow-list lives **only** in a protected local config (`config.json`, mode 0600, directory 0700, owner-checked,
   checksummed, created exclusively by the approved `register --apply` step). Workflows, PR content and environment variables
   cannot widen it; hooks fail closed when it is absent or insecure.
2. Exactly one check context (`verify`). No job-level `if:` and no second same-named job: forbidden runs are *failed* by the
   job-started hook, never skipped (a skipped required check passes).
3. A dedicated standard macOS user or disposable VM is a mandatory production precondition, enforced by `doctor`/`register`/`start`.
4. The manager installs itself to `~/.local/bin` independent of the source checkout.
5. Runner version and SHA-256 are pinned in-repo and compared with official metadata only by an explicit command / CI job.

## Consequences / limitations
- A self-hosted runner executes repository code on your machine and is **not** equivalent to a throw-away hosted VM.
- The workflow file at the PR head decides what runs after the hook passes; same-repo branch writers can change it (open risk).
- macOS differs from ubuntu images (BSD userland, bash 3.2, no apt, no containers); `doctor --workflow` flags known incompatibilities.
- Not validated: no real runner has been registered or run. Hosted-macOS CI only exercises synthetic tests.
- Do not attach a self-hosted runner to a public repository that accepts fork pull requests.

## Revisit B if
the target repository will not accept a workflow change, or running the PR-head workflow on the Mac is judged unacceptable.
