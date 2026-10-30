"""Synthetic coverage for the read-only timeline reader (`status` progress + `logs --follow`). Fake secrets only."""
import contextlib
import io
import json
import os
import stat
from unittest import mock

from local_ci_runner import cli, timeline
from tests.fixtures import TmpCase

SECRET = "ghp_FAKESECRETTOKEN0123456789 TEST-OUTPUT-LEAK"
RUN, JOB = "123456789", "verify"


def rec(stage="node-tests", event="started", elapsed=0, pid=4242, state="running", exit_status=None, est=None, ts="2026-01-02T03:04:05Z"):
    return {"stage": stage, "event": event, "timestamp": ts, "elapsed_ms": elapsed, "pid": pid,
            "process_state": state, "exit_status": exit_status, "estimated_progress": est}


def append(f, text):
    with open(f, "a") as h:
        h.write(text)


def line(r):
    return (r if isinstance(r, str) else json.dumps(r)) + "\n"


class TimelineCase(TmpCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.logs = self.home / "logs"

    def write(self, records, run=RUN, job=JOB, name="timeline.log", mode=0o600, raw=None):
        d = self.logs / run / job
        for p in (self.home, self.logs, self.logs / run, d):
            p.mkdir(exist_ok=True)
            os.chmod(p, 0o700)
        f = d / name
        f.write_text(raw if raw is not None else "".join(line(r) for r in records))
        os.chmod(f, mode)
        return f

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        env = {"LOCAL_CI_RUNNER_HOME": str(self.home), "HOME": str(self.tmp)}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def snapshot(self):
        snap = {}
        for root, dirs, files in os.walk(self.home):
            for n in dirs + files:
                p = os.path.join(root, n)
                st = os.lstat(p)
                snap[p] = (stat.S_IMODE(st.st_mode), st.st_mtime_ns, st.st_size)
        return snap


class Progress(TimelineCase):
    def test_status_shows_sanitized_current_progress(self):
        self.write([rec("clj-kondo", "completed", 1000, 1, "exited", 0), rec("node-tests", "heartbeat", 61000, 4242, "running", None, {"estimated": True, "percent": 40})])
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("progress", out)
        for want in ("node-tests", "heartbeat", "4242", "running", "~40%", "estimated"):
            self.assertIn(want, out)

    def test_status_without_timeline_reports_none(self):
        rc, out, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        self.assertIn("progress    : none", out)

    def test_per_stage_failure_is_not_job_terminal(self):
        for ev, state, code in (("failed", "exited", 1), ("timeout", "timed_out", None), ("cancelled", "cancelled", None), ("completed", "exited", 0)):
            self.write([rec("node-tests", ev, 5, 9, state, code)])
            cur, _ = timeline.find_current(self.logs)
            self.assertIsNotNone(cur, ev)

    def test_job_terminal_events_clear_current(self):
        for ev, state, code in (("completed", "exited", 0), ("failed", "exited", 1), ("timeout", "timed_out", None), ("cancelled", "cancelled", None)):
            self.write([rec("node-tests", "started"), rec("verify-job", ev, 9, None, state, code)])
            self.assertIsNone(timeline.find_current(self.logs)[0], ev)
            rc, out, _ = self.run_cli("status")
            self.assertIn("progress    : none", out)
            self.assertNotIn("node-tests", out)

    def test_terminal_job_remains_available_to_explicit_lookup(self):
        self.write([rec("node-tests", "started"), rec("verify-job", "failed", 9, None, "exited", 1)])
        recs = timeline.read_timeline(self.logs, RUN, JOB)
        self.assertEqual([r["stage"] for r in recs], ["node-tests", "verify-job"])

    def test_newest_active_job_wins(self):
        self.write([rec(ts="2026-01-02T03:00:00Z")], run="111", job="old")
        self.write([rec("arch-contract-map", ts="2026-01-02T04:00:00Z")], run="222", job="new")
        cur, _ = timeline.find_current(self.logs)
        self.assertEqual((cur.run_id, cur.job_id, cur.record["stage"]), ("222", "new", "arch-contract-map"))

    def test_redaction_fixture_never_printed(self):
        raw = line(rec()) + line({**rec(), "stage": SECRET})
        self.write(None, raw=raw)
        rc, out, err = self.run_cli("status")
        self.assertNotIn("FAKESECRET", out + err)
        self.assertNotIn("TEST-OUTPUT", out + err)
        rc, out, err = self.run_cli("logs", "--follow")
        self.assertNotIn("FAKESECRET", out + err)
        self.assertNotIn("TEST-OUTPUT", out + err)

    def test_extra_key_value_never_echoed(self):
        self.write(None, raw=line({**rec(), "output": SECRET}))
        self.assertIsNone(timeline.find_current(self.logs)[0])
        rc, out, err = self.run_cli("status")
        self.assertNotIn("FAKESECRET", out + err)
        self.assertIn("rejected", out)


class Validation(TimelineCase):
    def bad(self, raw):
        self.write(None, raw=raw)
        with self.assertRaises(timeline.TimelineError) as cm:
            timeline.read_timeline(self.logs, RUN, JOB)
        self.assertNotIn("FAKESECRET", str(cm.exception))
        self.assertIsNone(timeline.find_current(self.logs)[0])

    def test_malformed_json(self):
        self.bad("{not json}\n")
        self.bad("[1,2]\n")
        self.bad('{"stage": NaN}\n')

    def test_extra_and_missing_keys(self):
        self.bad(line({**rec(), "extra": 1}))
        r = rec(); del r["pid"]
        self.bad(line(r))

    def test_duplicate_keys(self):
        self.bad(json.dumps(rec()).replace('"pid": 4242', '"pid": 4242, "pid": 5') + "\n")

    def test_field_values(self):
        for k, v in (("stage", "other"), ("event", "weird"), ("timestamp", "2026-01-02 03:04:05"), ("timestamp", "2026-13-02T03:04:05Z"),
                     ("timestamp", "2026-01-02T03:04:05+00:00"), ("elapsed_ms", -1), ("elapsed_ms", 1.5), ("elapsed_ms", True),
                     ("pid", 0), ("pid", "7"), ("pid", True), ("process_state", "zombie"), ("exit_status", "0"), ("exit_status", 1.0),
                     ("estimated_progress", 50), ("estimated_progress", {"percent": 50}), ("estimated_progress", {"estimated": False, "percent": 50}),
                     ("estimated_progress", {"estimated": True, "percent": "50"}), ("estimated_progress", {"estimated": True, "percent": 101}),
                     ("estimated_progress", {"estimated": True, "percent": 5, "x": 1})):
            self.bad(line({**rec(), k: v}))

    def test_valid_nullables_and_stages(self):
        for st in ("clj-kondo", "shadow-compile", "node-tests", "dev-tooling-tests", "integration-preflight", "integration-node-tests", "arch-contract-map", "verify-job"):
            self.write([rec(st, "started", pid=None, state="not_run", exit_status=None, est=None)])
            self.assertEqual(len(timeline.read_timeline(self.logs, RUN, JOB)), 1)

    def test_oversized_record_and_file(self):
        self.bad("x" * 2049 + "\n")
        self.bad(line(rec()) * (1024 * 1024 // len(line(rec())) + 2))

    def test_partial_last_line_ignored(self):
        self.write(None, raw=line(rec()) + '{"stage": "node-te')
        self.assertEqual(len(timeline.read_timeline(self.logs, RUN, JOB)), 1)

    def test_non_regular_file_refused(self):
        d = self.write([rec()]).parent
        (d / "timeline.log").unlink()
        os.mkfifo(d / "timeline.log", 0o600)
        with self.assertRaises(timeline.TimelineError):
            timeline.read_timeline(self.logs, RUN, JOB)
        self.assertIsNone(timeline.find_current(self.logs)[0])

    def test_loose_modes_refused_and_not_repaired(self):
        f = self.write([rec()], mode=0o644)
        before = self.snapshot()
        self.assertIsNone(timeline.find_current(self.logs)[0])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o644)
        f2 = self.write([rec()], mode=0o600)
        os.chmod(f2.parent, 0o755)
        self.assertIsNone(timeline.find_current(self.logs)[0])
        self.assertEqual(stat.S_IMODE(f2.parent.stat().st_mode), 0o755)


class PathSafety(TimelineCase):
    def test_traversal_refused(self):
        self.write([rec()])
        for run, job in (("..", JOB), (RUN, ".."), ("../x", JOB), (RUN, "../../etc"), ("12a", JOB), (RUN, ".hidden"), (RUN, "a b"), (RUN, ""), ("", JOB), (RUN, "a/b")):
            with self.assertRaises(timeline.TimelineError, msg=(run, job)):
                timeline.read_timeline(self.logs, run, job)

    def test_symlink_root_refused(self):
        self.write([rec()])
        real = self.home / "real-logs"
        self.logs.rename(real)
        self.logs.symlink_to(real)
        self.assertIsNone(timeline.find_current(self.logs)[0])
        with self.assertRaises(timeline.TimelineError):
            timeline.read_timeline(self.logs, RUN, JOB)

    def test_symlink_run_dir_refused(self):
        self.write([rec()])
        (self.logs / RUN).rename(self.logs / "elsewhere")
        (self.logs / RUN).symlink_to(self.logs / "elsewhere")
        self.assertIsNone(timeline.find_current(self.logs)[0])
        with self.assertRaises(timeline.TimelineError):
            timeline.read_timeline(self.logs, RUN, JOB)

    def test_symlink_job_dir_refused(self):
        self.write([rec()])
        (self.logs / RUN / JOB).rename(self.logs / RUN / ".real")
        (self.logs / RUN / JOB).symlink_to(self.logs / RUN / ".real")
        self.assertIsNone(timeline.find_current(self.logs)[0])
        with self.assertRaises(timeline.TimelineError):
            timeline.read_timeline(self.logs, RUN, JOB)

    def test_symlink_file_refused(self):
        f = self.write([rec()])
        other = f.parent / "other"
        f.rename(other)
        f.symlink_to(other)
        self.assertIsNone(timeline.find_current(self.logs)[0])
        with self.assertRaises(timeline.TimelineError):
            timeline.read_timeline(self.logs, RUN, JOB)

    def test_symlink_rotated_file_refused(self):
        f = self.write([rec()])
        target = self.tmp / "outside"
        target.write_text(line(rec()))
        os.chmod(target, 0o600)
        (f.parent / "timeline.log.1").symlink_to(target)
        with self.assertRaises(timeline.TimelineError):
            timeline.read_timeline(self.logs, RUN, JOB)

    def test_unrelated_names_ignored(self):
        self.write([rec()])
        (self.logs / "notes").mkdir()
        (self.logs / RUN / "not-a-slug!").mkdir()
        (self.logs / RUN / JOB / "timeline.log.4").write_text("garbage")
        self.assertIsNotNone(timeline.find_current(self.logs)[0])


class Rotation(TimelineCase):
    def test_rotated_files_read_oldest_first(self):
        self.write([rec("clj-kondo", "started", 1)], name="timeline.log.2")
        self.write([rec("shadow-compile", "started", 2)], name="timeline.log.1")
        self.write([rec("node-tests", "started", 3)])
        self.assertEqual([r["stage"] for r in timeline.read_timeline(self.logs, RUN, JOB)], ["clj-kondo", "shadow-compile", "node-tests"])
        self.write([rec("dev-tooling-tests", "started", 0)], name="timeline.log.3")
        self.assertEqual(timeline.read_timeline(self.logs, RUN, JOB)[0]["stage"], "dev-tooling-tests")

    def test_active_state_comes_from_newest_file(self):
        self.write([rec("verify-job", "completed", 9, None, "exited", 0)], name="timeline.log.1")
        self.write([rec("node-tests", "started")])
        self.assertIsNotNone(timeline.find_current(self.logs)[0])

    def test_terminal_in_newest_file_after_rotation(self):
        self.write([rec("node-tests", "started")], name="timeline.log.1")
        self.write([rec("verify-job", "failed", 9, None, "exited", 1)])
        self.assertIsNone(timeline.find_current(self.logs)[0])

    def test_total_bound(self):
        for i in (1, 2, 3):
            self.write([rec()], name=f"timeline.log.{i}")
        self.write([rec()])
        self.assertEqual(len(timeline.read_timeline(self.logs, RUN, JOB)), 4)


class Follow(TimelineCase):
    def follow(self, sleeps=()):
        calls = list(sleeps)

        def fake_sleep(_):
            if not calls:
                raise KeyboardInterrupt
            calls.pop(0)()
        with mock.patch.object(timeline, "_sleep", fake_sleep):
            return self.run_cli("logs", "--follow")

    def test_requires_follow_flag(self):
        rc, _, err = self.run_cli("logs")
        self.assertEqual(rc, 2)

    def test_no_active_job(self):
        rc, out, _ = self.run_cli("logs", "--follow")
        self.assertEqual(rc, 1)
        self.assertIn("no active job", out)

    def test_terminal_job_not_followed(self):
        self.write([rec(), rec("verify-job", "completed", 9, None, "exited", 0)])
        rc, out, _ = self.run_cli("logs", "--follow")
        self.assertEqual(rc, 1)
        self.assertNotIn("node-tests", out)

    def test_streams_new_events_then_stops_on_terminal(self):
        f = self.write([rec("clj-kondo", "started", 0)])
        add = lambda r: (lambda: append(f, line(r)))
        rc, out, _ = self.follow([add(rec("clj-kondo", "failed", 5, 7, "exited", 1)),
                                  add(rec("node-tests", "heartbeat", 60000, 8, "running", None, {"estimated": True, "percent": 10})),
                                  add(rec("verify-job", "failed", 70000, None, "exited", 1))])
        self.assertEqual(rc, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("clj-kondo", lines[0]); self.assertIn("failed", lines[1]); self.assertIn("~10%", lines[2]); self.assertIn("verify-job", lines[3])

    def test_no_duplicates_on_repoll_and_rotation(self):
        f = self.write([rec("clj-kondo", "started", 0)])

        def rotate():
            f.rename(f.parent / "timeline.log.1")
            self.write([rec("node-tests", "started", 1)])
        rc, out, _ = self.follow([lambda: None, rotate, lambda: self.write([rec("node-tests", "started", 1), rec("verify-job", "completed", 9, None, "exited", 0)])])
        lines = out.strip().splitlines()
        self.assertEqual(sum("clj-kondo" in l for l in lines), 1)
        self.assertEqual(sum("node-tests" in l for l in lines), 1)

    def test_ctrl_c_exits_cleanly(self):
        self.write([rec()])
        rc, out, err = self.follow()
        self.assertEqual(rc, 130)
        self.assertNotIn("Traceback", err)

    def test_malformed_mid_follow_stops_without_leak(self):
        f = self.write([rec()])
        rc, out, err = self.follow([lambda: append(f, f'{{"stage": "{SECRET}"}}\n')])
        self.assertEqual(rc, 2)
        self.assertNotIn("FAKESECRET", out + err)

    def test_symlink_swap_mid_follow_refused(self):
        f = self.write([rec()])
        other = self.tmp / "other"
        other.write_text(line(rec("arch-contract-map")))
        os.chmod(other, 0o600)

        def swap():
            f.unlink()
            f.symlink_to(other)
        rc, out, err = self.follow([swap])
        self.assertEqual(rc, 2)
        self.assertNotIn("arch-contract-map", out)

    def test_follow_is_read_only(self):
        self.write([rec(), rec("verify-job", "failed", 9, None, "exited", 1)])
        before = self.snapshot()
        self.run_cli("logs", "--follow")
        self.run_cli("status")
        self.assertEqual(self.snapshot(), before)

    def test_does_not_shell_out(self):
        self.write([rec(), rec("verify-job", "failed", 9, None, "exited", 1)])
        with mock.patch("subprocess.Popen", side_effect=AssertionError("spawn")), mock.patch("subprocess.run", side_effect=AssertionError("spawn")):
            self.run_cli("logs", "--follow")
