---
title: "Serial LLM Gateway"
date: 2026-10-22
description: "An OpenAI-compatible front door for one local model: many callers, exactly one inference on the GPU at a time."
tags: ["llm", "mlx", "docker", "self-hosted"]
toc: true
---

## What it is

My Mac mini (M6, ~32 GB unified memory) runs a 27B Qwen model with `mlx-dsppark`. One
generation already pushes it into swap, so two at once means memory-guard shedding or worse.
Several scripts and apps want to use the model, each speaking the OpenAI API.

**Serial LLM Gateway** sits in front of the model server and guarantees that **only one inference
request reaches the model at any moment**. Everyone else waits in a small in-memory queue. To callers it
*is* the OpenAI API: they change `:8000` to `:8001` in their base URL and nothing else.

## At a glance

| | |
|---|---|
| **Runs on** | Docker on the Mac mini; the model stays native on macOS |
| **Port** | `8001` (gateway) → `8000` (mlx-dsppark, loopback only) |
| **API** | `POST /v1/chat/completions` (streaming and non-streaming), `GET /v1/models` |
| **Stack** | Python 3.12, FastAPI, Uvicorn (1 worker), httpx; no database or broker |
| **Code** | [`serial-llm-gateway/`](https://github.com/syuan33/YuanLab/tree/master/serial-llm-gateway) in this repo |
| **Status** | Working and tested against a fake upstream; real-model checks listed under [Gotchas](#gotchas-and-limits) |

## How it works

```text
OpenAI clients ─┐
scripts        ─┼─►  gateway :8001  ──►  one slot  ──►  mlx-dsppark :8000  ──►  GPU
apps           ─┘    bounded queue        (never 2)       (127.0.0.1 only)
```

- **One slot.** A request runs only when the previous one's upstream response has been fully read
  and closed. Queued requests don't hold any upstream connection.
- **Bounded queue.** At most `MAX_QUEUE_SIZE` (default 8) requests wait. More than that gets
  `503 queue_full` with `Retry-After`.
- **Priority from two independent flags.** `urgent` and `important` weigh the same:

  | Level | `X-Priority` |
  |---|---|
  | 2 (first) | `urgent,important` |
  | 1 | `urgent` *or* `important`, first come first served between them |
  | 0 (default) | nothing, or `routine` |

  Within a level the order is first come, first served. Only **trusted keys** can set a priority;
  anyone else's header is ignored and their job runs as routine. A job that waits 5 minutes moves up
  one level, so routine work is never starved, and 2 queue places are kept free for escalated jobs.
- **No preemption.** An urgent job never interrupts the running one. It goes to the front of the queue.
- **Transparent.** The request body is forwarded byte-for-byte and SSE streams are passed through
  unchanged. The gateway adds no heartbeats or queue events.

## Deploy

### 1. Start the model, bound to loopback

```bash
mlx-dsppark serve \
  --model ~/qwen3.27b \
  --drafter incoai/Qwen3.8-27B-DFlash2 \
  --mode dflash \
  --context-window 32768 \
  --memory-guard \
  --host 127.0.0.1 \
  --port 8000
```

`127.0.0.1` instead of `0.0.0.0` matters: anything that can reach `:8000` directly bypasses the queue.

### 2. Configure the gateway

```bash
git clone https://github.com/syuan33/YuanLab.git
cd YuanLab/serial-llm-gateway
cp .env.example .env
openssl rand -hex 32     # run once per key below
```

The settings in `.env` that matter most:

```env
GATEWAY_API_KEY=<every caller sends this>
TRUSTED_API_KEYS=<callers allowed to set priority; comma separated>
ADMIN_API_KEY=<me; enables /admin/*>
GATEWAY_BIND=<Mac's LAN IP, e.g. 192.168.1.50>
```

### 3. Start and verify

```bash
docker compose up -d --build
docker compose ps                       # wait for "(healthy)"
curl http://localhost:8001/v1/models    # should list the model
curl http://localhost:8001/status       # "backend": "ready"
```

If `/v1/models` returns `502`, the container can't reach the model on the Mac's loopback. Restart
`mlx-dsppark` with `--host <LAN IP>` and block port 8000 in the macOS firewall.

> Updating: `git pull && docker compose up -d --build`. Stopping: `docker compose down`.
> Queue state lives in memory, so a restart drops waiting requests.

## Use

### From any OpenAI client

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://M6_IP:8001/v1",
    api_key="<GATEWAY_API_KEY>",
    timeout=1800,   # time in the queue counts; the SDK's 600 s default can be too short
)

reply = client.chat.completions.create(
    model="qwen3.27b",
    messages=[{"role": "user", "content": "Summarise this…"}],
)
print(reply.choices[0].message.content)

for chunk in client.chat.completions.create(model="qwen3.27b", stream=True,
                                            messages=[{"role": "user", "content": "Hi"}]):
    print(chunk.choices[0].delta.content or "", end="")
```

### With curl

```bash
curl http://M6_IP:8001/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.27b","messages":[{"role":"user","content":"Say hi"}]}'

curl -N http://M6_IP:8001/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.27b","stream":true,"messages":[{"role":"user","content":"Count to 5"}]}'
```

### Priority and job ids

Send a trusted key plus headers. `X-Job-Id` is optional. Make it hard to guess, because knowing it is
what lets you cancel the job.

```python
trusted = OpenAI(base_url="http://M6_IP:8001/v1", api_key="<TRUSTED key>", timeout=1800)
trusted.chat.completions.create(
    model="qwen3.27b", messages=[...],
    extra_headers={"X-Priority": "urgent,important", "X-Job-Id": "report-7f3a9c21e0"},
)
```

To escalate a job that is already waiting:

```bash
curl -X POST http://M6_IP:8001/user/priority/report-7f3a9c21e0 \
  -H "Authorization: Bearer <TRUSTED key>" -d '{"priority":"urgent"}'
```

### Cancelling

```bash
curl -X POST http://M6_IP:8001/user/cancel/report-7f3a9c21e0 \
  -H "Authorization: Bearer $GATEWAY_API_KEY"
```

A queued job is removed and never reaches the model. A running job is reported to its caller as
**not finished**: `410 job_cancelled`, or for a stream that has already started, a final
`data: {"error": …}` event with no `[DONE]`.

## Operate

```bash
curl http://M6_IP:8001/status                      # running, waiting per priority, oldest wait, totals
curl -H "Authorization: Bearer $KEY" \
     http://M6_IP:8001/jobs/report-7f3a9c21e0      # one job: state, reason, position, timings
docker compose logs -f                             # job ids and timings only, no prompt content
```

Admin controls (all need `ADMIN_API_KEY`):

| Call | Effect |
|---|---|
| `POST /admin/skip-current` | stop the running job, then continue with the next |
| `POST /admin/reset-all` | stop the running job and remove all queued ones; all are marked not finished |
| `POST /admin/recover` | backend is `uncertain`: I've checked the model is idle, so resume |

What callers may see:

| Response | Meaning | Action |
|---|---|---|
| `503 queue_full` | queue is full | the SDK retries; or raise `MAX_QUEUE_SIZE` |
| `503 queue_timeout` | waited 10 min without starting | reduce load, or raise `QUEUE_TIMEOUT_SECONDS` |
| `503 backend_unavailable` | gateway can't confirm the model stopped | check the model, then `/admin/recover` |
| `410 job_cancelled` | stopped by user or admin | expected; not retried automatically |
| `502` / `504` | model server unreachable or timed out | check `mlx-dsppark` |

## Gotchas and limits

- **Stopping a job doesn't free the GPU right away.** The gateway can only close an HTTP connection,
  and it can't know whether that stops the model. By default (`STOP_POLICY=drain`) the caller is
  released immediately, but the gateway reads the abandoned generation to its end before starting the
  next job, so two generations never overlap. `STOP_POLICY=abort` closes the connection and then waits
  a cooldown plus a health check. Only switch to it after confirming the model really stops on
  disconnect.
- **Long-held connections.** No bytes flow while a request is queued or a non-streaming request is
  generating. Client and proxy timeouts must cover queue wait plus generation.
- **Memory is still the model's problem.** The gateway prevents concurrent load but can't shrink one
  request's RAM or swap. `MAX_REQUEST_BYTES` (1 MiB) is a byte limit, not a token limit.
- **In memory only.** Queue, counters and the last 200 job records reset on restart.
- **Only chat completions are queued.** Other OpenAI endpoints aren't exposed.
- **Keep it on the LAN.** Never port-forward `8001`. Use a VPN (Tailscale or WireGuard) for remote use.

Still to verify against the real `mlx-dsppark`:

- [ ] Does it stop generating when the client disconnects? Test with a long `curl -N`, press Ctrl-C,
  and watch the log or GPU in Activity Monitor.
- [ ] Does `GET /v1/models` respond while a generation is running? Recovery checks use it.
- [ ] Does `host.docker.internal` reach a service bound to the Mac's `127.0.0.1`?
- [ ] Which OpenAI fields (`tools`, `n`, `response_format`, …) it honours.

## Reference

| Variable | Default | Purpose |
|---|---|---|
| `UPSTREAM_URL` | `http://host.docker.internal:8000` | model server as seen from the container |
| `MAX_QUEUE_SIZE` | `8` | waiting requests (the running one not counted) |
| `QUEUE_TIMEOUT_SECONDS` | `600` | max wait before starting |
| `UPSTREAM_TIMEOUT_SECONDS` | `1200` | max time for one inference |
| `MAX_REQUEST_BYTES` | `1048576` | larger bodies get `413` |
| `STOP_POLICY` | `drain` | `drain` (safe) or `abort` (close connection, then recover) |
| `RECOVERY_COOLDOWN_SECONDS` | `10` | wait after an unconfirmed stop |
| `AUTO_RECOVER` | `true` | `false` = always wait for `/admin/recover` |
| `PRIORITY_AGING_SECONDS` | `300` | wait time per automatic one-level promotion (`0` = off) |
| `RESERVED_PRIORITY_SLOTS` | `2` | queue places routine jobs can't take |
| `GATEWAY_API_KEY` / `TRUSTED_API_KEYS` / `ADMIN_API_KEY` | empty | caller, escalation and admin auth |
| `GATEWAY_BIND` | `0.0.0.0` | host interface to publish `8001` on |

Full details, including troubleshooting and the test suite, are in the
[project README](https://github.com/syuan33/YuanLab/blob/master/serial-llm-gateway/README.md).
