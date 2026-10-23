import ast
import random
import sys
from pathlib import Path

import pytest

from m6bench import reviewset as rs

DATA = Path(__file__).resolve().parent.parent / "data" / "review_tasks.jsonl.gz"

SAMPLE = '''
import os

def a(x, y):
    if x < y:
        return x
    return y

def b(items, name):
    if items is None:
        raise ValueError("no items")
    if name in items and len(items) > 1:
        return items[name]
    return None

def c(path):
    f = open(path)
    data = f.read()
    f.close()
    return data

def d(lo, hi):
    return range(lo, hi)

def e(seq):
    return seq == []
'''


def kinds(source):
    return {c.kind for c in rs.find_candidates(source)}


def test_every_kind_is_found_and_stays_valid():
    assert kinds(SAMPLE) == set(rs.KINDS)
    for cand in rs.find_candidates(SAMPLE):
        out = rs.apply_ops(SAMPLE, cand.ops)
        assert ast.dump(ast.parse(out)) != ast.dump(ast.parse(SAMPLE)), cand.kind
        assert out != SAMPLE


def pick(kind, needle):
    """The candidate of `kind` whose source line contains `needle`."""
    lines = SAMPLE.splitlines()
    return next(c for c in rs.find_candidates(SAMPLE) if c.kind == kind and needle in lines[c.lineno - 1])


def test_specific_mutations():
    assert "if x <= y:" in rs.apply_ops(SAMPLE, pick("cmp_boundary", "x < y").ops)
    assert "len(items) >= 1" in rs.apply_ops(SAMPLE, pick("cmp_boundary", "len(items) > 1").ops)
    assert "if items is not None:" in rs.apply_ops(SAMPLE, pick("cmp_invert", "items is None").ops)
    assert "name not in items" in rs.apply_ops(SAMPLE, pick("cmp_invert", "name in items").ops)
    assert "name in items or len(items)" in rs.apply_ops(SAMPLE, pick("bool_flip", "name in items").ops)
    assert "range(hi, lo)" in rs.apply_ops(SAMPLE, pick("arg_swap", "range(lo, hi)").ops)
    assert "f.close()" not in rs.apply_ops(SAMPLE, pick("dropped_close", "f.close()").ops)
    assert 'raise ValueError("no items")' not in rs.apply_ops(SAMPLE, pick("missing_guard", "items is None").ops)
    assert "seq != []" in rs.apply_ops(SAMPLE, pick("cmp_invert", "seq == []").ops)


def test_inject_reports_final_line_numbers():
    rng = random.Random(3)
    new, bugs = rs.inject(SAMPLE, rng, 3, min_gap=4)
    assert 1 <= len(bugs) <= 3
    lines = new.splitlines()
    for b in bugs:
        assert 1 <= b["line"] <= len(lines)
        if b["mutated"]:
            assert lines[b["line"] - 1].strip() == b["mutated"]
            assert b["mutated"] != b["original"].splitlines()[0].strip()
    ast.parse(new)


def test_inject_is_deterministic():
    assert rs.inject(SAMPLE, random.Random(9), 2, min_gap=4) == rs.inject(SAMPLE, random.Random(9), 2, min_gap=4)


def test_parse_findings_variants():
    assert rs.parse_findings("[]") == ([], True)
    fenced = 'Here you go:\n```json\n[{"file": "a.py", "line": 4, "kind": "x", "explanation": "y"}]\n```'
    assert rs.parse_findings(fenced)[0][0]["line"] == 4
    thinking = 'I considered [1, 2, 3] first. Final answer: [{"file": "a.py", "line": 9}]'
    assert rs.parse_findings(thinking)[0] == [{"file": "a.py", "line": 9}]
    assert rs.parse_findings("no json here") == ([], False)


TASK = {"id": "T", "clean": False, "files": [{"path": "a.py", "text": "x\n"}],
        "bugs": [{"file": "a.py", "line": 10, "kind": "cmp_invert"}, {"file": "b.py", "line": 50, "kind": "missing_guard"}]}


def test_score_review():
    reply = '[{"file":"a.py","line":12},{"file":"x/b.py","line":50},{"file":"a.py","line":200},{"file":"a.py","line":"?"}]'
    s = rs.score_review(TASK, reply)
    assert s["matched"] == [0, 1] and s["unmatched"] == 2 and s["parse_ok"]
    assert s["per_kind"] == {"cmp_invert": [1, 1], "missing_guard": [1, 1]}
    # one finding cannot match two bugs, and a second finding on the same bug is unmatched
    s = rs.score_review(TASK, '[{"file":"a.py","line":10},{"file":"a.py","line":11}]')
    assert s["matched"] == [0] and s["unmatched"] == 1
    assert rs.score_review(TASK, "garbage")["parse_ok"] is False
    clean = {**TASK, "clean": True, "bugs": []}
    assert rs.score_review(clean, '[{"file":"a.py","line":1}]')["unmatched"] == 1


@pytest.fixture(scope="module")
def tasks():
    return rs.load_tasks(DATA)


def test_committed_dataset_shape(tasks):
    assert len(tasks) == 24
    for size in "SML":
        group = [t for t in tasks if t["size"] == size]
        assert len(group) == 8 and sum(t["clean"] for t in group) == 2
    assert all(not t["bugs"] for t in tasks if t["clean"])
    total = sum(len(t["bugs"]) for t in tasks)
    assert 30 <= total <= 36
    assert {b["kind"] for t in tasks for b in t["bugs"]} >= {"cmp_boundary", "cmp_invert", "bool_flip", "missing_guard"}


def test_committed_dataset_is_consistent(tasks):
    for t in tasks:
        sizes = {f["path"]: len(f["text"].splitlines()) for f in t["files"]}
        for f in t["files"]:
            ast.parse(f["text"])
        for b in t["bugs"]:
            assert 1 <= b["line"] <= sizes[b["file"]] + 1
            assert b["original"] and b["original"] != b["mutated"]
        text = rs.render_numbered(t)
        assert text.startswith("===") and "|" in text
    assert "S01" in rs.show(tasks[0]) or tasks[0]["id"] in rs.show(tasks[0])


@pytest.mark.skipif(not Path(f"/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}").exists(),
                    reason="stdlib not at the usual path")
def test_regeneration_matches_committed_data_when_stdlib_matches(tasks):
    stdlib = Path(f"/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}")
    if f"CPython {sys.version_info.major}.{sys.version_info.minor} stdlib" != tasks[0]["corpus"]:
        pytest.skip("stdlib version differs from the committed corpus")
    assert rs.build_tasks(stdlib) == tasks
