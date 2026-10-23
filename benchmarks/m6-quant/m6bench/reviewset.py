"""Seeded-defect review tasks: generate them from real code, build the prompt, score the reply.

A task is a few real source excerpts (CPython's standard library by default) with 0 to 3 small
mutations injected: a flipped comparison, swapped arguments, a deleted guard, and so on. The reply
is scored by script: a mutation counts as found when a finding cites its file and a line within
`LINE_TOLERANCE` lines. Some mutations can turn out harmless in context ("equivalent mutants");
use `show` to spot-check a sample by hand.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path

LINE_TOLERANCE = 3
SIZE_CHARS = {"S": 11_000, "M": 33_000, "L": 66_000}  # code characters; ~4k, 12k and 24k prompt tokens once numbered
KINDS = ["cmp_boundary", "cmp_invert", "bool_flip", "arg_swap", "missing_guard", "dropped_close"]
MIN_MODULE_CHARS = 4_000
SKIP_MODULES = {"_pydecimal.py", "_pydatetime.py", "pydoc.py", "this.py", "antigravity.py", "__phello__.py"}

_CMP_TEXT = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
             ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in"}
_BOUNDARY = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
_INVERT = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}


@dataclass
class Candidate:
    kind: str
    lineno: int  # 1-based, original numbering
    end_lineno: int
    func: str
    ops: list  # ("replace", lineno, col0, col1, text) | ("delete", lineno0, lineno1)


def _op_regex(text: str) -> str:
    return r"\s+".join(map(re.escape, text.split()))


class _Finder(ast.NodeVisitor):
    def __init__(self, lines: list[str]):
        self.lines, self.stack, self.out = lines, [], []

    def _func(self) -> str | None:
        return self.stack[-1] if self.stack else None

    def _single_line(self, node) -> bool:
        return node.lineno == node.end_lineno and self.lines[node.lineno - 1].isascii()

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _add(self, kind, node, ops):
        self.out.append(Candidate(kind, node.lineno, node.end_lineno, self._func(), ops))

    def visit_Compare(self, node):
        if self._func() and len(node.ops) == 1 and self._single_line(node):
            op = type(node.ops[0])
            right = node.comparators[0]
            if right.lineno == node.lineno and node.left.end_lineno == node.lineno:
                between = self.lines[node.lineno - 1][node.left.end_col_offset:right.col_offset]
                m = re.search(_op_regex(_CMP_TEXT[op]), between)
                new = _BOUNDARY.get(op) or _INVERT.get(op)
                if m and new:
                    kind = "cmp_boundary" if op in _BOUNDARY else "cmp_invert"
                    c0 = node.left.end_col_offset + m.start()
                    self._add(kind, node, [("replace", node.lineno, c0, node.left.end_col_offset + m.end(), _CMP_TEXT[new])])
        self.generic_visit(node)

    def visit_BoolOp(self, node):
        if self._func() and self._single_line(node) and node.values[0].end_lineno == node.values[1].lineno == node.lineno:
            between = self.lines[node.lineno - 1][node.values[0].end_col_offset:node.values[1].col_offset]
            m = re.search(r"\b(and|or)\b", between)
            if m:
                new = "or" if m.group(1) == "and" else "and"
                c0 = node.values[0].end_col_offset + m.start()
                self._add("bool_flip", node, [("replace", node.lineno, c0, node.values[0].end_col_offset + m.end(), new)])
        self.generic_visit(node)

    def visit_Call(self, node):
        simple = (ast.Name, ast.Attribute, ast.Constant)
        if (self._func() and len(node.args) >= 2 and self._single_line(node)
                and all(isinstance(a, simple) for a in node.args[:2])
                and node.args[0].lineno == node.args[1].lineno == node.lineno):
            a, b = node.args[:2]
            line = self.lines[node.lineno - 1]
            ta, tb = line[a.col_offset:a.end_col_offset], line[b.col_offset:b.end_col_offset]
            if ta != tb:
                self._add("arg_swap", node, [("replace", node.lineno, b.col_offset, b.end_col_offset, ta),
                                             ("replace", node.lineno, a.col_offset, a.end_col_offset, tb)])
        self.generic_visit(node)

    def visit_If(self, node):
        if (self._func() and not node.orelse and len(node.body) == 1 and node.end_lineno - node.lineno <= 6
                and isinstance(node.body[0], (ast.Raise, ast.Return, ast.Continue, ast.Break))
                and "__name__" not in ast.dump(node.test)):
            self._add("missing_guard", node, [("delete", node.lineno, node.end_lineno)])
        self.generic_visit(node)

    def visit_Expr(self, node):
        call = node.value
        if (self._func() and isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr in ("close", "release") and node.lineno == node.end_lineno):
            self._add("dropped_close", node, [("delete", node.lineno, node.end_lineno)])
        self.generic_visit(node)


def find_candidates(source: str) -> list[Candidate]:
    tree = ast.parse(source)
    finder = _Finder(source.splitlines())
    finder.visit(tree)
    return finder.out


def apply_ops(source: str, ops: list) -> str:
    lines = source.splitlines()
    for op in sorted(ops, key=lambda o: (o[1], o[2] if o[0] == "replace" else 0), reverse=True):
        if op[0] == "replace":
            _, ln, c0, c1, text = op
            lines[ln - 1] = lines[ln - 1][:c0] + text + lines[ln - 1][c1:]
        else:
            del lines[op[1] - 1:op[2]]
    return "\n".join(lines) + "\n"


def inject(source: str, rng: random.Random, n_bugs: int, min_gap: int = 15) -> tuple[str, list[dict]]:
    """Inject up to `n_bugs` mutations. Returns the new source and the bug records (final line numbers)."""
    by_kind: dict[str, list[Candidate]] = {}
    for c in find_candidates(source):
        by_kind.setdefault(c.kind, []).append(c)
    kinds = sorted(by_kind)
    rng.shuffle(kinds)
    chosen: list[Candidate] = []
    attempts = 0
    while len(chosen) < n_bugs and attempts < 200 and kinds:
        attempts += 1
        kind = kinds[len(chosen) % len(kinds)] if attempts <= len(kinds) * 3 else rng.choice(kinds)
        cand = rng.choice(by_kind[kind])
        if any(abs(cand.lineno - c.lineno) < min_gap or cand.func == c.func for c in chosen):
            continue
        trial = apply_ops(source, [op for c in chosen + [cand] for op in c.ops])
        try:
            ast.parse(trial)
        except SyntaxError:
            by_kind[kind].remove(cand)
            if not by_kind[kind]:
                kinds.remove(kind)
            continue
        chosen.append(cand)
    chosen.sort(key=lambda c: c.lineno)
    new_source = apply_ops(source, [op for c in chosen for op in c.ops])
    src_lines, new_lines = source.splitlines(), new_source.splitlines()
    bugs, shift = [], 0
    for c in chosen:
        final = c.lineno - shift
        deleted = c.ops[0][0] == "delete"
        bugs.append({"kind": c.kind, "function": c.func, "line": final,
                     "original": "\n".join(src_lines[c.lineno - 1:c.end_lineno]).strip(),
                     "mutated": "" if deleted else new_lines[final - 1].strip()})
        if deleted:
            shift += c.end_lineno - c.lineno + 1
    return new_source, bugs


def _stdlib_modules(stdlib: Path) -> list[Path]:
    out = []
    for p in sorted(stdlib.glob("*.py")):
        size = p.stat().st_size
        if p.name in SKIP_MODULES or size < MIN_MODULE_CHARS or not p.read_bytes().isascii():
            continue
        out.append(p)
    return out


def _prefix_at_statement_boundary(source: str, budget: int) -> str:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    end, size = 0, 0
    for node in tree.body:
        chunk = sum(len(line) for line in lines[end:node.end_lineno])
        if size + chunk > budget:
            break
        size += chunk
        end = node.end_lineno
    return "".join(lines[:end])


def assemble(budget: int, rng: random.Random, modules: list[Path], used: set[str]) -> list[dict]:
    """Pick whole modules (or statement-aligned prefixes) until about `budget` characters."""
    order = [m for m in modules if m.name not in used]
    rng.shuffle(order)
    parts, remaining = [], budget
    for path in order:
        if remaining < 2_500:
            break
        text = path.read_text()
        if len(text) > remaining * 1.05:
            text = _prefix_at_statement_boundary(text, remaining)
            if len(text) < 2_500:
                continue
        ast.parse(text)
        parts.append({"path": path.name, "orig_sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text})
        used.add(path.name)
        remaining -= len(text)
    return parts


def build_tasks(stdlib: Path, seed: int = 20261023, per_size: int = 8, clean_per_size: int = 2) -> list[dict]:
    modules = _stdlib_modules(stdlib)
    rng = random.Random(seed)
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    bug_counts = [1, 1, 2, 2, 3, 3]
    tasks = []
    for size in ("S", "M", "L"):
        used: set[str] = set()
        for i in range(per_size):
            clean = i >= per_size - clean_per_size
            parts = assemble(SIZE_CHARS[size], rng, modules, used)
            task = {"id": f"{size}{i + 1:02d}", "size": size, "clean": clean, "corpus": f"CPython {version} stdlib",
                    "files": [], "bugs": []}
            want = 0 if clean else bug_counts[i % len(bug_counts)]
            capacity = [len({c.func for c in find_candidates(part["text"])}) for part in parts]
            counts = [0] * len(parts)
            for _ in range(want):
                open_parts = [k for k, cap in enumerate(capacity) if counts[k] < cap]
                if open_parts:
                    counts[rng.choice(open_parts)] += 1
            for part, n in zip(parts, counts):
                text, bugs = inject(part["text"], rng, n) if n else (part["text"], [])
                task["files"].append({"path": part["path"], "text": text, "orig_sha256": part["orig_sha256"]})
                task["bugs"] += [{**b, "file": part["path"]} for b in bugs]
            tasks.append(task)
    return tasks


def save_tasks(tasks: list[dict], path: Path) -> None:
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        for t in tasks:
            f.write(json.dumps(t) + "\n")


def load_tasks(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def render_numbered(task: dict) -> str:
    out = []
    for f in task["files"]:
        out.append(f"=== {f['path']} ===")
        out += [f"{i:5d} | {line}" for i, line in enumerate(f["text"].splitlines(), 1)]
        out.append("")
    return "\n".join(out)


REVIEW_INSTRUCTIONS = (
    "Review the following Python code excerpts. Each line is prefixed with its line number. "
    "Find definite logic defects: places where the code is wrong. Ignore style, naming, missing docs and "
    "performance. Report at most 5. Respond with ONLY a JSON array; each item is "
    '{"file": "<file name>", "line": <line number>, "kind": "<short category>", "explanation": "<at most 25 words>"}. '
    "If you find no definite defects, respond with []."
)


def build_review_messages(task: dict) -> list[dict]:
    return [{"role": "system", "content": "You are a meticulous code reviewer."},
            {"role": "user", "content": f"{REVIEW_INSTRUCTIONS}\n\n{render_numbered(task)}"}]


def parse_json_value(text: str, want: type):
    """Last JSON value of type `want` in `text` (tolerates reasoning text and code fences)."""
    decoder = json.JSONDecoder()
    opener = "[" if want is list else "{"
    for start in reversed([m.start() for m in re.finditer(re.escape(opener), text)]):
        try:
            value, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if isinstance(value, want):
            return value
    return None


def parse_findings(text: str) -> tuple[list[dict], bool]:
    value = parse_json_value(text, list)
    if value is None:
        return [], False
    return [f for f in value if isinstance(f, dict)], True


def score_review(task: dict, text: str, tolerance: int = LINE_TOLERANCE) -> dict:
    findings, parse_ok = parse_findings(text)
    matched: set[int] = set()
    unmatched = 0
    for f in findings:
        try:
            line = int(f.get("line"))
        except (TypeError, ValueError):
            unmatched += 1
            continue
        name = Path(str(f.get("file", ""))).name
        hit = next((i for i, b in enumerate(task["bugs"])
                    if i not in matched and Path(b["file"]).name == name and abs(b["line"] - line) <= tolerance), None)
        if hit is None:
            unmatched += 1
        else:
            matched.add(hit)
    per_kind: dict[str, list[int]] = {}
    for i, b in enumerate(task["bugs"]):
        k = per_kind.setdefault(b["kind"], [0, 0])
        k[1] += 1
        k[0] += i in matched
    return {"parse_ok": parse_ok, "n_findings": len(findings), "n_bugs": len(task["bugs"]),
            "matched": sorted(matched), "unmatched": unmatched, "per_kind": per_kind}


def show(task: dict) -> str:
    """Human-readable list of a task's injected bugs, for spot-checking."""
    out = [f"{task['id']} ({'clean' if task['clean'] else 'seeded'}), {len(task['files'])} files"]
    for b in task["bugs"]:
        out += [f"  - {b['file']}:{b['line']} in {b['function']}() [{b['kind']}]",
                f"      was: {b['original'].splitlines()[0] if b['original'] else ''}",
                f"      now: {b['mutated'] or '(deleted)'}"]
    return "\n".join(out)
