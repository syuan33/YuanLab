import json
import os
from unittest import mock

from local_ci_runner import safety, workspace
from tests.fixtures import TmpCase


class Allowlist(TmpCase):
    def test_only_allow_listed_repo_accepted(self):
        allowed = frozenset({"example-org/example-repo"})
        safety.validate_repo("example-org/example-repo", allowed)
        for bad in ["example-org/other", "evil/example-repo", "example-org/example-repo-fork", "example-org/example-repo/../x", "",
                    "EXAMPLE-ORG/EXAMPLE-REPO", "*"]:
            with self.assertRaises(safety.SafetyError, msg=bad):
                safety.validate_repo(bad, allowed)
        with self.assertRaises(safety.SafetyError):
            safety.validate_repo("example-org/example-repo", frozenset())

    def test_fork_refused(self):
        safety.validate_head_repo("example-org/example-repo", "example-org/example-repo")
        with self.assertRaises(safety.SafetyError):
            safety.validate_head_repo("example-org/example-repo", "mallory/example-repo")

    def test_sha_must_be_full_lowercase_hex(self):
        safety.validate_sha("a" * 40)
        for bad in ["main", "abc123", "A" * 40, "g" * 40, "a" * 39, "a" * 41, "HEAD~1", "a" * 40 + "\n"]:
            with self.assertRaises(safety.SafetyError, msg=bad):
                safety.validate_sha(bad)

    def test_refuses_root(self):
        with mock.patch("os.geteuid", return_value=0):
            with self.assertRaises(safety.SafetyError):
                safety.refuse_root()


class Cleanup(TmpCase):
    def setUp(self):
        super().setUp()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def test_removes_owned_workspace(self):
        ws = workspace.create_workspace(self.runs, "x")
        (ws / "src" / "f").write_text("1")
        safety.safe_remove(ws, self.runs)
        self.assertFalse(ws.exists())

    def test_refuses_unmarked_dir(self):
        d = self.runs / "plain"
        d.mkdir()
        (d / "keep").write_text("1")
        with self.assertRaises(safety.SafetyError):
            safety.safe_remove(d, self.runs)
        self.assertTrue((d / "keep").exists())

    def test_refuses_outside_root(self):
        ws = workspace.create_workspace(self.tmp / "other", "x")
        with self.assertRaises(safety.SafetyError):
            safety.safe_remove(ws, self.runs)
        self.assertTrue(ws.exists())

    def test_refuses_root_itself_and_parents(self):
        for p in (self.runs, self.tmp, self.runs / ".."):
            with self.assertRaises(safety.SafetyError):
                safety.safe_remove(p, self.runs)

    def test_refuses_symlink_to_owned_dir(self):
        ws = workspace.create_workspace(self.runs, "x")
        link = self.runs / "link"
        link.symlink_to(ws)
        with self.assertRaises(safety.SafetyError):
            safety.safe_remove(link, self.runs)
        self.assertTrue(ws.exists())

    def test_refuses_forged_marker_copied_from_other_dir(self):
        ws = workspace.create_workspace(self.runs, "x")
        fake = self.runs / "fake"
        fake.mkdir()
        (fake / safety.MARKER).write_text((ws / safety.MARKER).read_text())
        with self.assertRaises(safety.SafetyError):
            safety.safe_remove(fake, self.runs)

    def test_refuses_foreign_tool_marker(self):
        d = self.runs / "d"
        d.mkdir()
        (d / safety.MARKER).write_text(json.dumps({"tool": "other", "path": str(d)}))
        with self.assertRaises(safety.SafetyError):
            safety.safe_remove(d, self.runs)

    def test_symlink_inside_workspace_not_followed(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "precious").write_text("1")
        ws = workspace.create_workspace(self.runs, "x")
        (ws / "src" / "l").symlink_to(outside)
        safety.safe_remove(ws, self.runs)
        self.assertTrue((outside / "precious").exists())


class Workspace(TmpCase):
    def test_dirty_or_existing_dir_refused(self):
        runs = self.tmp / "runs"
        runs.mkdir()
        (runs / "dirty").mkdir()
        (runs / "dirty" / "leftover").write_text("x")
        with self.assertRaises(safety.SafetyError):
            workspace.create_workspace(runs, "dirty")

    def test_each_workspace_is_fresh_and_private(self):
        runs = self.tmp / "runs"
        a = workspace.create_workspace(runs)
        b = workspace.create_workspace(runs)
        self.assertNotEqual(a, b)
        self.assertEqual(os.stat(a).st_mode & 0o777, 0o700)
        self.assertEqual(list((a / "src").iterdir()), [])
