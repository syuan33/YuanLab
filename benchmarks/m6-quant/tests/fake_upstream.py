"""A fake OpenAI-compatible model server with a prefill rate and an optional memory cliff."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def canned(last: str) -> str:
    if "Respond with ONLY a JSON array" in last:
        return "[]"
    if "mapping the question number" in last:
        ids = re.findall(r"^(\d+)\. Which release", last, re.M)
        return json.dumps({i: "UNKNOWN" for i in ids})
    if "Return the updated knowledge base" in last:
        return "FACTS:\n- 1.0.0 | fake fact\nEVENTS:\n- nothing"
    if "reply with only the word Noted" in last:
        return "Noted"
    if "numbered list of 100" in last:
        return " ".join(f"fact{i}" for i in range(256))
    return "OK"


class FakeUpstream:
    def __init__(self, prefill_tps=20_000, decode_tps=2_000, cliff_tokens=None, cliff_factor=4.0):
        self.prefill_tps, self.decode_tps = prefill_tps, decode_tps
        self.cliff_tokens, self.cliff_factor = cliff_tokens, cliff_factor
        self.calls = 0
        self.app = FastAPI()
        self.app.get("/v1/models")(lambda: {"object": "list", "data": [{"id": "fake", "object": "model"}]})
        self.app.post("/v1/chat/completions")(self.chat)

    async def chat(self, request: Request):
        body = await request.json()
        self.calls += 1
        text = "\n".join(str(m["content"]) for m in body["messages"])
        ptoks = max(1, round(len(text) / 3.5))
        delay = ptoks / self.prefill_tps * (self.cliff_factor if self.cliff_tokens and ptoks >= self.cliff_tokens else 1)
        words = canned(str(body["messages"][-1]["content"])).split(" ")[: body.get("max_tokens", 64)]
        include_usage = (body.get("stream_options") or {}).get("include_usage")

        async def gen():
            await asyncio.sleep(delay)
            for w in words:
                await asyncio.sleep(1 / self.decode_tps)
                yield f'data: {json.dumps({"choices": [{"index": 0, "delta": {"content": w + " "}}]})}\n\n'.encode()
            yield f'data: {json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})}\n\n'.encode()
            if include_usage:
                yield f'data: {json.dumps({"choices": [], "usage": {"prompt_tokens": ptoks, "completion_tokens": len(words)}})}\n\n'.encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")


class ServerThread:
    def __init__(self, app):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> "ServerThread":
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            assert time.time() < deadline, "server did not start"
            time.sleep(0.01)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
