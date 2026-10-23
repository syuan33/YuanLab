"""Memory telemetry: parsers for macOS tools, a CSV sampler, and the abort guard.

The macOS parsers are written against the usual output of `sysctl vm.swapusage`, `vm_stat`,
`memory_pressure` and `sysctl kern.memorystatus_vm_pressure_level`. Run `selftest` on the M6
before the first real run to confirm they all return numbers there.
"""

from __future__ import annotations

import csv
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

FIELDS = ["t", "swap_used_mb", "swapouts", "pageouts", "mem_free_pct", "pressure_level", "rss_mb"]
_UNIT_MB = {"K": 1 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024.0}
PRESSURE_CRITICAL = 4  # kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical


def parse_swapusage(text: str) -> float | None:
    """`vm.swapusage: total = 2048.00M  used = 1024.50M  free = 1023.50M  (encrypted)` -> used MB."""
    m = re.search(r"used\s*=\s*([\d.]+)\s*([KMGT])", text)
    return float(m.group(1)) * _UNIT_MB[m.group(2)] if m else None


def parse_vm_stat_counter(text: str, name: str) -> int | None:
    """`Pageouts:                              123.` -> 123."""
    m = re.search(rf"^{re.escape(name)}:\s+(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def parse_free_percent(text: str) -> int | None:
    m = re.search(r"free percentage:\s*(\d+)\s*%", text)
    return int(m.group(1)) if m else None


def parse_int(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isdigit() else None


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _meminfo() -> dict[str, int]:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            out[key] = int(rest.split()[0])  # kB
    except (OSError, ValueError, IndexError):
        pass
    return out


def sample_once(pid: int | None = None) -> dict:
    row: dict = dict.fromkeys(FIELDS)
    row["t"] = time.time()
    if sys.platform == "darwin":
        row["swap_used_mb"] = parse_swapusage(_run(["sysctl", "vm.swapusage"]))
        vm = _run(["vm_stat"])
        row["swapouts"] = parse_vm_stat_counter(vm, "Swapouts")
        row["pageouts"] = parse_vm_stat_counter(vm, "Pageouts")
        row["mem_free_pct"] = parse_free_percent(_run(["memory_pressure"]))
        row["pressure_level"] = parse_int(_run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]))
    else:  # Linux: only used for dry runs
        info = _meminfo()
        if "SwapTotal" in info and "SwapFree" in info:
            row["swap_used_mb"] = (info["SwapTotal"] - info["SwapFree"]) / 1024
        if "MemAvailable" in info and info.get("MemTotal"):
            row["mem_free_pct"] = round(100 * info["MemAvailable"] / info["MemTotal"])
    if pid:
        rss_kb = parse_int(_run(["ps", "-o", "rss=", "-p", str(pid)]))
        row["rss_mb"] = None if rss_kb is None else rss_kb / 1024
    return row


class Sampler:
    """Appends a telemetry row to a CSV every `interval` seconds while active."""

    def __init__(self, path: Path, interval: float = 2.0, pid: int | None = None):
        self.path, self.interval, self.pid = Path(path), interval, pid
        self.baseline: dict = {}
        self.last: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._file = None
        self._writer = None
        self._lock = threading.Lock()

    def _write(self, row: dict) -> None:
        with self._lock:
            self._writer.writerow(row)
            self._file.flush()
            self.last = row

    def __enter__(self) -> "Sampler":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=FIELDS)
        self._writer.writeheader()
        self.baseline = sample_once(self.pid)
        self._write(self.baseline)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._write(sample_once(self.pid))

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._write(sample_once(self.pid))
        self._file.close()

    def swap_growth_gb(self) -> float | None:
        a, b = self.baseline.get("swap_used_mb"), self.last.get("swap_used_mb")
        return None if a is None or b is None else (b - a) / 1024


@dataclass
class AbortGuard:
    """Stops a run before it can thrash the machine. Returns a reason string, or None."""

    max_swap_growth_gb: float = 4.0
    slow_factor: float = 5.0
    slow_consecutive: int = 2
    _slow: int = 0

    def check_memory(self, sampler: Sampler | None) -> str | None:
        if sampler is None:
            return None
        growth = sampler.swap_growth_gb()
        if growth is not None and growth > self.max_swap_growth_gb:
            return f"swap grew by {growth:.1f} GiB (limit {self.max_swap_growth_gb:g})"
        if sampler.last.get("pressure_level") == PRESSURE_CRITICAL:
            return "memory pressure is critical"
        return None

    def check_call(self, seconds: float, expected: float | None) -> str | None:
        """`expected` is the time a linear extrapolation of the previous call predicts."""
        if expected and seconds > self.slow_factor * expected:
            self._slow += 1
        else:
            self._slow = 0
        if self._slow >= self.slow_consecutive:
            return f"{self._slow} consecutive calls took more than {self.slow_factor:g}x the expected time"
        return None
