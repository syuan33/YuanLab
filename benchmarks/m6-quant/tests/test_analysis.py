import json

from m6bench import analysis as a


def linear(n_points=8, step=2000, rate=250.0, overhead=1.0):
    return [(step * (i + 1), overhead + step * (i + 1) / rate) for i in range(n_points)]


def test_no_cliff_in_a_straight_line():
    assert a.find_cliff(linear()) is None


def test_cliff_found_at_the_jump():
    pts = linear(10)
    pts = [(n, t if n < 12000 else t * 3.5) for n, t in pts]
    cliff = a.find_cliff(pts)
    assert cliff["last_good_tokens"] == 10000 and cliff["first_bad_tokens"] == 12000
    assert cliff["ttft_ratio"] > 2


def test_single_outlier_in_the_middle_is_not_a_cliff():
    pts = linear(10)
    pts[6] = (pts[6][0], pts[6][1] * 4)   # one noisy call, the next point is back on the line
    assert a.find_cliff(pts) is None


def test_cliff_at_last_point_counts():
    pts = linear(6)
    pts[-1] = (pts[-1][0], pts[-1][1] * 5)
    assert a.find_cliff(pts)["first_bad_tokens"] == pts[-1][0]


def test_implied_kv():
    # 26 GiB cap, 14.1 weights, 1.2 drafter, 2 overhead -> 8.7 GiB for context; cliff at 36k tokens
    kib = a.implied_kv_kib(26, 14.1, 1.2, 2.0, 36_000)
    assert abs(kib - 8.7 * 1024 * 1024 / 36_000) < 1e-6
    assert a.implied_kv_kib(21.3, 20.4, 1.2, 2.0, 30_000) is None   # no room at all


def test_kv_per_token_dense_and_hybrid():
    dense = {"num_hidden_layers": 64, "num_key_value_heads": 8, "head_dim": 128}
    assert a.kv_kib_per_token(dense)["kv_kib_per_token"] == 256
    hybrid = {"text_config": {"num_hidden_layers": 64, "num_key_value_heads": 4, "hidden_size": 5120, "num_attention_heads": 40,
                              "layer_types": ["linear_attention"] * 48 + ["full_attention"] * 16}}
    out = a.kv_kib_per_token(hybrid)
    assert out["full_attention_layers"] == 16 and out["head_dim"] == 128 and out["kv_kib_per_token"] == 2 * 16 * 4 * 128 * 2 / 1024


def test_report_from_records(tmp_path):
    arm = tmp_path / "A1"
    arm.mkdir()
    env = {"arm": "A1", "engine": "x", "cap_gib": 21.3, "weights_gib": 14.1, "drafter_gib": 1.2, "kv": {"kv_kib_per_token": 256}}
    (arm / "env.json").write_text(json.dumps(env))
    recs = []
    for rep in range(2):
        for n, t in [(m, (t if m < 12000 else t * 3.5)) for m, t in linear(10)]:
            recs.append({"kind": "ramp", "ok": True, "target_tokens": n, "prompt_tokens": n, "ttft": t, "prefill_tps": n / t,
                         "swap_used_mb": 512.0})
    recs.append({"kind": "chain_answers", "label": "rolling (scored)", "score": {"correct": 4, "total": 10}})
    recs.append({"kind": "review", "label": "S01 (scored)", "size": "S", "clean": False,
                 "score": {"matched": [0], "n_bugs": 2, "unmatched": 1, "parse_ok": True, "per_kind": {"cmp_invert": [1, 2]}}})
    recs.append({"kind": "aborted", "label": "x", "reason": "swap grew"})
    (arm / "results.jsonl").write_text("\n".join(json.dumps(r) for r in recs))
    text = a.write_report(arm).read_text()
    for needle in ("## Prompt-size ramp", "**Cliff:** between 10000 and 12000", "Implied context memory", "4", "1/2", "swap grew"):
        assert needle in text
