"""One streaming chat call against an OpenAI-compatible server, with timing."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

import httpx

STANDARD_KEYS = {"id", "object", "created", "model", "choices", "usage", "system_fingerprint", "service_tier"}


@dataclass
class Result:
    ok: bool = False
    status: int | None = None
    error: str | None = None
    t_send: float = 0.0  # wall clock, to line up with telemetry
    ttft: float | None = None
    total: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tokens_estimated: bool = False
    prefill_tps: float | None = None
    decode_tps: float | None = None
    finish_reason: str | None = None
    reasoning_chars: int = 0
    text: str = ""
    server_extra: dict = field(default_factory=dict)

    def record(self, with_text: bool = False) -> dict:
        d = asdict(self)
        if not with_text:
            d.pop("text")
        return d


def _delta_text(choice: dict) -> tuple[str, str]:
    delta = choice.get("delta") or {}
    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
    return delta.get("content") or "", reasoning


def chat(base_url: str, messages: list[dict], *, model: str, max_tokens: int, temperature: float = 0.0,
         extra_body: dict | None = None, api_key: str | None = None, timeout: float = 1500.0,
         stream_options: bool = True, headers: dict | None = None) -> Result:
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": True}
    if stream_options:
        body["stream_options"] = {"include_usage": True}
    body.update(extra_body or {})
    hdrs = {"content-type": "application/json", **(headers or {})}
    if api_key:
        hdrs["authorization"] = f"Bearer {api_key}"

    res = Result(t_send=time.time())
    t0 = time.perf_counter()
    t_first = t_last = None
    chunks = 0
    usage: dict | None = None
    parts: list[str] = []
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            with client.stream("POST", f"{base_url.rstrip('/')}/v1/chat/completions", json=body, headers=hdrs) as r:
                res.status = r.status_code
                if r.status_code != 200:
                    res.error = r.read().decode("utf-8", "replace")[:500]
                    res.total = time.perf_counter() - t0
                    return res
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    if obj.get("error"):
                        res.error = json.dumps(obj["error"])[:500]
                        break
                    res.server_extra.update({k: v for k, v in obj.items() if k not in STANDARD_KEYS})
                    if obj.get("usage"):
                        usage = obj["usage"]
                    for choice in obj.get("choices") or []:
                        content, reasoning = _delta_text(choice)
                        if content or reasoning:
                            now = time.perf_counter()
                            t_first = t_first or now
                            t_last = now
                            chunks += 1
                            parts.append(content)
                            res.reasoning_chars += len(reasoning)
                        if choice.get("finish_reason"):
                            res.finish_reason = choice["finish_reason"]
    except httpx.HTTPError as exc:
        res.error = f"{type(exc).__name__}: {exc}"[:500]
    res.total = time.perf_counter() - t0
    res.text = "".join(parts)
    if usage:
        res.prompt_tokens = usage.get("prompt_tokens")
        res.completion_tokens = usage.get("completion_tokens")
    if res.completion_tokens is None:
        res.completion_tokens, res.tokens_estimated = chunks, True
    if t_first is not None:
        res.ttft = t_first - t0
        if res.prompt_tokens and res.ttft > 0:
            res.prefill_tps = res.prompt_tokens / res.ttft
        if t_last and t_last > t_first and res.completion_tokens and res.completion_tokens > 1:
            res.decode_tps = (res.completion_tokens - 1) / (t_last - t_first)
    res.ok = res.error is None and res.status == 200 and t_first is not None
    return res
