"""Command line: `python -m m6bench <command>`. See README.md for the run book."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

import httpx

from . import analysis, chain as chainlib, reviewset, workloads
from .telemetry import AbortGuard, Sampler, _run, sample_once

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
GIB = 1024 ** 3


def _dir_gib(path: str | None) -> float | None:
    if not path:
        return None
    return round(sum(f.stat().st_size for f in Path(path).expanduser().rglob("*.safetensors")) / GIB, 2)


def collect_env(args) -> dict:
    env: dict = {"arm": args.arm, "engine": args.engine, "quant": args.quant, "drafter": args.drafter,
                 "block_size": args.block_size, "reasoning": args.reasoning, "notes": args.notes,
                 "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "python": platform.python_version()}
    if sys.platform == "darwin":
        env["machine"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        env["macos"] = _run(["sw_vers", "-productVersion"]).strip()
        mem = _run(["sysctl", "-n", "hw.memsize"]).strip()
        env["ram_gib"] = round(int(mem) / GIB, 1) if mem.isdigit() else None
        wired = _run(["sysctl", "-n", "iogpu.wired_limit_mb"]).strip()
        env["wired_limit_mb"] = int(wired) if wired.isdigit() else None
        if env["ram_gib"]:  # 0 means the system default, commonly reported as about two thirds of RAM
            env["cap_gib"] = round(env["wired_limit_mb"] / 1024, 1) if env["wired_limit_mb"] else round(env["ram_gib"] * 2 / 3, 1)
    else:
        env["machine"] = platform.platform()
    env["weights_gib"], env["drafter_gib"] = _dir_gib(args.model_dir), _dir_gib(args.drafter_dir)
    if args.cap_gib:
        env["cap_gib"] = args.cap_gib
    if args.model_config:
        env["kv"] = analysis.kv_kib_per_token(json.loads(Path(args.model_config).expanduser().read_text()))
    try:
        env["gateway_commit"] = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                                               capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        env["gateway_commit"] = None
    return env


def _config(args) -> workloads.BenchConfig:
    return workloads.BenchConfig(
        base_url=args.base_url, model=args.model, arm=args.arm, out_dir=Path(args.out) / args.arm,
        api_key=args.api_key, extra_body=json.loads(args.extra_body) if args.extra_body else {}, pid=args.pid,
        stream_options=not args.no_stream_options, timeout=args.timeout, interval=args.interval)


def _run_with_telemetry(args, fn) -> None:
    cfg = _config(args)
    with Sampler(cfg.out_dir / "telemetry.csv", interval=cfg.interval, pid=cfg.pid) as sampler:
        bench = workloads.Bench(cfg, sampler, AbortGuard(max_swap_growth_gb=args.max_swap_growth_gb))
        bench.calibrate()
        print(f"characters per token: {bench.chars_per_token:.2f}")
        fn(bench)
    print(f"results: {bench.results_path}")


def cmd_selftest(args) -> int:
    t0 = time.perf_counter()
    row = sample_once(args.pid)
    print(f"telemetry sample took {time.perf_counter() - t0:.2f}s")
    missing = [k for k, v in row.items() if v is None]
    for k, v in row.items():
        print(f"  {k:15s} {v}")
    if sys.platform == "darwin" and missing:
        print(f"WARNING: no value for {missing}; fix the parser in m6bench/telemetry.py before trusting results")
    try:
        r = httpx.get(f"{args.base_url.rstrip('/')}/v1/models", timeout=10)
        print(f"GET /v1/models -> {r.status_code}")
    except httpx.HTTPError as exc:
        print(f"GET /v1/models failed: {type(exc).__name__}")
        return 1
    return 0


def cmd_env(args) -> int:
    path = Path(args.out) / args.arm / "env.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    env = collect_env(args)
    path.write_text(json.dumps(env, indent=2))
    print(json.dumps(env, indent=2))
    return 0


def cmd_ramp(args) -> int:
    targets = [int(x) for x in args.targets.split(",")]
    _run_with_telemetry(args, lambda b: workloads.run_ramp(b, targets, args.reps))
    return 0


def cmd_decode(args) -> int:
    prompts = [int(x) for x in args.prompts.split(",")]
    _run_with_telemetry(args, lambda b: workloads.run_decode(b, prompts, args.reps, args.max_tokens))
    return 0


def cmd_chains(args) -> int:
    data = chainlib.load_chain(Path(args.chain))
    _run_with_telemetry(args, lambda b: workloads.run_chains(b, data, tuple(args.which.split(",")), args.summary_max_tokens))
    return 0


def cmd_cache(args) -> int:
    _run_with_telemetry(args, lambda b: workloads.run_cache_probe(b, args.prefix_tokens))
    return 0


def cmd_review(args) -> int:
    tasks = reviewset.load_tasks(Path(args.tasks))
    sizes = set(args.sizes.split(","))
    tasks = [t for t in tasks if t["size"] in sizes]
    if args.limit:
        tasks = tasks[:args.limit]
    _run_with_telemetry(args, lambda b: workloads.run_review(b, tasks, args.max_tokens))
    return 0


def cmd_burst(args) -> int:
    out = workloads.run_burst(args.base_url, args.n, args.api_key, args.model)
    print(out)
    cfg = _config(args)
    workloads.Bench(cfg).write({"kind": "burst", **out})
    if args.expect_served is not None and (out["served"], out["rejected"]) != (args.expect_served, args.expect_rejected):
        print(f"MISMATCH: expected {args.expect_served} served and {args.expect_rejected} rejected")
        return 1
    return 0


def cmd_report(args) -> int:
    print(analysis.write_report(Path(args.out) / args.arm))
    return 0


def cmd_show(args) -> int:
    for t in reviewset.load_tasks(Path(args.tasks)):
        if not args.id or t["id"] == args.id:
            print(reviewset.show(t))
    return 0


def cmd_build_chain(args) -> int:
    data = chainlib.build_chain(Path(args.changelog))
    Path(args.output).write_text(json.dumps(data))
    print(f"{len(data['chunks'])} chunks, {len(data['questions'])} questions -> {args.output}")
    return 0


def cmd_build_review(args) -> int:
    tasks = reviewset.build_tasks(Path(args.stdlib), seed=args.seed)
    reviewset.save_tasks(tasks, Path(args.output))
    print(f"{len(tasks)} tasks, {sum(len(t['bugs']) for t in tasks)} injected bugs -> {args.output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="m6bench")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, run=True):
        sp.add_argument("--arm", default="A1", help="label for this run, e.g. A1")
        sp.add_argument("--out", default=str(ROOT / "results"))
        if run:
            sp.add_argument("--base-url", default="http://127.0.0.1:8000")
            sp.add_argument("--model", default="default")
            sp.add_argument("--api-key")
            sp.add_argument("--extra-body", help='JSON merged into every request, e.g. \'{"reasoning_effort":"low"}\'')
            sp.add_argument("--pid", type=int, help="model server process id, for resident memory")
            sp.add_argument("--no-stream-options", action="store_true")
            sp.add_argument("--timeout", type=float, default=1500)
            sp.add_argument("--interval", type=float, default=2.0, help="telemetry seconds")
            sp.add_argument("--max-swap-growth-gb", type=float, default=4.0)
        return sp

    s = common(sub.add_parser("selftest", help="check telemetry parsers and the server"))
    s.set_defaults(fn=cmd_selftest)
    s = common(sub.add_parser("env", help="record the environment"), run=False)
    for a in ("engine", "quant", "drafter", "block-size", "reasoning", "notes", "model-dir", "drafter-dir", "model-config"):
        s.add_argument(f"--{a}")
    s.add_argument("--cap-gib", type=float)
    s.set_defaults(fn=cmd_env)
    s = common(sub.add_parser("ramp"))
    s.add_argument("--targets", default=",".join(map(str, workloads.RAMP_TARGETS)))
    s.add_argument("--reps", type=int, default=3)
    s.set_defaults(fn=cmd_ramp)
    s = common(sub.add_parser("decode"))
    s.add_argument("--prompts", default="1000,8000")
    s.add_argument("--reps", type=int, default=3)
    s.add_argument("--max-tokens", type=int, default=256)
    s.set_defaults(fn=cmd_decode)
    s = common(sub.add_parser("chains"))
    s.add_argument("--chain", default=str(DATA / "chain_bundler.json"))
    s.add_argument("--which", default="rolling,keep")
    s.add_argument("--summary-max-tokens", type=int, default=1200)
    s.set_defaults(fn=cmd_chains)
    s = common(sub.add_parser("cache"))
    s.add_argument("--prefix-tokens", type=int, default=10_000)
    s.set_defaults(fn=cmd_cache)
    s = common(sub.add_parser("review"))
    s.add_argument("--tasks", default=str(DATA / "review_tasks.jsonl.gz"))
    s.add_argument("--sizes", default="S,M,L")
    s.add_argument("--limit", type=int)
    s.add_argument("--max-tokens", type=int, default=1500)
    s.set_defaults(fn=cmd_review)
    s = common(sub.add_parser("burst", help="11 simultaneous requests; point --base-url at the gateway"))
    s.add_argument("--n", type=int, default=11)
    s.add_argument("--expect-served", type=int)
    s.add_argument("--expect-rejected", type=int)
    s.set_defaults(fn=cmd_burst)
    s = common(sub.add_parser("report"), run=False)
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("show", help="list injected bugs, for spot-checking")
    s.add_argument("--tasks", default=str(DATA / "review_tasks.jsonl.gz"))
    s.add_argument("--id")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("build-chain")
    s.add_argument("--changelog", required=True)
    s.add_argument("--output", default=str(DATA / "chain_bundler.json"))
    s.set_defaults(fn=cmd_build_chain)
    s = sub.add_parser("build-review")
    s.add_argument("--stdlib", required=True)
    s.add_argument("--seed", type=int, default=20261023)
    s.add_argument("--output", default=str(DATA / "review_tasks.jsonl.gz"))
    s.set_defaults(fn=cmd_build_review)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
