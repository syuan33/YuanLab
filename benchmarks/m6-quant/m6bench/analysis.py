"""Cliff detection, implied context memory per token, and the per-arm markdown report."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

GIB = 1024 ** 3


def _fit_line(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least squares y = a*x + b."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    return a, my - a * mx


def find_cliff(points: list[tuple[float, float]], factor: float = 2.0, min_fit: int = 3) -> dict | None:
    """Find where time to first token jumps away from a straight-line fit.

    `points` are (prompt_tokens, ttft_seconds). Walking up in size, each point is compared with the line
    fitted to all smaller points. The cliff is the first point more than `factor` times its prediction that
    is also followed by another such point (or is the last point). Returns the last good and first bad size.
    """
    pts = sorted(points)
    for i in range(min_fit, len(pts)):
        a, b = _fit_line([p[0] for p in pts[:i]], [p[1] for p in pts[:i]])
        if pts[i][1] <= factor * max(a * pts[i][0] + b, 1e-9):
            continue
        nxt = pts[i + 1] if i + 1 < len(pts) else None
        if nxt is not None and nxt[1] <= factor * max(a * nxt[0] + b, 1e-9):
            continue
        return {"last_good_tokens": pts[i - 1][0], "first_bad_tokens": pts[i][0],
                "ttft_ratio": pts[i][1] / max(a * pts[i][0] + b, 1e-9)}
    return None


def implied_kv_kib(cap_gib: float, weights_gib: float, drafter_gib: float, overhead_gib: float,
                   cliff_tokens: float) -> float | None:
    """If the cliff is where memory runs out: KiB of context memory per token that explains it."""
    room = cap_gib - weights_gib - drafter_gib - overhead_gib
    if room <= 0 or cliff_tokens <= 0:
        return None
    return room * 1024 * 1024 / cliff_tokens


def kv_kib_per_token(config: dict, bytes_per_value: int = 2) -> dict:
    """Context memory per token implied by a model's config.json (fp16 cache unless told otherwise)."""
    cfg = config.get("text_config", config)
    layers = cfg.get("num_hidden_layers")
    kv_heads = cfg.get("num_key_value_heads") or cfg.get("num_attention_heads")
    head_dim = cfg.get("head_dim") or (cfg["hidden_size"] // cfg["num_attention_heads"]
                                       if cfg.get("hidden_size") and cfg.get("num_attention_heads") else None)
    out = {"layers": layers, "kv_heads": kv_heads, "head_dim": head_dim}
    types = cfg.get("layer_types")
    full = sum(1 for t in types if "full" in str(t)) if isinstance(types, list) else layers
    out["full_attention_layers"] = full
    if full and kv_heads and head_dim:
        out["kv_kib_per_token"] = 2 * full * kv_heads * head_dim * bytes_per_value / 1024
    return out


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _median(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _fmt(v, nd=1, unit="") -> str:
    return "n/a" if v is None else f"{v:.{nd}f}{unit}"


def ramp_table(records: list[dict]) -> tuple[list[list], list[tuple[float, float]]]:
    """Median per target size (warm-up excluded). Returns table rows and (tokens, ttft) points."""
    by_target: dict[int, list[dict]] = {}
    for r in records:
        if r.get("kind") == "ramp" and r.get("ok"):
            by_target.setdefault(r["target_tokens"], []).append(r)
    rows, points = [], []
    for target in sorted(by_target):
        rs = by_target[target]
        tokens = _median([r["prompt_tokens"] for r in rs])
        ttft = _median([r["ttft"] for r in rs])
        swap = _median([r.get("swap_used_mb") for r in rs])
        rows.append([target, _fmt(tokens, 0), _fmt(ttft, 1, " s"), _fmt(_median([r["prefill_tps"] for r in rs]), 0),
                     _fmt(None if swap is None else swap / 1024, 2, " GiB"), len(rs)])
        if tokens and ttft:
            points.append((tokens, ttft))
    return rows, points


def _table(header: list[str], rows: list[list]) -> str:
    if not rows:
        return "_no data_\n"
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def write_report(arm_dir: Path) -> Path:
    records = read_jsonl(arm_dir / "results.jsonl")
    env = json.loads((arm_dir / "env.json").read_text()) if (arm_dir / "env.json").exists() else {}
    out = [f"# Arm {env.get('arm', arm_dir.name)}\n"]
    if env:
        keys = ["engine", "quant", "drafter", "block_size", "reasoning", "machine", "macos", "gateway_commit",
                "wired_limit_mb", "cap_gib", "weights_gib", "drafter_gib", "kv", "notes"]
        out += ["## Environment\n", _table(["field", "value"], [[k, env[k]] for k in keys if k in env])]

    rows, points = ramp_table(records)
    out += ["## Prompt-size ramp (median, warm-up excluded)\n",
            _table(["target", "prompt tokens", "TTFT", "prefill tok/s", "swap used", "n"], rows)]
    cliff = find_cliff(points)
    if cliff:
        out.append(f"**Cliff:** between {cliff['last_good_tokens']:.0f} and {cliff['first_bad_tokens']:.0f} prompt tokens "
                   f"(TTFT {cliff['ttft_ratio']:.1f}x the straight-line fit).\n")
        if all(k in env for k in ("cap_gib", "weights_gib", "drafter_gib")):
            kib = implied_kv_kib(env["cap_gib"], env["weights_gib"], env["drafter_gib"],
                                 env.get("overhead_gib", 2.0), cliff["first_bad_tokens"])
            expected = (env.get("kv") or {}).get("kv_kib_per_token")
            out.append(f"Implied context memory if this is a memory cliff: {_fmt(kib, 0)} KiB/token; "
                       f"from config.json: {_fmt(expected, 0)} KiB/token.\n")
    else:
        out.append("**Cliff:** none detected in the sizes tested.\n")

    dec = [r for r in records if r.get("kind") == "decode" and r.get("ok")]
    by_t: dict[int, list[dict]] = {}
    for r in dec:
        by_t.setdefault(r["target_tokens"], []).append(r)
    out += ["## Decode speed\n", _table(["prompt target", "decode tok/s (median)", "completion tokens", "n"],
            [[t, _fmt(_median([r["decode_tps"] for r in rs]), 1), _fmt(_median([r["completion_tokens"] for r in rs]), 0), len(rs)]
             for t, rs in sorted(by_t.items())])]

    steps = [r for r in records if r.get("kind") in ("chain_rolling_step", "chain_keep_step") and r.get("ok")]
    out += ["## Chains: step times\n", _table(["chain", "step", "prompt tokens", "time (s)", "swap used"],
            [[r["kind"].replace("chain_", "").replace("_step", ""), r["label"], r["prompt_tokens"], _fmt(r["total"], 0),
              _fmt(None if r.get("swap_used_mb") is None else r["swap_used_mb"] / 1024, 2, " GiB")] for r in steps])]
    answers = [r for r in records if r.get("kind") == "chain_answers"]
    out += ["## Chains: question accuracy (10 questions)\n",
            _table(["chain", "correct", "of"], [[r["label"], r["score"]["correct"], r["score"]["total"]] for r in answers])]

    cache = [r for r in records if r.get("kind") == "cache"]
    out += ["## Prefix-cache probe\n", _table(["call", "prompt tokens", "TTFT"],
            [[r["label"], r.get("prompt_tokens"), _fmt(r.get("ttft"), 1, " s")] for r in cache])]

    rev = [r for r in records if r.get("kind") == "review" and r.get("score")]
    out.append("## Seeded-defect review\n")
    if rev:
        rows = []
        for size in ("S", "M", "L", "all"):
            sel = [r for r in rev if size == "all" or r["size"] == size]
            buggy = [r for r in sel if not r["clean"]]
            found = sum(len(r["score"]["matched"]) for r in buggy)
            total = sum(r["score"]["n_bugs"] for r in buggy)
            clean = [r for r in sel if r["clean"]]
            rows.append([size, f"{found}/{total}", sum(r["score"]["unmatched"] for r in buggy),
                         f"{sum(r['score']['unmatched'] for r in clean)} over {len(clean)} clean tasks",
                         sum(1 for r in sel if not r["score"]["parse_ok"]), len(sel)])
        out.append(_table(["size", "bugs found", "unmatched findings (buggy)", "unmatched findings (clean)",
                           "unparseable replies", "tasks"], rows))
        kinds: dict[str, list[int]] = {}
        for r in rev:
            for kind, (found, total) in r["score"]["per_kind"].items():
                k = kinds.setdefault(kind, [0, 0])
                k[0] += found
                k[1] += total
        out.append(_table(["bug kind", "found/total"], [[k, f"{v[0]}/{v[1]}"] for k, v in sorted(kinds.items())]))
    else:
        out.append("_no data_\n")

    burst = [r for r in records if r.get("kind") == "burst"]
    if burst:
        out += ["## Burst through the gateway\n", _table(["clients", "served", "rejected", "other"],
                [[r["n"], r["served"], r["rejected"], r["other"]] for r in burst])]
    aborted = [r for r in records if r.get("kind") == "aborted"]
    if aborted:
        out += ["## Aborted runs\n", _table(["where", "reason"], [[r["label"], r["reason"]] for r in aborted])]
    path = arm_dir / "report.md"
    path.write_text("\n".join(out))
    return path
