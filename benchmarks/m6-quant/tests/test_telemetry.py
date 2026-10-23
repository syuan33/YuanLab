import csv
import time
from types import SimpleNamespace

from m6bench import telemetry as t


def test_parse_swapusage():
    s = "vm.swapusage: total = 2048.00M  used = 1024.50M  free = 1023.50M  (encrypted)"
    assert t.parse_swapusage(s) == 1024.5
    assert t.parse_swapusage("vm.swapusage: total = 4.00G  used = 3.50G  free = 0.50G") == 3584.0
    assert t.parse_swapusage("vm.swapusage: total = 0.00M  used = 0.00M  free = 0.00M") == 0.0
    assert t.parse_swapusage("nonsense") is None


def test_parse_vm_stat_and_pressure():
    vm = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
          "Pages free:                               3441.\n"
          "Pageouts:                                 5021.\n"
          "Swapins:                                    12.\n"
          "Swapouts:                                 1234.\n")
    assert t.parse_vm_stat_counter(vm, "Pageouts") == 5021
    assert t.parse_vm_stat_counter(vm, "Swapouts") == 1234
    assert t.parse_vm_stat_counter(vm, "Missing") is None
    assert t.parse_free_percent("The system has 34359738368 (2097152 pages)\nSystem-wide memory free percentage: 62%\n") == 62
    assert t.parse_int("4\n") == 4 and t.parse_int("") is None


def test_sampler_writes_csv(tmp_path):
    path = tmp_path / "t.csv"
    with t.Sampler(path, interval=0.05) as s:
        time.sleep(0.2)
        assert s.baseline["t"] <= s.last["t"]
    rows = list(csv.DictReader(path.open()))
    assert len(rows) >= 3 and set(rows[0]) == set(t.FIELDS)


def _sampler(growth, level=1):
    return SimpleNamespace(swap_growth_gb=lambda: growth, last={"pressure_level": level})


def test_guard_memory():
    g = t.AbortGuard(max_swap_growth_gb=4)
    assert g.check_memory(None) is None
    assert g.check_memory(_sampler(1.0)) is None
    assert "swap grew" in g.check_memory(_sampler(4.5))
    assert "critical" in g.check_memory(_sampler(0.0, level=4))
    assert g.check_memory(_sampler(None)) is None


def test_guard_needs_two_slow_calls_in_a_row():
    g = t.AbortGuard(slow_factor=5, slow_consecutive=2)
    assert g.check_call(60, 10) is None      # one slow call
    assert g.check_call(9, 10) is None       # recovered: counter resets
    assert g.check_call(60, 10) is None
    assert "consecutive" in g.check_call(70, 10)
    assert g.check_call(100, None) is None   # no expectation, no verdict
