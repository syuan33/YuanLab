"""The experiments: ramp, decode speed, chains, cache probe, review, burst. Each appends to results.jsonl."""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import chain as chainlib
from . import client, reviewset
from .telemetry import AbortGuard, Sampler

RAMP_TARGETS = [1000, 2000, 4000, 8000, 12000, 16000, 20000, 24000, 28000, 32000, 36000, 40000]
WORDS = ("the of and to in a is that for it as was with be by on not he this are or his from at which but have an had "
         "they you were her all she there would their we him been has when who will more no if out so said what up its "
         "about into than them can only other new some could time these two may then do first any my now such like our "
         "over man me even most made after also did many before must through back years where much your way well down "
         "should because each just those people how too little state good very make world still own see men work long "
         "get here between both life being under never day same another know while last might us great old year off "
         "come since against go came right used take three").split()


class Aborted(Exception):
    pass


@dataclass
class BenchConfig:
    base_url: str
    model: str
    arm: str
    out_dir: Path
    api_key: str | None = None
    extra_body: dict = field(default_factory=dict)
    pid: int | None = None
    stream_options: bool = True
    timeout: float = 1500.0
    interval: float = 2.0


class Bench:
    """Runs calls, records them, and applies the abort guard."""

    def __init__(self, cfg: BenchConfig, sampler: Sampler | None = None, guard: AbortGuard | None = None):
        self.cfg, self.sampler, self.guard = cfg, sampler, guard or AbortGuard()
        self.chars_per_token = 4.0
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        self.results_path = cfg.out_dir / "results.jsonl"

    def write(self, record: dict) -> None:
        record = {"arm": self.cfg.arm, **record}
        with self.results_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def call(self, kind: str, label: str, messages: list[dict], *, max_tokens: int, target_tokens: int | None = None,
             with_text: bool = False, expected: float | None = None, **extra) -> client.Result:
        res = client.chat(self.cfg.base_url, messages, model=self.cfg.model, max_tokens=max_tokens,
                          extra_body=self.cfg.extra_body, api_key=self.cfg.api_key, timeout=self.cfg.timeout,
                          stream_options=self.cfg.stream_options)
        last = self.sampler.last if self.sampler else {}
        self.write({"kind": kind, "label": label, "target_tokens": target_tokens,
                    "swap_used_mb": last.get("swap_used_mb"), "pressure_level": last.get("pressure_level"),
                    **res.record(with_text=with_text), **extra})
        reason = self.guard.check_memory(self.sampler) or (self.guard.check_call(res.total, expected) if res.ok else None)
        if reason:
            self.write({"kind": "aborted", "label": label, "reason": reason})
            raise Aborted(f"{label}: {reason}")
        return res

    # ---- prompt building ---------------------------------------------------------------

    def filler(self, tokens: int, seed: int | None = None) -> str:
        rng = random.Random(seed)
        out, size, want = [], 0, int(tokens * self.chars_per_token)
        while size < want:
            w = rng.choice(WORDS)
            out.append(w)
            size += len(w) + 1
            if rng.random() < 0.08:
                out.append("\n")
        return " ".join(out)[:want]  # exact length, so the prompt lands on its target size

    def calibrate(self) -> float:
        """Learn characters per token from the server's own usage report (two sizes, so overhead cancels)."""
        counts = []
        for chars in (2_000, 8_000):
            text = self.filler(chars // 4, seed=1)[:chars]
            res = self.call("calibrate", f"{chars} chars", [{"role": "user", "content": f"[run {uuid.uuid4().hex[:8]}] {text}\nOK?"}],
                            max_tokens=2)
            counts.append((len(text), res.prompt_tokens) if res.prompt_tokens and not res.tokens_estimated else None)
        if all(counts) and counts[1][1] > counts[0][1]:
            self.chars_per_token = (counts[1][0] - counts[0][0]) / (counts[1][1] - counts[0][1])
        return self.chars_per_token

    def prompt(self, tokens: int, tail: str) -> list[dict]:
        head = f"[run {uuid.uuid4().hex[:12]}]\n"  # unique start: no prefix cache can match
        return [{"role": "user", "content": head + self.filler(tokens, seed=tokens) + "\n\n" + tail}]


def run_ramp(bench: Bench, targets=RAMP_TARGETS, reps: int = 3) -> None:
    bench.call("warmup", "warm-up", bench.prompt(500, "Reply with the single word OK."), max_tokens=8)
    prev: tuple[int, float] | None = None
    try:
        for rep in range(reps):
            prev = None
            for target in targets:
                expected = prev[1] * target / prev[0] if prev else None
                res = bench.call("ramp", f"rep{rep + 1} {target}", bench.prompt(target, "Reply with the single word OK."),
                                 max_tokens=8, target_tokens=target, expected=expected)
                slow = expected is not None and res.total > bench.guard.slow_factor * expected
                if res.ok and res.prompt_tokens and not slow:  # the baseline is the last healthy call
                    prev = (res.prompt_tokens, res.total)
    except Aborted as exc:
        print(f"ABORTED: {exc}")


def run_decode(bench: Bench, prompts=(1000, 8000), reps: int = 3, max_tokens: int = 256) -> None:
    tail = "Now write a numbered list of 100 different short facts about the ocean, one per line."
    try:
        for target in prompts:
            for rep in range(reps):
                bench.call("decode", f"rep{rep + 1} {target}", bench.prompt(target, tail), max_tokens=max_tokens,
                           target_tokens=target)
    except Aborted as exc:
        print(f"ABORTED: {exc}")


def run_chains(bench: Bench, data: dict, which=("rolling", "keep"), summary_max_tokens: int = 1200) -> None:
    chunks, questions = data["chunks"], data["questions"]
    n = len(chunks)
    try:
        if "rolling" in which:
            summary = ""
            for i, chunk in enumerate(chunks, 1):
                res = bench.call("chain_rolling_step", f"step {i}/{n}", chainlib.rolling_prompt(summary, chunk, i, n),
                                 max_tokens=summary_max_tokens, with_text=False)
                if not res.ok:
                    break
                summary = res.text.strip()
            else:
                msgs = [{"role": "system", "content": "Answer using ONLY the knowledge base below."},
                        {"role": "user", "content": f"KNOWLEDGE BASE:\n{summary}\n\n{chainlib.question_block(questions)}"}]
                res = bench.call("chain_answers", "rolling", msgs, max_tokens=400, with_text=True)
                bench.write({"kind": "chain_answers", "label": "rolling (scored)", "score": chainlib.score_answers(questions, res.text),
                             "summary_chars": len(summary)})
        if "keep" in which:
            history: list[dict] = []
            for i, chunk in enumerate(chunks, 1):
                history.append({"role": "user", "content": chainlib.keep_prompt_first(chunk, i, n)})
                res = bench.call("chain_keep_step", f"step {i}/{n}", list(history), max_tokens=20)
                if not res.ok:
                    break
                history.append({"role": "assistant", "content": res.text.strip() or "Noted."})
            else:
                history.append({"role": "user", "content": chainlib.question_block(questions)})
                res = bench.call("chain_answers", "keep-everything", history, max_tokens=400, with_text=True)
                bench.write({"kind": "chain_answers", "label": "keep-everything (scored)",
                             "score": chainlib.score_answers(questions, res.text)})
    except Aborted as exc:
        print(f"ABORTED: {exc}")


def run_cache_probe(bench: Bench, prefix_tokens: int = 10_000) -> None:
    prefix = f"[cache probe {uuid.uuid4().hex[:8]}]\n" + bench.filler(prefix_tokens, seed=7)
    control = f"[cache probe {uuid.uuid4().hex[:8]}]\n" + bench.filler(prefix_tokens, seed=8)
    try:
        for label, text, tail in (("first send", prefix, "Question one: reply with the single word OK."),
                                  ("same prefix again", prefix, "Question two: reply with the single word YES."),
                                  ("control, new prefix", control, "Question three: reply with the single word NO.")):
            bench.call("cache", label, [{"role": "user", "content": f"{text}\n\n{tail}"}], max_tokens=8,
                       target_tokens=prefix_tokens)
    except Aborted as exc:
        print(f"ABORTED: {exc}")


def run_review(bench: Bench, tasks: list[dict], max_tokens: int = 1500) -> None:
    try:
        for task in tasks:
            res = bench.call("review", task["id"], reviewset.build_review_messages(task), max_tokens=max_tokens,
                             with_text=True, size=task["size"], clean=task["clean"])
            if res.ok:
                bench.write({"kind": "review", "label": f"{task['id']} (scored)", "size": task["size"], "clean": task["clean"],
                             "prompt_tokens": res.prompt_tokens, "score": reviewset.score_review(task, res.text)})
    except Aborted as exc:
        print(f"ABORTED: {exc}")


def run_burst(base_url: str, n: int = 11, api_key: str | None = None, model: str = "any") -> dict:
    """Fire `n` short requests at once (use the gateway URL). Counts 200s, 503s and anything else."""
    codes: list[int] = []
    lock = threading.Lock()
    body = {"model": model, "max_tokens": 8, "temperature": 0, "messages": [{"role": "user", "content": "Reply with the single word OK."}]}
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}

    def one():
        try:
            r = httpx.post(f"{base_url.rstrip('/')}/v1/chat/completions", json=body, headers=headers, timeout=1500)
            code = r.status_code
        except httpx.HTTPError:
            code = 0
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=one) for _ in range(n)]
    for t in threads:
        t.start()
        time.sleep(0.02)
    for t in threads:
        t.join()
    return {"n": n, "served": codes.count(200), "rejected": codes.count(503),
            "other": sum(1 for c in codes if c not in (200, 503))}
