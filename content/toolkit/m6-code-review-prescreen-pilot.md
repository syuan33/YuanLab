---
title: "Can a local 27B model prescreen code reviews?"
date: 2026-11-05
description: "A small M6 pilot completed most review packages but found no verified blocker early enough to save a review round."
tags: ["llm", "code-review", "benchmark", "local-ai"]
toc: true
---

I tested whether a local model could inspect a code-review package before the primary reviewers and catch a blocker early. The practical goal was to avoid a paid repair-and-rereview round. In this pilot, the answer was **no demonstrated savings**. The model completed most jobs, but none of its checked findings was an actionable blocker that would have avoided a later review round.

This is a result for one model, one prompt design, and a small set of historical packages. It is not a general verdict on local code review.

## Setup

The machine was a 32 GB M6 Mac mini running `Qwen3.8-27B-oQ4e-mtp`. The server-reported model ID was checked on every request. Requests were serialized, with one model request in flight at a time. Thinking was off, temperature was 0.2, and each code chunk was limited to about 30,000 input tokens. Packages with multiple chunks received a final merge request. A failed request could be retried once.

The sample contained 14 historical review heads from one private project. Thirteen completed; one failed because a chunk returned malformed JSON on both attempts. Eleven heads were in a RED test phase and three were GREEN governance or documentation changes. There were no GREEN code changes or clean, accepted controls. Inputs were frozen before comparison with the primary reviewers, but finding labels were assigned with the model and reference summaries visible side by side, so verification was not fully blind.

The prompt deliberately omitted RED/GREEN phase information. That choice made the model treat some intentionally failing RED stubs as production defects. The private source, prompts, raw responses, paths, and identifiers are omitted here.

## Results

| Measure | Observation |
|---|---:|
| Historical heads | 14 sampled; 13 completed |
| Model requests | 64 attempts, including 4 retries |
| Wall-clock time | 2.13 hours, including the failed head |
| P0–P2 candidates | 76 |
| Verified actionable blockers found early | 0 |
| Candidate findings not individually checked | 19 |
| Primary-review P0–P2 findings | 64; roughly 58 had relevant input coverage |
| Clear or partial overlaps with primary findings | 1 clear, 2 partial |
| Repair rounds demonstrably avoided | 0 |

The clear overlap described an intentionally wrong RED stub. It did not represent an early repair opportunity. The two partial overlaps were on governance semantics. Among the 76 candidates, 17 described expected RED stubs, 7 were contradicted by source, 19 depended on context outside the chunk, 9 were valid but nonblocking notes, 2 were other true nonblocking observations, 1 was the clear overlap, 2 were partial overlaps, and 19 were not individually checked. The unchecked group means the count of genuine findings remains uncertain.

Against the roughly 58 primary findings whose context appeared in the input, the estimated finding overlap was about 2–5%, depending on whether partial matches count. This is an approximate comparison, not a ground-truth recall score: context coverage was estimated from identifiers, and the primary reviewers' findings were the reference set.

The model's overall verdict was also unhelpful for screening. Every completed head received `repair` or `needs-human`, and the sample had no clean controls from which to estimate a false-alarm rate. A verdict that always asks for more work cannot identify which package needs early attention.

## What worked and what did not

The local service ran without server or memory interruptions. The model copied the supplied input hash, reviewed commit ID, and prescreen ID correctly in 47 parseable responses. It misreported its own `provider` field in all 47; the prompt's loose wording contributed to that error. An independent harness should record provenance rather than rely on the model's self-description.

Chunking kept the requests within memory limits, but it hid cross-file context. Nineteen candidate findings could not be judged from the chunk shown to the model. A merge reduced 100 chunk-level findings to 90 across the multi-chunk heads. Three high-severity findings lacked a match in the merged output; all three were judged noise. That observation does not establish that merging would preserve a real blocker.

This pilot measured a possible **prescreen**, not the model's usefulness as a low-weight advisory voice alongside stronger reviewers. Earlier, smaller benchmarks exercised other model builds and prompt sizes, so their results cannot be combined into a quality claim for this configuration. A successful local run is evidence of operational feasibility; finding useful defects requires separate evidence.

## Decision and next test

I would not put this prescreen in the default review path based on these data. It added model time and human triage, while the estimated avoided repair rounds and realized savings were both zero. The estimate for primary-review input cost was based on package size, not measured billing, and prescreening did not reduce the required primary review.

A useful follow-up would include accepted clean heads, GREEN code changes, phase-aware prompts, complete relevant context, a primary-reviewer baseline on the same inputs, and blind checking of every P0–P2 candidate. Those changes would test whether the weak signal came from the model, the 30k chunk boundary, or the pilot design. Until then, its output is best treated as a set of leads to verify against source, never as a pass or fail gate.
