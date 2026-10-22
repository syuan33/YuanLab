# serial-llm-gateway

A small OpenAI-compatible gateway that makes sure **only one inference request at a time**
reaches a local `mlx-dsppark` server. Callers queue in memory: FIFO within each priority level,
higher levels first. Existing OpenAI clients only change their base URL from `:8000/v1` to `:8001/v1`.

```text
OpenAI clients ──► gateway :8001 (Docker) ──► one request at a time ──► mlx-dsppark :8000 (macOS host)
                    bounded priority queue
```

The code is two files: `app/scheduler.py` (queue, single slot, backend state) and `app/main.py`
(HTTP proxying). There is one Uvicorn worker, one process, and no external services.

## Quick start

### 1. Run mlx-dsppark natively on the Mac, bound to loopback

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

Use `--host 127.0.0.1` rather than `0.0.0.0`. Callers that can still reach `:8000` directly
bypass the queue. Docker Desktop's `host.docker.internal` normally reaches services bound to the
Mac's loopback. If it doesn't on your setup, see Troubleshooting.

### 2. Start the gateway

```bash
cd serial-llm-gateway
cp .env.example .env          # edit keys and limits
docker compose up -d --build
docker compose ps             # should show (healthy)
```

Without Docker: `pip install -r requirements.txt && UPSTREAM_URL=http://127.0.0.1:8000 uvicorn app.main:app --port 8001 --workers 1`.

Always run exactly **one** worker. The queue lives in process memory, so a second worker or replica
would have its own queue and break the one-at-a-time guarantee.

### 3. Try it

```bash
# Normal completion
curl http://M6_IP:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "qwen3.27b", "messages": [{"role": "user", "content": "Say hi"}]}'

# Streaming (-N disables curl buffering)
curl -N http://M6_IP:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "qwen3.27b", "stream": true, "messages": [{"role": "user", "content": "Count to 5"}]}'

# Models (passed straight through, not queued)
curl http://M6_IP:8001/v1/models
```

If `GATEWAY_API_KEY` is set, add `-H "Authorization: Bearer $GATEWAY_API_KEY"`.

### Python OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://M6_IP:8001/v1",
    api_key="your-gateway-key-or-anything",   # sent as Bearer; checked only if GATEWAY_API_KEY is set
    timeout=1800,                             # queue wait + generation; the SDK default is 600s
)

resp = client.chat.completions.create(model="qwen3.27b", messages=[{"role": "user", "content": "Hello"}])
print(resp.choices[0].message.content)

for chunk in client.chat.completions.create(
    model="qwen3.27b", stream=True, messages=[{"role": "user", "content": "Hello"}],
):
    print(chunk.choices[0].delta.content or "", end="")

