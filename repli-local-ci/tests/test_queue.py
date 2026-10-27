import os

from repli_local_ci.queue import JobQueue
from tests.fixtures import TmpCase

R = "syuan33/repli"
A, B, C = "a" * 40, "b" * 40, "c" * 40


class QueueTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.q = JobQueue(self.tmp / "q")

    def test_dedupe_same_repo_sha(self):
        j1, s1 = self.q.enqueue(R, A, "feat")
        j2, s2 = self.q.enqueue(R, A, "feat")
        self.assertEqual((s1, s2), ("queued", "duplicate"))
        self.assertEqual(j1["id"], j2["id"])

    def test_new_head_supersedes_queued(self):
        j1, _ = self.q.enqueue(R, A, "feat")
        j2, _ = self.q.enqueue(R, B, "feat")
        self.assertEqual(self.q.get(j1["id"])["state"], "superseded")
        self.assertEqual(self.q.get(j2["id"])["state"], "queued")

    def test_other_branch_not_superseded(self):
        j1, _ = self.q.enqueue(R, A, "feat1")
        self.q.enqueue(R, B, "feat2")
        self.assertEqual(self.q.get(j1["id"])["state"], "queued")

    def test_new_head_requests_cancel_of_running(self):
        j1, _ = self.q.enqueue(R, A, "feat")
        self.assertTrue(self.q.claim(j1["id"]))
        self.q.enqueue(R, B, "feat")
        self.assertTrue(self.q.cancel_requested(j1["id"]))

    def test_concurrency_one(self):
        j1, _ = self.q.enqueue(R, A, "x")
        j2, _ = self.q.enqueue(R, B, "y")
        self.assertTrue(self.q.claim(j1["id"]))
        self.assertFalse(self.q.claim(j2["id"]))
        self.q.finish(j1["id"], "done")
        self.assertTrue(self.q.claim(j2["id"]))

    def test_fifo_head(self):
        j1, _ = self.q.enqueue(R, A, "x")
        j2, _ = self.q.enqueue(R, B, "y")
        self.assertEqual(self.q.head()["id"], j1["id"])
        self.assertFalse(self.q.claim(j2["id"]))  # not at the head -> must wait

    def test_stale_running_job_is_reclaimed(self):
        j1, _ = self.q.enqueue(R, A, "x")
        self.q.claim(j1["id"], pid=2 ** 22 + 12345)  # nonexistent pid
        j2, _ = self.q.enqueue(R, B, "y")
        self.assertTrue(self.q.claim(j2["id"]))
        self.assertEqual(self.q.get(j1["id"])["state"], "failed")

    def test_live_pid_is_not_stolen(self):
        j1, _ = self.q.enqueue(R, A, "x")
        self.q.claim(j1["id"], pid=os.getpid())
        j2, _ = self.q.enqueue(R, B, "y")
        self.assertFalse(self.q.claim(j2["id"]))
