---
title: "M6 as a Long-Document Agent"
date: 2026-10-23
description: "What a 27B model on a 32 GB Mac mini does behind the Serial LLM Gateway when you feed it long inputs: latency by prompt size, queue behavior, and two ways to chain a long task."
tags: ["llm", "mlx", "benchmark", "self-hosted"]
toc: true
---

## What it is

The [Serial LLM Gateway](../serial-llm-gateway/) puts a one-at-a-time queue in front of a single local model.
This post is the first real-model test of it: a 27B Qwen model served by `mlx-dsppark` on a Mac mini with about
32 GB of unified memory, with a 32,768-token context window configured. The question was practical: **can this
machine act as an agent that chews through long text, and what does that cost in time?**

Short answer: yes, if you feed it in slices of a few thousand tokens and carry state forward in a short running
summary. A single call with a very long prompt works too, but latency climbs steeply past about 30k tokens.

## At a glance

| | |
|---|---|
| **Model** | 27B Qwen, `dflash` mode, 32,768-token window configured |
| **Hardware** | Mac mini, ~32 GB unified memory, model native on macOS, gateway in Docker |
| **Client** | Python standard library only, non-streaming calls, thinking disabled, `temperature` 0 |
| **Date** | 2026-10-23 |
| **Status** | One session on an otherwise idle machine; numbers are indicative, not a soak test |

## How it was measured

Every call went through the gateway, so the numbers include the queue. Prefill time, decode time and token counts
come from the timing block the model server returns with each response. Prompts were synthetic text (no real
documents), so the results say nothing about answer quality beyond the recall test described below.

Four experiments, in order: a prompt-size ramp, an 11-client burst, a rolling-summary chain, and a
grow-everything chain.

## Results

### Latency by prompt size

One request at a time, 8 output tokens, so the time to first token is essentially prefill.

| Prompt tokens | Time to first token |
|---:|---:|
| 466 | 5 s |
| 1,812 | 9 s |
| 5,322 | 21 s |
| 10,650 | 44 s |
| 19,461 | 94 s |
| 24,798 | 116 s |

Prefill runs at roughly 200 to 260 tokens per second and slows a little as the prompt grows. Decode speed was far
less stable. The very first call decoded at only 1.3 tokens per second while the machine was still paging heavily;
during the chain runs it settled at 13 to 24 tokens per second. Plan around the slow case if the machine has just
been busy with something else.

### The queue under a burst

Eleven clients fired within about four seconds of each other, each with a short prompt.

- Nine were served, strictly one at a time and in arrival order.
- The last two received `503 queue_full`, which matches the configured limit of one running plus eight waiting.
- `/status` answered in about 20 ms throughout, including while a job was running.

So the gateway does what it is for: concurrency on the GPU stayed at one, and overload failed fast instead of
piling up.

### Long task A: rolling summary

Twelve sequential steps. Each step sent the current summary plus a new log of about 2,800 tokens, and asked for an
updated summary in two fixed sections: a verbatim `FACTS:` list and a short `EVENTS:` paragraph. One distinctive
access code was planted in each log.

| | |
|---|---|
| Prompt size per step | 3.7k to 4.0k tokens, flat |
| Time per step | 24 to 32 s (prefill 15 to 17 s, decode 8 to 15 s) |
| Codes recalled at the end | **12 of 12**, exact |

Because the summary replaces the history instead of growing it, each step costs about the same as the first. Twelve
logs, roughly 34k tokens of input in total, took about six minutes.

### Long task B: keep everything

The same ten logs, but every step re-sent all earlier logs and the model's earlier replies.

| Step | Prompt tokens | Step time |
|---:|---:|---:|
| 1 | 3.7k | 20 s |
| 4 | 14.8k | 63 s |
| 6 | 22.2k | 86 s |
| 8 | 29.6k | 82 s |
| 9 | 33.3k | 250 s |
| 10 | 37.0k | 440 s |

The final question, asked with roughly 40k tokens of context, returned all **10 of 10** codes. Two observations:

1. The server accepted prompts well beyond the 32,768-token window it was configured with. I did not find out why
   (no enforcement, a sliding window, or cache reuse are all plausible), so I would not rely on it.
2. The cost, not the correctness, is the limit: crossing roughly 30k tokens turned an 80-second step into a
   four- to seven-minute one.

## What to take away

- **Slice the input.** Steps of 3 to 4k tokens were fast, predictable and cheap on memory.
- **Carry state in a structured summary.** A short summary with a fixed "never drop these lines" section kept every
  planted fact. My first attempt used unstructured prose and scored 1 of 12, mostly because I also capped the
  output too low; the structure and the budget mattered more than the model.
- **Treat about 30k tokens as the practical ceiling** for a single call on this hardware, whatever the configured window.
- **Budget for variance.** The same model produced 1.3 and 24 tokens per second in the same session, depending on
  memory pressure.
- **Let the queue do its job.** Bursts of ten or more were handled without errors beyond the expected `503`s.

## Gotchas and limits

- One session on an idle machine. There was no long soak test and no competing workload.
- Recall of planted codes is not the same as judgment. I did **not** test whether the model finds real defects,
  summarizes faithfully, or avoids false positives on real documents.
- Cancel and disconnect behavior (the gateway's `drain` policy), priority aging and the reserved queue slots were
  not exercised against the real model.
- The model server's own port should be bound to loopback only. If it is reachable from the LAN, callers can bypass
  the queue entirely; check your firewall.
- Token counts are the server's own; word-count estimates run about 10 to 25 percent off.

## Reference

| Setting | Value used |
|---|---|
| Gateway queue | 1 running + 8 waiting; `503 queue_full` beyond that |
| Per-call timeout (client) | 900 to 1,500 s |
| Output caps | 8 tokens (ramp), 120 (growth), 900 (summary) |
| Related | [Serial LLM Gateway](../serial-llm-gateway/) |
