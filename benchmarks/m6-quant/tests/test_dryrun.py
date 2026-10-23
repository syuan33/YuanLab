"""The whole harness against a fake server: every command runs and its output has the right shape."""

import json
from pathlib import Path

from m6bench import analysis, workloads
from m6bench.cli import main

DATA = Path(__file__).resolve().parent.parent / "data"


def run(url, tmp, *argv):
    return main([argv[0], "--arm", "T", "--out", str(tmp), "--base-url", url, "--interval", "0.1", *argv[1:]])


def records(tmp):
    return analysis.read_jsonl(tmp / "T" / "results.jsonl")


def test_selftest_and_env(fake, tmp_path):
    _, url = fake()
    assert main(["selftest", "--base-url", url, "--arm", "T"]) == 0
    assert main(["env", "--arm", "T", "--out", str(tmp_path), "--engine", "fake", "--quant", "q4", "--cap-gib", "21.3"]) == 0
    env = json.loads((tmp_path / "T" / "env.json").read_text())
    assert env["engine"] == "fake" and env["cap_gib"] == 21.3


def test_ramp_finds_the_planted_cliff_and_reports_it(fake, tmp_path):
    _, url = fake(prefill_tps=20_000, cliff_tokens=6000, cliff_factor=4.0)
    assert run(url, tmp_path, "ramp", "--targets", "500,1000,2000,3000,4000,5000,6000,8000,12000", "--reps", "2") == 0
    recs = records(tmp_path)
    ramp = [r for r in recs if r["kind"] == "ramp"]
    assert len(ramp) == 18 and any(r["kind"] == "warmup" for r in recs) and not any(r["kind"] == "aborted" for r in recs)
    assert all(r["prompt_tokens"] and r["prefill_tps"] and not r["tokens_estimated"] for r in ramp)
    _, points = analysis.ramp_table(recs)
    cliff = analysis.find_cliff(points)
    assert cliff and cliff["last_good_tokens"] < 6000 <= cliff["first_bad_tokens"] < 6100
    assert (tmp_path / "T" / "telemetry.csv").exists()
    report = analysis.write_report(tmp_path / "T").read_text()
    assert "**Cliff:** between" in report


def test_calibration_learns_the_servers_token_size(fake, tmp_path):
    _, url = fake()
    from m6bench.telemetry import Sampler
    cfg = workloads.BenchConfig(base_url=url, model="m", arm="T", out_dir=tmp_path / "T")
    bench = workloads.Bench(cfg)
    assert abs(bench.calibrate() - 3.5) < 0.2


def test_abort_guard_stops_the_ramp(fake, tmp_path):
    _, url = fake(prefill_tps=20_000, cliff_tokens=3000, cliff_factor=30.0)
    run(url, tmp_path, "ramp", "--targets", "1000,2000,3000,4000,5000,6000", "--reps", "1")
    recs = records(tmp_path)
    assert any(r["kind"] == "aborted" for r in recs)
    assert len([r for r in recs if r["kind"] == "ramp"]) < 6


def test_decode_cache_and_chains(fake, tmp_path):
    f, url = fake(decode_tps=2_000)
    assert run(url, tmp_path, "decode", "--prompts", "500", "--reps", "2") == 0
    assert run(url, tmp_path, "cache", "--prefix-tokens", "2000") == 0
    assert run(url, tmp_path, "chains") == 0
    recs = records(tmp_path)
    dec = [r for r in recs if r["kind"] == "decode"]
    assert len(dec) == 2 and all(r["completion_tokens"] == 256 and r["decode_tps"] > 100 for r in dec)
    assert [r["label"] for r in recs if r["kind"] == "cache"] == ["first send", "same prefix again", "control, new prefix"]
    steps = [r for r in recs if r["kind"] in ("chain_rolling_step", "chain_keep_step")]
    assert len(steps) == 24
    keep = [r["prompt_tokens"] for r in steps if r["kind"] == "chain_keep_step"]
    assert keep == sorted(keep) and keep[-1] > 5 * keep[0]          # keep-everything grows
    roll = [r["prompt_tokens"] for r in steps if r["kind"] == "chain_rolling_step"]
    assert max(roll) < 2 * min(roll) + 1000                           # rolling stays flat
    scored = [r for r in recs if r["kind"] == "chain_answers" and "score" in r]
    assert [r["score"]["total"] for r in scored] == [10, 10] and all(r["score"]["parse_ok"] for r in scored)


def test_review_runs_and_is_scored(fake, tmp_path):
    _, url = fake()
    assert run(url, tmp_path, "review", "--sizes", "S", "--limit", "3") == 0
    scored = [r for r in records(tmp_path) if r["kind"] == "review" and "score" in r]
    assert len(scored) == 3 and all(r["score"]["parse_ok"] and r["score"]["matched"] == [] for r in scored)
    text = analysis.write_report(tmp_path / "T").read_text()
    assert "## Seeded-defect review" in text and "0/" in text


def test_burst_counts_status_codes(fake, tmp_path):
    _, url = fake()
    out = workloads.run_burst(url, n=4)
    assert out == {"n": 4, "served": 4, "rejected": 0, "other": 0}
    assert run(url, tmp_path, "burst", "--n", "3", "--expect-served", "3", "--expect-rejected", "0") == 0
    assert run(url, tmp_path, "burst", "--n", "3", "--expect-served", "2", "--expect-rejected", "1") == 1
