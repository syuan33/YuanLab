---
title: "Local Code-Review Models on a 32 GB Mac"
date: 2026-11-05
description: "Which local model to load for long-context code review on a 32 GB Apple-silicon Mac as of end of October 2026: Ornith 1.5 35B-A3B (oQ3e, oQ4e) versus Qwen3.8-27B, with method, numbers and limits."
tags: ["llm", "mlx", "omlx", "benchmark", "code-review"]
toc: true
draft: true
---

> **Abstract.** I measured four local model builds as *advisory code reviewers* on a Mac mini with 32 GB of
> unified memory, serving long review prompts (up to about 135k tokens) through oMLX. The question was practical:
> which build should be loaded when a third, cheap, private reviewer is wanted next to two frontier reviewers?
> On this machine the limiting resource is memory headroom for the KV cache, not raw model quality.
> **Ornith 1.5 35B-A3B at the lower-bit `oQ3e` quantization** was the fastest option (a 135k-token package in
> 4.3-5.3 minutes versus 15.5 for Qwen3.8-27B) and ran from 19k-token chunks up to a single 132k request without a
> memory abort. **Qwen3.8-27B `oQ4e`** is slower but leaves similar headroom and is the dense, more conventional fallback
> (this study did not show a clear merge-reliability difference; see §4.6). Every build under-called subtle contract gaps that the frontier reviewers found,
> and none of the three builds tested passed any of nine implement-from-tests tasks. Local models are usable as a screening vote, not as a gate
> and not as an implementer. All numbers are single runs; limits are in [section 7](#7-limits).

## 1. Introduction

A review process that needs three independent opinions gets expensive in tokens, and some of the material cannot
leave the local network. A local model on a LAN box looks like a natural third reviewer. The questions are
concrete:

1. Can a 32 GB Mac serve a review prompt of 40k-135k tokens at all, without being killed by memory pressure?
2. Which model build and which request size give the best speed, stability and review quality?
3. What must the surrounding harness do so that the output can be trusted?

The input is a **review package**: a small header, notes from the author, a unified diff, the full text of changed
source and test files, and an excerpt of a design document. The reviewer returns one JSON object with a verdict
(`accept`, `repair` or `needs-human`), ranked findings (P0-P3) and the list of things it did not check. Reference
verdicts for each package came from two frontier reviewers (Claude and Codex) that had reviewed the same text.

## 2. Hardware and models

**Machine.** Mac mini, Apple silicon, 32 GB unified memory, on a LAN. oMLX serves several models behind one
OpenAI-compatible endpoint; naming a model in a request loads it and unloads the previous one (about 6-9 s). Its
static memory cap was 29 GB; the dynamic ceiling moved between 24.3 and 26.9 GB with other load on the machine.

| Label | Model | Quantization | Resident | Server |
|---|---|---|---|---|
| Qwen3.8 Q4 (mlx-dspark) | Qwen3.8 27B, dense | 4-bit, with a DFlash draft model | ~23.6 GB (whole box) | mlx-dspark |
| Qwen3.8 oQ4e (oMLX) | Qwen3.8 27B, dense | `oQ4e`, multi-token prediction | 18.4 GB | oMLX |
| Ornith oQ4e | Ornith 1.5 35B-A3B, mixture of experts | `oQ4e`, multi-token prediction | 22.65 GB | oMLX |
| Ornith oQ3e | Ornith 1.5 35B-A3B, mixture of experts | `oQ3e`, multi-token prediction | 17.4 GB | oMLX |

In the build names, `mtp` marks multi-token prediction (used here for faster decoding) and the `oQ*e` quantizations were
chosen for better MLX performance. oMLX was version 0.7.3 (build 3574).

Model sources and revisions are in the notes.[^src] The Ornith builds hold all 35B parameters in RAM even though only about 3B are active per token. That is why they
decode quickly but need much more memory than their speed suggests.[^naming]

{{< fig name="fig-memory" >}}
**Figure 1.** Resident model memory against the oMLX guard. Ornith oQ4e sits at the guard and leaves little headroom for
KV cache (it ran 19k-token requests but not 30k); Qwen3.8-27B and Ornith oQ3e leave about 8 GB.
{{< /fig >}}

## 3. Method

- **Prompts.** Eight real review packages of 42k-107k tokens (136-401 KB of text) plus one synthetic 132k-token
  package: the largest real package, padded with unrelated design text, with a planted *needle* (a codeword in the
  middle) to test retrieval. The same package was reused for the chunked comparison across models.
- **Strictly sequential.** One request in flight at any time, enforced with a lock file. A shared box must not be
  asked to do two things at once, and a second client switching models aborts the first one's request.
- **Settings.** Temperature 0.2, `max_tokens` 4096-8000, thinking off (section 5), a closing reminder appended:
  *"you have NO tools; everything is inline; reply with the single JSON object only."*
- **Chunking.** For packages that do not fit in one request, files are split at line boundaries into chunks of
  19k, 30k, 50k or 100k tokens with 3-5k overlap. Each chunk is reviewed in its own request, then a merge request
  deduplicates the findings ([Figure 2](#fig-pipeline)).
- **Scoring.** Throughput and memory come from the server's own usage and health output. Review quality is scored
  by hand against the two reference reviewers' findings; verdict agreement alone is a weak bar (section 4.4).

{{< fig name="fig-pipeline" >}}
**Figure 2.** Chunked review. In the one like-for-like pair (§4.3) the merge turned 47 chunk-level findings into 16 (oQ4e) and 37 into 10 (oQ3e). How many of the
reduction was legitimate de-duplication and how many were dropped findings was not counted item by item.
{{< /fig >}}

## 4. Results

### 4.1 Speed, memory and request size

| | Qwen3.8 Q4 (mlx-dspark) | Qwen3.8 oQ4e (oMLX) | Ornith oQ4e | Ornith oQ3e |
|---|---|---|---|---|
| Prefill tok/s | 157-235 | ~250-280 | ~1,200 | 660-1,840 (falls with length) |
| Decode tok/s | 3.4-13.8 | 18-21 | 68-87 | 18-79 (falls with length) |
| Largest request that ran | 132k, one shot | ~42k one shot; 30k chunks | ~19k chunks (30k aborted) | 132k one shot |
| 135k package | not chunked | 6 chunks + merge, ~15.5 min | 10 chunks + merge, ~6.4 min | 4.3-5.3 min, any chunking |

For the Qwen3.8 Q4 build on mlx-dspark, decode falls from 13.6 tok/s at 45k tokens to 3.4 tok/s at 132k, so a 600-second deadline
that suits 40k-token prompts fails above roughly 80k tokens ([Figure 3](#fig-decode)).

{{< fig name="fig-decode" >}}
**Figure 3.** Decode speed against prompt length, Qwen3.8 27B Q4 on mlx-dspark, eight real runs
and one synthetic.
{{< /fig >}}

{{< fig name="fig-wallclock" >}}
**Figure 4.** Wall-clock time for the same ~135k-token package. Grey: builds that cannot run larger chunks. Blue:
Ornith oQ3e at every chunk size tried, plus one shot.
{{< /fig >}}

The deciding factor was memory. oMLX can admit a request and then abort it mid-prefill when process memory crosses
a hard watermark below the dynamic ceiling: Ornith oQ4e was aborted at 30k tokens (23.5 GB used against a 23.2 GB
watermark) and ran cleanly at 19k. The lighter oQ3e build ran 30k, 50k, 100k and 132k requests with resident memory
flat at 17.4 GB. For Qwen3.8-27B the largest *measured* single request was ~42k tokens (one run). A 135k one-shot was *estimated* at
25.9 GB peak (17.6 GB weights + 8.3 GB KV/attention, extrapolated from the server's memory estimate) against a 22.7 GB
limit and was not attempted; a ~80k-token ceiling is that estimate scaled down, not a measurement. A 70k request was
cut short when another client swapped the model, so the range between 42k and 80k is untested.

### 4.2 Chunk size (Ornith oQ3e, same 135k package)

| Unit | Requests | Time | Per-chunk verdicts | Merge verdict | Findings P0/P1/P2/P3 |
|---|---|---|---|---|---|
| 19k × 10 + merge | 11 | 318 s | 8 needs-human, 2 repair | needs-human | 2/1/5/2 |
| 30k × 6 + merge | 7 | ~4.7 min | mixed (4 different verdicts) | needs-human | 1/1/5/0 |
| 50k × 4 + merge | 5 | ~4.6 min | all needs-human | needs-human | 0/0/4/5 |
| 100k × 2 + merge | 3 | ~4.3 min | repair, needs-human | repair | 0/1/3/3 |
| one shot, 132k | 1 | 267 s | n/a | **accept** | 0/0/0/3 |

Larger units gave more consistent verdicts but fewer and milder findings. The one-shot run retrieved the planted
codeword and returned perfect JSON, yet judged the package `accept` with three P3 notes where both reference
reviewers asked for changes. The 100k run **lost** the codeword: it sat 33k tokens into a 44.7k chunk, the chunk
reply omitted it, and the merge wrote the chunk's verdict in its place. A 133k context *fits and retrieves*; review
judgement softens toward the far end of it. Both effects are single samples.

### 4.3 Quantization: `oQ3e` against `oQ4e` at equal chunk size (19k)

| | Ornith oQ4e | Ornith oQ3e |
|---|---|---|
| Whole package | 382 s | **318 s** |
| Decode tok/s | 68-81 | 55-63 |
| Chunk-level findings (P0/P1/P2/P3) | 47 (0/6/23/18) | 37 (1/4/21/11) |
| Merge | 16 findings, 0 P0 | 10 findings, **2 P0** |
| Header-field copy errors | 0 of 10 | 1 of 11 |
| Shared item raised by both references | kept in merge | **dropped by merge** |

The finding counts above are raw model output; I did not check them item by item, so a higher count or a smaller
count here does **not** show that either quantization reviews better. The only quality-relevant observations are the
dropped shared item and the two merge-level P0s, which were not confirmed as real defects.

The smaller build also made one visible mistake: it copied a 64-character hash with one wrong digit, and the
merge step then reported that mismatch as a P0 defect in the input. The same kind of error appeared once in an
Ornith oQ3e 30k run and once in Qwen3.8 (a commit hash), and never in the oQ4e build. Three cases are not a rate. One *hypothesis* is that lower-bit quantization hurts exact long-string transcription; this
study did not test it (it would need many replicates per build). **Never trust the
model to echo identifiers; write them from the harness.**

### 4.4 Review quality against the two reference reviewers

On the one package where every variant was compared, the reference reviewers disagreed with each other
(`repair` against `needs-human`) and shared exactly one finding. Any verdict other than `accept` therefore matched
one of them, so verdict agreement says little. By hand:

- **No variant found either reference reviewer's P2 findings.** Both are "this test is missing" findings that need
  a cross-read of the design text against the test list.
- The only overlap was on items the author's own notes already disclosed. Keyword scoring first suggested 5-7 of 7
  reference items for the small-chunk runs; hand-checking showed those were false matches.
- The best overlap (3 of 7) came from the Qwen3.8 Q4 (mlx-dspark) build in a one-shot 107k run, so "one shot is worse" is not a
  general result.
- Small chunks inflate harshness: the prompt asks the model to list what it could not see, and the small-chunk runs
  turn that into findings ("source under test not shown"). That happened to align with the references here and
  would produce false alarms on a clean package; no clean control was run.


**Agreement with each reference reviewer.** Rows are the local model's verdict, columns the reference reviewer's. Eight real
review packages, Qwen3.8 Q4 (mlx-dspark), one run each; the second reference was absent on one package.

| vs Claude (8 packages) | ref accept | ref repair | ref needs-human |
|---|---|---|---|
| model **accept** | 0 | **3** | 0 |
| model **repair** | 0 | 3 | 0 |
| model **needs-human** | 1 | 1 | 0 |

| vs Codex (7 packages) | ref accept | ref repair | ref needs-human |
|---|---|---|---|
| model **accept** | 0 | **3** | 0 |
| model **repair** | 0 | 2 | 0 |
| model **needs-human** | 0 | 0 | 2 |

Exact-label agreement is 3 of 8 with Claude and 4 of 7 with Codex. The two references agreed with each other on 5 of 7 packages
where both reported. Collapsing to "asks for changes" against "accept": the model asked for changes on 5 packages and was
never contradicted by *both* references on those, but it said `accept` on 3 packages where **both** references said `repair`
(a false-accept rate of 3 of 8). Every package here needed changes according to at least one reference, so **specificity (how
often a clean package is accepted) cannot be computed**: there was no clean control.

**On the one shared package** (Claude: `repair`, Codex: `needs-human`), merge-level verdicts of eight variants:

| Variant | Verdict | = Claude | = Codex |
|---|---|---|---|
| Qwen3.8 Q4 (mlx-dspark), one shot 107k | needs-human | no | yes |
| Qwen3.8 oQ4e, 6×30k | repair | yes | no |
| Ornith oQ4e, 10×19k | needs-human | no | yes |
| Ornith oQ3e, 10×19k | needs-human | no | yes |
| Ornith oQ3e, 6×30k | needs-human | no | yes |
| Ornith oQ3e, 4×50k | needs-human | no | yes |
| Ornith oQ3e, 2×100k | repair | yes | no |
| Ornith oQ3e, one shot 132k (padded) | **accept** | no | no |

That is 2 of 8 with Claude and 5 of 8 with Codex, and 7 of 8 not `accept`. As the reviewers split, a verdict other than `accept` is
bound to match one of them, so this is a weak test of anything beyond "does it refuse to accept".

**Item-level overlap with the reference findings** (hand-checked; Claude had 5 distinct findings, Codex 3, one shared). Partial
or related matches are shown as ranges.

| Variant | Claude items found | Codex items found | Either reviewer's P2 found |
|---|---|---|---|
| Qwen3.8 Q4 (mlx-dspark), one shot 107k | 2 / 5 | 2 / 3 | no |
| Qwen3.8 oQ4e, 6×30k | 1-2 / 5 | 1 / 3 | no |
| Ornith oQ4e, 10×19k | 1-2 / 5 | 1 / 3 | no |
| Ornith oQ3e, 6×30k | 1-2 / 5 | 1 / 3 | no |
| Ornith oQ3e, 4×50k | 1 / 5 | 1 / 3 | no |
| Ornith oQ3e, 2×100k | 1 / 5 | 0 / 3 | no |
| Ornith oQ3e, one shot 132k | 1 / 5 | 1-2 / 3 | no |

The shared finding is the main overlap; no variant found a reference reviewer's "this test is missing" P2.

On the eight earlier real packages with the Qwen3.8 Q4 (mlx-dspark) build, 5 of 8 verdicts matched a reference. All three misses
were `accept` where both references said `repair`. Every `repair` or `needs-human` it issued had a matching
reference verdict.

### 4.5 Coding from tests (negative result)

Each model was asked to implement three small existing modules from the real test file, interface declarations and
one sibling for style, never the answer. Output was linted, compiled with the project's real build and run, with
one repair round that fed back the actual compiler output. Reference implementations passed in the same harness.

**0 of 9 passed** (3 models × 3 tasks; the Qwen3.8 Q4 build on mlx-dspark was not run on this task). The closest: Qwen3.8-27B compiled one task and passed 8 of 10 assertions.
Failures were unbalanced brackets and unresolved symbols. The repair round mostly returned the same code
(7 of 9 near-identical or identical) even with the error text in the prompt. This was a Lisp-family language
(ClojureScript), which is unforgiving about brackets, so it may be harder than the average language, and the
three tasks are not a sample of coding in general. No frontier baseline was run. Conclusion: *these nine attempts all
failed*; it is not evidence about other languages, larger repair budgets or other prompts.

### 4.6 Hand-checking the merge-level findings

To test the claim that a higher finding count is not a higher accuracy, I read the merge-level P0-P2 findings of the four chunked
runs on the shared package against the package text (the test, the code it exercises, and the design excerpt): 30 findings, of
which I checked 19. The other 11 were not checked and are not counted below.

| Merge (findings checked / P0-P2 total) | False or unsupported | Expected at RED (accurate description of a deliberate stub) | True and already disclosed by the author | New real defect |
|---|---|---|---|---|
| Qwen3.8-27B, 6×30k (4 / 5) | 1 | 3 | 0 | 0 |
| Ornith oQ4e, 10×19k (8 / 14) | 4 | 2 | 2 | 0 |
| Ornith oQ3e, 10×19k (3 / 7) | 3 | 0 | 0 | 0 |
| Ornith oQ3e, 4×50k (4 / 4) | 1 | 1 | 2 (one is the planted codeword) | 0 |

What "false or unsupported" looked like:

- A test said to contradict its own docstring (Qwen3.8 and oQ4e): the test was *inverted on purpose* by the design decision it
  cites, and the docstring says so.
- A test called a vacuous control (oQ4e, twice): one asserts an exact literal key set and a forbidden-key check; the other
  restores the console *after* capturing a buffer that already holds the canary.
- "A try/catch swallows all values including non-Error" (oQ3e 19k, a **P0**): the code catches `js/Error` explicitly, so the
  premise of the finding is wrong.
- "Hash mismatch between chunk 9 and the header" (oQ3e 19k, **P0**): the model's own mis-copy of a hash, reported as a defect.
- "reviewed SHA differs between headers" (oQ3e 19k): the package holds one reviewed SHA and a different *base* SHA; no mismatch.
- "A test asserts a constant stub equals a real SHA-256" (oQ3e 50k): that is a deliberately failing RED test, and the package
  states that 10 of its 12 assertions fail by design.

Reading, with small numbers: **none of the 19 checked findings was a new real defect**, in any build. The false rate among the
checked ones was lowest for Qwen3.8 (1 of 4) and the oQ3e 50k merge (1 of 4), and highest for the oQ3e 19k merge (3 of 3, both of
its P0s). That supports "the oQ3e 19k merge should not be trusted" and "a lone P0 needs a source check", but it does **not**
establish a ranking between Qwen3.8 and Ornith: the samples are tiny and the unchecked findings could change any row. It also
does not measure recall, which the earlier comparison against the two reference reviewers already showed to be low for all of them.

## 5. Failure modes

1. **Thinking mode never finishes.** On a 42k prompt with an 8,192-token budget the model spent the whole budget
   in its reasoning channel and produced no answer. Turn thinking off for review.
2. **Hallucinated tool calls.** Without the closing "you have no tools" reminder the model emitted a shell-command
   block instead of a review. With the reminder this did not recur in any run.
3. **Wrong self-identification.** Every model filled `"provider"` with `"claude"`. Record provider, model and
   quantization from the harness, not from the reply.
4. **Under-calling.** Subtle contract gaps were rated P3 or ignored, giving an `accept` against reviewers who asked
   for changes. This is the dangerous direction. It was worst for long one-shot prompts.
5. **Severity inflation on intended stubs.** Test-first work has deliberately wrong placeholder code; models
   repeatedly flagged it as a P0 defect. Tell the reviewer the phase.
6. **Malformed JSON** in roughly one of every 6-10 chunks (a stray brace; a fenced block that still failed to
   parse). A tolerant parser and one retry per chunk is needed or that chunk's findings vanish silently.
7. **Errors inside HTTP 200.** A memory abort arrives as the text `[Error: Request aborted` in a successful
   stream. Scan content for it.
8. **Model swaps by another client** abort your in-flight request ("model is being unloaded"). One operator at a
   time.
9. **No prefix reuse** between different chunks, even with a shared instruction head; only a byte-identical repeat
   hit the cache.

## 6. Recommendations: which model, for what

| Use | Pick | Setup |
|---|---|---|
| Advisory reviewer, speed matters | **Ornith 1.5 35B-A3B `oQ3e`** | 50k-token chunks, 5k overlap, merge; one shot only if you can afford its softer verdict |
| Advisory reviewer, dense-model fallback | **Qwen3.8-27B `oQ4e`** | 25-30k chunks with 5k overlap plus merge (measured); one shot only up to the ~42k actually run (larger is an estimate); slower (~15 min per 135k). A fallback, not shown to be better at merging |
| Short context, throughput first | Ornith `oQ4e` | at most ~19k tokens per request on a 32 GB machine |
| Implementer in a test-first loop | none tested | 0 of 9 passed in this study |

Harness requirements for any of them:

- Overwrite identifier fields (hash, commit, id, phase, provider) with harness-held values, and strip them from the
  merge input.
- Tolerant JSON parse and one retry per chunk; scan for `[Error` inside 200 responses.
- Closing reminder: no tools, JSON only. Thinking off. Tell the reviewer the phase and what is intentionally a stub.
- Deadline of at least 1,500 s for a single request above about 80k tokens on the Qwen3.8 Q4 (mlx-dspark) build (measured: 1,072 s at 132k).
- Prevent the merge from dropping a finding raised by two or more chunks. Treat a lone P0 from a local model as
  a claim to check against the source, not as a result.
- Treat the local verdict as a screening signal. An `accept` from it is no evidence when a stronger reviewer
  disagrees; a `repair` or `needs-human` is worth reading.
- One request at a time, one operator at a time.

## 7. Limits

- **Single runs.** One run per configuration at temperature 0.2, so run-to-run variance is unmeasured.
- **One package for the chunked comparison**, plus one synthetic variant. Verdict match is one data point per
  model.
- Ornith finding counts and severities were **not verified against the code** one by one. More findings is not
  more correct findings.
- Verdicts are compared with two frontier reviewers' outputs, not with ground-truth defects.
- Quantization effect is confounded with chunk size for `oQ4e` (it cannot run larger chunks). The like-for-like
  19k comparison isolates it, but with one run each.
- The recommendation order between the two models rests on speed, memory and request-size evidence. There is no
  same-condition merge-accuracy comparison, and the review-quality evidence is one package.
- Not tested: thinking mode on the new builds; short-prompt orchestration workloads; 200k contexts; the effect of
  draft-model and multi-token-prediction settings; any frontier baseline for the coding task; lower-bit quality
  loss on a ground-truth set.
- Memory and ceiling figures are the server's own reports and moved during the day.

## 8. Reproducing this

The review packages are private and are not published. Build an equivalent one: a short header, a unified diff
of a few files, the full text of those files and their tests, a design excerpt, then (for the retrieval test)
padding plus a one-line codeword in the middle. The request shape is plain OpenAI-compatible chat:

```bash
BASE=http://<your-mac>.local:8000          # oMLX endpoint
KEY=<your key>                              # not published here

curl $BASE/health -H "Authorization: Bearer $KEY"        # memory ceiling, loaded model
curl $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"Qwen3.8-27B-oQ4e-mtp",
       "messages":[{"role":"user","content":"<review package ...>\n\nYou have NO tools. Reply with one JSON object only."}],
       "max_tokens":4096, "temperature":0.2,
       "chat_template_kwargs":{"enable_thinking":false}}'
```

The reply schema used: `verdict`, `summary`, `findings[]` (severity, title, file, line, contract violated, how to
check), `checked[]`, `not_checked[]`. Run one request at a time, record the model and quantization yourself,
and keep the raw output of every request, including errors.

## Conclusion

For a 32 GB Apple-silicon Mac in late October 2026, load **Ornith 1.5 35B-A3B at `oQ3e`** when you want a fast
advisory pass over long review packages, and **Qwen3.8-27B at `oQ4e`** when you want a slower dense
model as the fallback (it was not shown to merge more reliably than Ornith). Neither replaces a frontier reviewer: all builds missed what the
frontier reviewers' subtle findings caught, and none of the three builds tested passed any of the nine implement-from-tests tasks. Their value is a private, cheap,
extra look, used with a harness that does not trust them for identifiers, severities or verdict authority.

## References and notes

[^naming]: Two different Qwen3.8 27B builds appear: a 4-bit build with a DFlash draft model on the mlx-dspark server ("Qwen3.8 Q4
    (mlx-dspark)"), and the oQ4e multi-token-prediction build on oMLX ("Qwen3.8 oQ4e"). They are different files on different
    servers and are not interchangeable in the results. Qwen3.7 was never tested; an earlier internal note mislabelled the
    mlx-dspark build with that name.

    Exact model IDs as served by oMLX 0.7.3 (build 3574) when the tests ran: `Qwen3.8-27B-oQ4e-mtp`,
    `Ornith-1.5-35B-A3B-oQ3e-mtp`, `Ornith-1.5-35B-A3B-oQ4e-mtp`, and `qwen3.27b.q4` (Qwen3.8 27B Q4 with a DFlash drafter, mlx-dspark server).
    An earlier, heavier Ornith build with fp16 parts (`…-oQ4e-fp16-mtp`, 23.6 GB) was also tried and rejected for size.

[^src]: Model sources.
    - Qwen3.8-27B oQ4e: [`jundot/Qwen3.8-27B-oQ4e-mtp`](https://huggingface.co/jundot/Qwen3.8-27B-oQ4e-mtp), Hugging Face revision `7dd276b` at 2026-11-05.
    - Ornith 1.5 35B-A3B oQ4e: [`scottlowry/Ornith-1.5-35B-A3B-oQ4e-mtp`](https://huggingface.co/scottlowry/Ornith-1.5-35B-A3B-oQ4e-mtp), revision `1d2d5d4` at 2026-11-05.
    - Ornith 1.5 35B-A3B oQ3e: `scottlowry/Ornith-1.5-35B-A3B-oQ3e-mtp`. At the time of writing this repository page was not publicly reachable (it may be private, renamed or gated), so it is named but not linked, and no revision is recorded.
    The revisions are the repositories' *current* heads when this post was written, not hashes captured at test time, so
    a later upload under the same name would not be visible in this record. Model-file hashes were not recorded.

## Appendix A. Per-run table, Qwen3.8 27B Q4 on mlx-dspark, real review packages

One run each, temperature 0.2, thinking off. "Match" is against the two frontier reviewers; `(Codex)` means it matched
only one of them, and ✗ means `accept` where both asked for changes.

| Run | Prompt tokens | Prefill s | Prefill tok/s | Decode tok/s | Total s | Verdict | Match |
|---|---|---|---|---|---|---|---|
| P1 | 41.9k | 15 (cache hit) | n/a | 13.2 | 62 | accept | ✗ |
| P2 | 45.1k | 193 | 234 | 13.6 | 294 | repair | ✓ |
| P3 | 54.5k | 232 | 235 | 13.8 | 442 | repair | ✓ |
| P4 | 64.1k | 326 | 197 | 11.0 | 400 | repair | ✓ (one reference absent) |
| P5 | 69.0k | 320 | 215 | 9.9 | 410 | needs-human | ✓ (Codex) |
| P6 | 70.1k | 317 | 221 | 9.7 | 501 | accept | ✗ |
| P7 | 86.0k | 473 | 182 | 6.7 | 581 | accept | ✗ |
| P8 | 106.9k | 645 | 166 | 5.2 | 877 | needs-human | ✓ (Codex) |
| S1 (synthetic) | 132.4k | 844 | 157 | 3.4 | 1,072 | repair | not scored |

## Appendix B. What the review comparison counted

Hand-checked reference overlap on the shared package (7 reference items; two frontier reviewers): Qwen3.8 Q4 (mlx-dspark)
one-shot 107k: 3; Qwen3.8 oQ4e 6×30k: 1-2; Ornith oQ4e 10×19k: 1-2; oQ3e 6×30k: 1-2; oQ3e 4×50k: 1; oQ3e 2×100k: 1;
oQ3e one-shot 132k: 1-2. Ranges mean the match was partial or related rather than the same finding.
