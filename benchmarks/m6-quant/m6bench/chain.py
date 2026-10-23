"""Rolling-summary test from a real changelog: build the data, and score the answers.

The model reads a changelog in sections. After the last section it must answer ten "which release
shipped this change?" questions. In the rolling variant it only has its own running summary; in the
keep-everything variant it has the whole text. Answers are scored by script.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path

HEADING = re.compile(r"^##\s+(\d[\w.\-]*)\s+(?:/\s+|\()(\d{4}-\d{2}-\d{2})")
VERSION = re.compile(r"\b\d+\.\d+\.\d+(?:\.\w+)?\b")
BULLET = re.compile(r"^\s*[*\-]\s+(.*)$")
N_CHUNKS, CHUNK_CHARS, MAX_CHUNK_CHARS, N_QUESTIONS = 12, 8_500, 12_000, 10


def _sections(text: str) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            out.append((m.group(1), [line]))
        elif out:
            out[-1][1].append(line)
    return out


def _description(bullet: str) -> str:
    """The change text without the trailing 'Pull request [#123](...) by someone'."""
    bullet = re.split(r"\s+(?:Pull request|\[#\d+\])", bullet)[0]
    return re.sub(r"\s+", " ", bullet).strip().rstrip(".")


def build_chain(changelog: Path, seed: int = 20261023) -> dict:
    raw = changelog.read_text()
    sections = _sections(raw)
    chunks: list[dict] = []  # newest first, as in the file
    cur_lines: list[str] = []
    cur_versions: list[str] = []
    for version, lines in sections:
        for idx, line in enumerate(lines):
            starts_item = idx == 0 or bool(BULLET.match(line))
            size = sum(len(x) + 1 for x in cur_lines)
            if cur_lines and starts_item and (size >= CHUNK_CHARS or size + len(line) > MAX_CHUNK_CHARS):
                chunks.append({"versions": cur_versions, "lines": cur_lines})
                cur_lines, cur_versions = [], []
                if idx != 0:
                    cur_lines.append(f"## {version} (continued)")
            if version not in cur_versions:
                cur_versions.append(version)
            cur_lines.append(line)
        if len(chunks) >= N_CHUNKS:
            break
    chunks = chunks[:N_CHUNKS]
    chunks.reverse()  # oldest first

    owner: dict[str, str] = {}  # description -> version, for every bullet in the chosen chunks
    count: dict[str, int] = {}
    per_chunk: list[list[str]] = []
    for chunk in chunks:
        version, found = None, []
        for line in chunk["lines"]:
            h = re.match(r"^##\s+(\d[\w.\-]*)", line)
            if h:
                version = h.group(1)
                continue
            m = BULLET.match(line)
            if m and version:
                d = _description(m.group(1))
                count[d] = count.get(d, 0) + 1
                owner[d] = version
                if len(d.split()) >= 6 and len(d) >= 40:
                    found.append(d)
        per_chunk.append(found)

    rng = random.Random(seed)
    order = list(range(len(chunks)))
    rng.shuffle(order)
    questions, used_versions = [], set()
    for ci in order:
        if len(questions) >= N_QUESTIONS:
            break
        options = [d for d in per_chunk[ci] if count[d] == 1 and owner[d] not in used_versions and not VERSION.search(d)]
        if options:
            d = rng.choice(options)
            used_versions.add(owner[d])
            questions.append({"chunk": ci, "change": d, "answer": owner[d]})
    questions.sort(key=lambda q: q["chunk"])
    for n, q in enumerate(questions, 1):
        q["id"] = str(n)
    return {"source": {"file": changelog.name, "sha256": hashlib.sha256(raw.encode()).hexdigest(),
                       "license": "MIT, Copyright (c) Bundler contributors"},
            "chunks": ["\n".join(c["lines"]) + "\n" for c in chunks], "questions": questions}


def load_chain(path: Path) -> dict:
    return json.loads(Path(path).read_text())


ROLLING_SYSTEM = (
    "You maintain a compact knowledge base of a software changelog that is read in sections. "
    "Output exactly two parts. FACTS: one line per change, in the form `<version> | <short description>`; "
    "keep names of commands, flags, settings and files verbatim; shorten wording, not identifiers; "
    "keep as many distinct changes as fit. EVENTS: at most 5 lines on the big themes."
)


def rolling_prompt(summary: str, chunk: str, i: int, n: int) -> list[dict]:
    return [{"role": "system", "content": ROLLING_SYSTEM},
            {"role": "user", "content": f"CURRENT KNOWLEDGE BASE:\n{summary or '(empty)'}\n\n"
                                        f"NEW CHANGELOG SECTION ({i}/{n}):\n{chunk}\n\nReturn the updated knowledge base."}]


def keep_prompt_first(chunk: str, i: int, n: int) -> str:
    return f"Changelog section {i}/{n}. Read it and reply with only the word Noted.\n\n{chunk}"


def question_block(questions: list[dict]) -> str:
    lines = [f'{q["id"]}. Which release shipped this change: "{q["change"]}"' for q in questions]
    return ("Answer each question with the release version number only (for example 4.0.12), or UNKNOWN. "
            "Respond with ONLY a JSON object mapping the question number (as a string) to the answer.\n\n" + "\n".join(lines))


def score_answers(questions: list[dict], text: str) -> dict:
    from .reviewset import parse_json_value
    got = parse_json_value(text, dict) or {}
    correct, detail = 0, {}
    for q in questions:
        versions = set(VERSION.findall(str(got.get(q["id"], ""))))
        ok = versions == {q["answer"]}
        detail[q["id"]] = ok
        correct += ok
    return {"correct": correct, "total": len(questions), "parse_ok": bool(got), "detail": detail}
