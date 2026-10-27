from __future__ import annotations

import re

R = "[REDACTED]"
_PATTERNS = [
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer|basic|token)?\s*\S+"),
    re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|private[_-]?key|credential)[A-Za-z0-9_.-]*\s*[=:]\s*)\S+"),
    re.compile(r"(://[^/\s:@]+:)[^@\s]+(@)"),
]
_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")
_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")


class Redactor:
    """Stateful line redactor (state needed for multi-line private key blocks)."""

    def __init__(self, literals=()):
        self.literals = [x for x in literals if x and len(x) >= 6]
        self._in_key = False

    def line(self, s: str) -> str:
        if self._in_key:
            if _END.search(s):
                self._in_key = False
            return R
        if _BEGIN.search(s):
            self._in_key = not _END.search(s)
            return R
        for lit in self.literals:
            s = s.replace(lit, R)
        s = _PATTERNS[0].sub(R, s)
        s = _PATTERNS[1].sub(lambda m: m.group(1) + R, s)
        s = _PATTERNS[2].sub(lambda m: m.group(1) + R, s)
        s = _PATTERNS[3].sub(lambda m: m.group(1) + R + m.group(2), s)
        return s
