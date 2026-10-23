import json
import re
from pathlib import Path

from m6bench import chain as ch

DATA = Path(__file__).resolve().parent.parent / "data" / "chain_bundler.json"


def test_committed_chain_data():
    d = json.loads(DATA.read_text())
    assert len(d["chunks"]) == 12 and len(d["questions"]) == 10
    assert d["source"]["license"].startswith("MIT")
    answers = [q["answer"] for q in d["questions"]]
    assert len(set(answers)) == 10                      # one question per release: no ambiguity
    for q in d["questions"]:
        chunk = re.sub(r"\s+", " ", d["chunks"][q["chunk"]])
        assert chunk.count(q["change"]) == 1
        before = chunk[:chunk.index(q["change"])]
        assert re.findall(r"## (\d[\w.\-]*)", before)[-1] == q["answer"]   # the release heading above the bullet
        assert all(q["change"] not in re.sub(r"\s+", " ", c) for i, c in enumerate(d["chunks"]) if i != q["chunk"])


def test_score_answers():
    qs = [{"id": "1", "answer": "2.3.12"}, {"id": "2", "answer": "2.4.3"}, {"id": "3", "answer": "4.0.1"}]
    s = ch.score_answers(qs, 'Sure! {"1": "2.3.12", "2": "version 2.4.4", "3": "UNKNOWN"}')
    assert (s["correct"], s["total"], s["parse_ok"]) == (1, 3, True)
    assert ch.score_answers(qs, "nothing")["parse_ok"] is False
    assert ch.score_answers(qs, '{"1": "2.3.12 or 2.3.13"}')["correct"] == 0   # hedging is wrong


def test_prompts_contain_the_pieces():
    qs = [{"id": "1", "change": "Fix a thing", "answer": "1.0.0"}]
    assert "Fix a thing" in ch.question_block(qs)
    msgs = ch.rolling_prompt("", "SECTION", 1, 12)
    assert "(empty)" in msgs[1]["content"] and "SECTION" in msgs[1]["content"]