# Optional: name your job (so you can cancel it) and ask for a priority (trusted keys only)
client.chat.completions.create(
    model="qwen3.27b", messages=[{"role": "user", "content": "Hello"}],
    extra_headers={"X-Job-Id": "my-report-2026-10-22-001", "X-Priority": "urgent"},
)
```

## Queue behaviour

- **One running request, ever.** The slot is handed to the next job only after the previous
  job's upstream response has been fully read and closed.
- **Bounded queue.** At most `MAX_QUEUE_SIZE` requests wait. The running request isn't counted.
  Excess requests get `503` with `Retry-After` and error code `queue_full`.
- **Waiting is free for the model.** A queued request holds only its own client connection;
  nothing is sent upstream until it starts. Streaming and non-streaming requests share the same queue.
- **Priority.** There are two independent flags, `urgent` and `important`, of equal weight:

  | Level | `X-Priority` value |
  |---|---|
  | 2 (first) | `urgent,important` |
  | 1 | `urgent` or `important` (equal; first come, first served between them) |
  | 0 (default) | none, or `routine` |

  Within a level the order is strictly first come, first served. Only `TRUSTED_API_KEYS` and
  `ADMIN_API_KEY` may set a priority. Anyone else's `X-Priority` is ignored (noted in the
  job record) and the job runs as routine.
- **Escalating later.** A trusted key can raise or lower a *queued* job:
  `POST /user/priority/{job_id}` with `{"priority": "urgent"}`. The job keeps its original arrival time.
- **No starvation.** A job that has waited `PRIORITY_AGING_SECONDS` (default 300) moves up one level,
  and so on. Set it to `0` for strict levels.
- **Room for urgent work.** Routine jobs may use only `MAX_QUEUE_SIZE - RESERVED_PRIORITY_SLOTS`
  places, so an escalated request can still get in when routine traffic has filled the queue.
- **No preemption.** A new urgent job never interrupts the running job. It goes to the
  front of the queue.

## Stopping jobs

Each request has a job id: the client's `X-Job-Id` header (8-128 characters of `[A-Za-z0-9._-]`;
use a random UUID, since the id is also what authorises a cancel), or a generated one. It is
returned in the `X-Job-Id` response header and in error bodies.

| Endpoint | Who | Queued job | Running job |
|---|---|---|---|
| `POST /user/cancel/{job_id}` | Whoever knows the id | removed, never sent upstream | stopped (see below) |
| `POST /admin/skip-current` | `ADMIN_API_KEY` | – | stopped; the next job follows |
| `POST /admin/reset-all` | `ADMIN_API_KEY` | all removed | stopped; everything ends as not finished |
| `POST /admin/recover` | `ADMIN_API_KEY` | – | clears a `recovering`/`uncertain` backend |

A job that is stopped **is reported as not finished**, never as a fake success:

- **Non-streaming, or streaming before any output:** HTTP `410` with
  `{"error": {"type": "job_cancelled", "code": "<state>", "job_id": ...}}` and `x-should-retry: false`.
  410 is used because the OpenAI SDKs automatically retry 409, 429 and 5xx.
- **Streaming that has already started:** one final event,
  `data: {"error": {"type": "job_cancelled", ...}}`, then the stream closes **without** `[DONE]`.
  The OpenAI Python SDK raises an `APIError` when it sees an `error` event.
- **Job record:** `GET /jobs/{job_id}` shows `state` (`completed`, `failed`, `timed_out`,
  `cancelled_by_user`, `skipped_by_admin`, `reset_by_admin`, `client_disconnected`, `shutdown`,
  or `queued`/`running`/`stopping`), a reason, queue position, timings and notes. The last
  200 jobs are kept in memory only.

### What "stop" means for the GPU: `STOP_POLICY`

The gateway can't stop the model directly. All it can do is close its HTTP connection to
`mlx-dsppark`, and **it can't confirm that this stops generation.**

- `drain` (default, safe): the caller is released immediately (it gets the 410 or error event),
  but the gateway keeps reading the upstream response to its end and throws it away. The next job
  starts only after upstream has actually finished, so overlap is impossible. The cost is that
  the GPU finishes the abandoned generation. It is still bounded by `UPSTREAM_TIMEOUT_SECONDS`, and
  `/status` shows the job as `stopping` meanwhile.
- `abort`: close the upstream connection immediately. The backend then goes to `recovering`:
  no job starts for `RECOVERY_COOLDOWN_SECONDS`, and then `GET /v1/models` on upstream must respond.
  With `AUTO_RECOVER=false` (or after `RECOVERY_ATTEMPTS` failed probes) it becomes `uncertain`,
  new requests get `503 backend_unavailable`, and an operator must `POST /admin/recover`.
  Admins can also do this one-off: `POST /admin/skip-current?force=true` or `/admin/reset-all?force=true`.

Use `abort` only after checking that mlx-dsppark really stops generating when the client disconnects (see
*Unverified assumptions*). Otherwise the cooldown is only a guess, and the tests show that a
too-short cooldown lets two generations overlap.

### Client disconnects

- **While queued:** detected within `DISCONNECT_POLL_SECONDS` (default 0.5 s). The job is removed from
  the queue and never reaches the model.
- **While running:** handled exactly like a stop under `STOP_POLICY` (drain by default).

## Timeouts

| Setting | Default | Effect |
|---|---|---|
| `QUEUE_TIMEOUT_SECONDS` | 600 | Wait longer than this without starting → `503 queue_timeout` with `Retry-After`; never sent upstream |
| `UPSTREAM_CONNECT_TIMEOUT_SECONDS` | 10 | Can't connect → `504`/`502`; the backend stays `ready` (nothing was sent) |
| `UPSTREAM_TIMEOUT_SECONDS` | 1200 | Total time for one inference, and the read timeout. When it is exceeded the connection is closed, the backend goes to `recovering` (termination is not confirmed), and the caller gets `504` (or a cut-off stream) |

Other upstream failures: an upstream HTTP error such as a 4xx/5xx JSON body is passed through
unchanged (status and body). If upstream is unreachable the caller gets `502 upstream_unavailable`.
If upstream fails mid-stream, the client stream is cut off with no `[DONE]`, the failure is logged,
and the backend goes to `recovering`.

**Shutdown** (`docker compose stop`, SIGTERM): Uvicorn stops accepting connections and gives open
requests 20 s (`--timeout-graceful-shutdown`). Queued requests are then dropped. The running job
gets `SHUTDOWN_GRACE_SECONDS` more and is then closed. No new upstream request starts once shutdown
begins. `stop_grace_period: 40s` in Compose covers both.

## Status and health

```bash
curl http://M6_IP:8001/healthz       # {"status":"ok"} – liveness only, used by the Docker health check
curl http://M6_IP:8001/status
```

```json
{
  "backend": "ready",
  "backend_reason": null,
  "running": 1,
  "waiting": 3,
  "waiting_by_priority": {"urgent+important": 0, "urgent": 1, "important": 0, "routine": 2},
  "max_queue_size": 8,
  "oldest_wait_seconds": 24,
  "current": {"priority": "routine", "stream": true, "running_seconds": 41},
  "total_completed": 120,
  "total_failed": 2,
  "total_timed_out": 1,
  "total_cancelled": 3,
  "total_rejected": 4
}
```

`backend` is `ready`, `recovering`, `uncertain` or `shutting_down`. Counters are in memory and
reset on restart. Neither `/status` nor the logs contain prompt or completion content. Logs record
job ids, priorities, states and timings only.

## Security

- **Keep mlx-dsppark private.** Bind it to `127.0.0.1`, as above. If it must listen on `0.0.0.0`,
  block port 8000 from the network: enable the macOS firewall (System Settings → Network → Firewall,
  "Block all incoming connections" except allowed apps) or add a `pf` rule.
- **Don't expose the gateway to the internet.** Publish `8001` only on your LAN. Set `GATEWAY_BIND`
  in `.env` to the Mac's LAN IP, or to `127.0.0.1` if only local tools call it. Docker Desktop port
  forwards are not reliably filtered by the macOS application firewall, so don't rely on it for 8001.
  Don't port-forward 8001 on your router. For remote access use a VPN such as Tailscale or WireGuard.
- **Set keys** (`openssl rand -hex 32`): `GATEWAY_API_KEY` for every caller, `TRUSTED_API_KEYS` for
  callers allowed to escalate, `ADMIN_API_KEY` to enable `/admin/*`. They are compared in constant
  time. The caller's key is **not** forwarded upstream. Use `UPSTREAM_API_KEY` if mlx-dsppark needs one.
  `/healthz` and `/status` are open (no sensitive data).
- **Container hardening:** non-root user, read-only filesystem, all capabilities dropped,
  `no-new-privileges`, 256 MB memory limit, 1 CPU.

## Troubleshooting

| Symptom | Check |
|---|---|
| Every request returns `502 upstream_unavailable` | Is mlx-dsppark running? From the container: `docker compose exec gateway python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:8000/v1/models').read())"`. If loopback isn't reachable from Docker, bind mlx-dsppark to the Mac's LAN IP and firewall port 8000 |
| `503 backend_unavailable` | `/status` shows `uncertain` and a reason. Make sure the model has stopped (Activity Monitor / server log), then `curl -X POST -H "Authorization: Bearer $ADMIN_API_KEY" http://M6_IP:8001/admin/recover` |
| `503 queue_full` | Increase `MAX_QUEUE_SIZE`, or have callers back off. The OpenAI SDK retries 503s on its own |
| `503 queue_timeout` | Increase `QUEUE_TIMEOUT_SECONDS`, or reduce load |
| SDK raises `APITimeoutError` while waiting | Raise the client `timeout`. Queue wait plus generation can exceed the SDK's 600 s default |
| Stream from an idle-timeout proxy drops while queued | No bytes are sent while waiting (see Limitations). Raise the proxy's idle timeout |
| Cancel didn't free the GPU | Expected with `STOP_POLICY=drain`. See *What "stop" means for the GPU* |
| Heavy swap / memory-guard 503s from upstream | Those come from the model server and are passed through. Reduce context or prompt size. The gateway can't help here |

## Limitations

- **Memory:** the gateway prevents concurrent inference but **can't reduce the model's RAM or swap
  use**. One 27B request with a long context can still swap. Memory-guard shedding responses from
  mlx-dsppark are passed through as-is.
- **Size limit is in bytes:** `MAX_REQUEST_BYTES` (default 1 MiB) is a byte limit (`413` beyond it)
  and doesn't correspond to a token count. There is no token counting, truncation, context
  management or memory-pressure telemetry (out of scope for v1).
- **Long-held connections:** a queued request holds an open HTTP connection with no bytes flowing
  until it starts. Clients, proxies and load balancers with idle or read timeouts shorter than the
  queue wait will give up. The gateway deliberately sends no SSE heartbeats or queue events,
  since their compatibility with every client isn't verified. For non-streaming requests nothing is
  sent until generation finishes.
- **No confirmed termination:** see `STOP_POLICY`. Termination is only *confirmed* when upstream
  sends the complete response.
- **Restarts:** all queue state, counters and job history are in memory and lost when the gateway
  restarts. If the gateway dies while upstream is generating, the new gateway process can't know
  that, so wait for the model to go idle before sending new traffic.
- **Endpoints:** only `POST /v1/chat/completions` is queued, and `GET /v1/models` is proxied.
  Other OpenAI endpoints (`/v1/completions`, `/v1/embeddings`, ...) are not exposed. Calling the model
  through any other path bypasses the queue.
- **Compatibility:** the request body is forwarded byte-for-byte, so unknown fields reach mlx-dsppark
  unchanged. Whether it honours `n`, `tools`, `response_format`, `logprobs`, `stream_options` and so
  on is up to mlx-dsppark. Compression is not passed through (the gateway asks upstream for identity
  encoding). Responses use chunked transfer encoding.
- **Not included:** persistent queues, multiple workers, dashboards and multiple upstream backends
  are deliberately absent.

## Unverified assumptions about mlx-dsppark

These could not be tested here (the tests use a fake upstream):

1. It serves `POST /v1/chat/completions` (SSE with `data: ... [DONE]` when streaming) and `GET /v1/models`.
2. **Whether it stops generating when the HTTP client disconnects.** Check before using `STOP_POLICY=abort`:
   start a long streaming request with curl, press Ctrl-C after a few tokens, and watch the server log
   or GPU usage in Activity Monitor.
3. `GET /v1/models` answers while a generation is running. If it doesn't, recovery probes may fail and
   the backend becomes `uncertain`.
4. Its error responses are JSON. They're forwarded as-is either way.
5. Docker Desktop's `host.docker.internal` reaches a service bound to the Mac's `127.0.0.1`.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest -q
```

The tests start a real gateway and an instrumented fake upstream (both Uvicorn on localhost). The
fake counts in-flight inferences, including simulated "GPU keeps working after disconnect", and every
test fails if more than one inference was ever in flight at once. Covered: sequential execution of
simultaneous requests, a shared queue for streaming and non-streaming requests, FIFO, overflow, queue
timeout, disconnect while queued and while running (drain and abort), user cancel, admin skip and
reset-all, upstream errors, unreachable upstream, read timeout, mid-stream failure, byte-exact SSE, unknown-field
forwarding, shutdown, uncertain backend, `/status`, priority levels, untrusted callers, escalation,
aging, reserved places, auth and 413.
