# M6 quantization benchmark harness

Measures what a quantization (Q4, Q5, Q6 ...) of a 27B model does on a 32 GB Apple Silicon Mac that serves
long documents: speed by prompt size, where it falls off a memory cliff, how well it keeps facts across a
long task, and whether it still finds bugs. Built for the study planned in
[PR 5](https://github.com/syuan33/YuanLab/pull/5#issuecomment-6373687956). Runtime needs only Python 3.12+
and `httpx`; everything else is the standard library.

**Status:** Day 0 is built and tested against a fake server (31 tests). It has **not** been run against the real
model yet. Things only the M6 can confirm are listed at the bottom.

## What is in here

| Part | What it does |
|---|---|
| `m6bench/client.py` | One streaming chat call. Records time to first token (TTFT), prefill and decode speed, token counts from the server's `usage` (estimated from chunks, and flagged, if missing), and any extra keys the server sends (for example a timing block) |
| `m6bench/telemetry.py` | Samples swap used, page-outs, free-memory percent, memory-pressure level and the server's resident memory every 2 s into `telemetry.csv`. `AbortGuard` stops a run when swap grows by more than 4 GiB, pressure goes critical, or two calls in a row take over 5x what the last healthy call predicts |
| `m6bench/workloads.py` | `ramp`, `decode`, `chains`, `cache`, `review`, `burst` |
| `m6bench/reviewset.py` | Builds and scores the seeded-defect review tasks |
| `m6bench/chain.py` | Builds and scores the changelog chain |
| `m6bench/analysis.py` | Cliff detection, implied context memory per token, `report.md` |
| `data/` | The 24 review tasks and the changelog chain (checked in, so every arm gets identical inputs; see `NOTICE.md`) |

## The tests it runs

- **Ramp:** prompts of 1k to 40k tokens, 3 repetitions, 8 output tokens, so TTFT is essentially prefill. Each
  prompt starts with a random string so a prefix cache cannot help. One warm-up call is excluded.
- **Decode:** 256 output tokens after 1k and 8k prompts.
- **Chains:** a real changelog in 12 sections of about 8.5k characters. *Rolling*: the model keeps a summary
  that is fed back each step. *Keep-everything*: every step re-sends all earlier sections. Afterwards both
  answer the same 10 "which release shipped this change?" questions in one call, scored by script.
- **Cache probe:** one 10k-token prefix sent twice, plus a control with a new prefix. A second TTFT far below
  the first means the server reuses the prefix.
- **Review:** 24 tasks, 8 each at about 4k, 12k and 24k prompt tokens. 6 per size contain 1 to 3 injected
  mutations (flipped comparison, swapped arguments, deleted guard, dropped `close()` ...); 2 per size are clean.
  The model must answer with a JSON list of findings. A mutation is found when a finding cites its file and a
  line within 3 lines. Anything else is an *unmatched finding* (not necessarily a false alarm).
- **Burst:** 11 simultaneous requests at the gateway. With the gateway defaults expect 7 served and 4 rejected
  (`RESERVED_PRIORITY_SLOTS=2` keeps two waiting places free for escalated jobs); 9 and 2 only with none reserved.

## Run book for one arm

```bash
cd benchmarks/m6-quant
python3 -m venv .venv && . .venv/bin/activate && pip install httpx
ARM=A1; URL=http://127.0.0.1:8000; MODEL=<name from /v1/models>; PID=$(pgrep -f mlx-dsppark | head -1)

# 1. Before the first run: do the parsers work on this Mac? (every field should have a value)
python -m m6bench selftest --arm $ARM --base-url $URL --pid $PID

# 2. Record the environment (do this AFTER setting any wired-limit change, so it is captured)
python -m m6bench env --arm $ARM --engine mlx-dspark --quant q4 --drafter incoai/Qwen3.8-27B-DFlash2 \
  --block-size 5 --reasoning "thinking off" --model-dir ~/models/qwen-q4 --drafter-dir ~/models/drafter \
  --model-config ~/models/qwen-q4/config.json

# 3. Experiments. Each appends to results/$ARM/results.jsonl and writes results/$ARM/telemetry.csv
COMMON="--arm $ARM --base-url $URL --model $MODEL --pid $PID"
python -m m6bench ramp   $COMMON
python -m m6bench decode $COMMON
python -m m6bench chains $COMMON
python -m m6bench cache  $COMMON
python -m m6bench review $COMMON            # add --sizes S,M or --limit 6 for a quick pass

# 4. Report
python -m m6bench report --arm $ARM         # results/$ARM/report.md
```

- **Hygiene:** plugged in, `caffeinate -dimsu` running, unrelated apps and Docker Desktop quit (or its VM
  memory noted), one model loaded at a time, a different `--arm` label for every variant (for example
  `A3-default-cap` and `A3-raised-cap`).
- **Raising the GPU memory cap** (Q5 variant): `sudo sysctl iogpu.wired_limit_mb=26624`. It resets on reboot.
  Leave about 6 GiB for macOS.
- **Thinking mode:** pass whatever the server accepts through `--extra-body`, for example
  `--extra-body '{"reasoning_effort":"low"}'`, and write it into `--reasoning`. Reasoning-heavy runs need a
  larger `--max-tokens` for `review`.
- **Through the gateway:** point `--base-url` at `:8001` (add `--api-key` if set). For raw speed measure the
  model server directly so the queue is not in the numbers. For the burst test use the gateway:
  `python -m m6bench burst --arm G --base-url http://127.0.0.1:8001 --expect-served 7 --expect-rejected 4`.
- **Spot-checking the review tasks** (some mutations may be harmless in context): `python -m m6bench show --id M03`.

## Reading the results

- **Cliff:** the first prompt size whose TTFT is over 2x the straight-line fit of the smaller sizes (and the
  next size too). If it is a memory cliff, the report solves for the context memory per token that explains it
  and prints it beside the value from `config.json`. Agreement supports the memory explanation.
- **Not comparable across arms** unless `env.json` shows the same machine state, wired limit and gateway commit.
- **Sample size:** 24 review tasks and 10 chain questions are small. Treat differences under about 10 points as noise.

## Limits of the data

- The code comes from public repositories the model has probably seen, which can make bug-spotting easier
  than on unseen code. The same inputs go to every arm, so comparisons stay fair; absolute scores do not
  transfer to private code. To test on private code run `build-review --stdlib <dir>` on your own
  directory and keep the output out of Git.
- Mutations are mechanical. A few may not change behaviour in context.
- Prompts use a rough 3.5 characters per token; the real counts come from the server and are what the report uses.

## Not yet verified on the M6

1. The macOS parsers for `sysctl vm.swapusage`, `vm_stat`, `memory_pressure` and
   `kern.memorystatus_vm_pressure_level` (written from their usual output; `selftest` shows what they return).
2. Whether the server honours `stream_options.include_usage` (otherwise token counts are estimated; use
   `--no-stream-options` if it rejects the field) and where it reports its own timings.
3. How `mlx-dspark` takes its thinking-mode and block-size settings.
4. Whether `iogpu.wired_limit_mb` and the default cap behave as described in the PR comment.

## Tests

```bash
pip install -r requirements.txt
python -m pytest -q            # 31 tests, about 45 s
```

The end-to-end tests run every command against a fake server that has a prefill rate and a planted cliff,
and check that the cliff is found, the guard aborts, and the reports are written.
