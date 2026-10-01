import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


from ralph import jobs

class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "jobs.sqlite"
        self.store = jobs.JobStore(self.path)

    def enqueue(self, head="a" * 40, *, force=False):
        return self.store.enqueue(42, "master", head, "synchronize", force)

    def row(self, job):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM jobs WHERE id = ?",
                                   (job["id"],)).fetchone())

    def test_queued_work_survives_restart_and_duplicate_event_coalesces(self):
        self.assertTrue(self.enqueue())
        self.assertFalse(self.enqueue())
        reopened = jobs.JobStore(self.path)
        self.assertEqual(reopened.recover(), 0)
        job = reopened.claim()
        self.assertEqual((job["number"], job["head"], job["generation"]),
                         (42, "a" * 40, 1))
        self.assertTrue(reopened.complete(job))
        self.assertFalse(reopened.enqueue(42, "master", "a" * 40,
                                          "synchronize"))

    def test_pending_head_coalesces_and_force_promotes_same_head(self):
        self.enqueue()
        self.assertTrue(self.enqueue("b" * 40))
        self.assertFalse(self.enqueue("b" * 40))
        self.assertTrue(self.enqueue("b" * 40, force=True))
        self.assertFalse(self.enqueue("b" * 40, force=True))
        job = self.store.claim()
        self.assertEqual((job["head"], job["generation"], job["force"]),
                         ("b" * 40, 2, True))
        self.assertIsNone(self.store.claim())

    def test_base_change_with_same_head_starts_new_generation(self):
        self.enqueue()
        self.assertTrue(self.store.enqueue(42, "release", "a" * 40,
                                           "edited"))
        first = self.store.claim()
        self.assertEqual((first["base_ref"], first["generation"]),
                         ("release", 2))
        self.assertTrue(self.store.complete(first))
        self.assertTrue(self.store.enqueue(42, "master", "a" * 40,
                                           "edited"))
        second = self.store.claim()
        self.assertEqual((second["base_ref"], second["generation"]),
                         ("master", 3))

    def test_newer_head_is_not_lost_when_older_claim_completes(self):
        self.enqueue()
        old = self.store.claim()
        self.assertTrue(self.enqueue("b" * 40))
        self.assertFalse(self.store.is_current(old))
        self.assertIsNone(self.store.claim())
        self.assertTrue(self.store.complete(old))
        new = self.store.claim()
        self.assertEqual((new["head"], new["generation"]), ("b" * 40, 2))

    def test_force_requeues_completed_or_running_same_head_once(self):
        self.enqueue()
        first = self.store.claim()
        self.assertTrue(self.enqueue(force=True))
        self.assertFalse(self.enqueue(force=True))
        self.assertFalse(self.store.is_current(first))
        self.assertTrue(self.store.supersede(first))
        second = self.store.claim()
        self.assertEqual((second["head"], second["generation"],
                          second["force"]), ("a" * 40, 2, True))
        self.assertTrue(self.store.complete(second))
        self.assertFalse(self.enqueue())
        self.assertTrue(self.enqueue(force=True))
        self.assertEqual(self.store.claim()["generation"], 3)

    def test_recover_releases_claim_and_rejects_stale_token(self):
        self.enqueue()
        old = self.store.claim()
        restarted = jobs.JobStore(self.path)
        self.assertEqual(restarted.recover(), 1)
        new = restarted.claim()
        self.assertEqual(new["id"], old["id"])
        self.assertNotEqual(new["token"], old["token"])
        self.assertFalse(restarted.save_result(old, {"content": "old"}))
        self.assertFalse(restarted.complete(old))
        self.assertIsNone(restarted.retry(old, "stale"))
        self.assertTrue(restarted.complete(new))

    def test_recover_skips_older_claim_when_new_head_is_pending(self):
        self.enqueue()
        old = self.store.claim()
        self.enqueue("b" * 40)
        restarted = jobs.JobStore(self.path)
        self.assertEqual(restarted.recover(), 1)
        self.assertEqual(self.row(old)["status"], "superseded")
        self.assertEqual(restarted.claim()["head"], "b" * 40)

    def test_recover_releases_multiple_running_claims(self):
        self.enqueue()
        self.store.enqueue(43, "master", "b" * 40, "opened")
        first = self.store.claim()
        second = self.store.claim()
        self.assertEqual({first["number"], second["number"]}, {42, 43})
        result = {"content": "Saved review"}
        self.assertTrue(self.store.save_result(first, result))

        restarted = jobs.JobStore(self.path)
        self.assertEqual(restarted.recover(), 2)
        recovered = [restarted.claim(), restarted.claim()]
        self.assertEqual({job["number"] for job in recovered}, {42, 43})
        saved = next(job for job in recovered if job["number"] == 42)
        self.assertEqual(saved["review_result"], result)
        for old in (first, second):
            self.assertFalse(restarted.complete(old))

    def test_saved_review_result_survives_retry_and_restart(self):
        self.enqueue()
        first = self.store.claim()
        result = {"base_sha": "c" * 40, "content": "Review body"}
        self.assertTrue(self.store.save_result(first, result))
        restarted = jobs.JobStore(self.path, retry_base_seconds=0)
        self.assertEqual(restarted.recover(), 1)
        second = restarted.claim()
        self.assertEqual(second["review_result"], result)
        self.assertEqual(restarted.retry(second, "Forgejo unavailable"),
                         "retry")
        third = restarted.claim()
        self.assertEqual(third["review_result"], result)
        self.assertTrue(restarted.complete(third))

    def test_retry_delay_is_capped_and_attempts_end_in_failure(self):
        store = jobs.JobStore(self.path, max_attempts=3,
                              retry_base_seconds=30,
                              retry_max_seconds=45)
        self.enqueue()
        with patch.object(jobs.time, "time", return_value=1000):
            first = store.claim()
            self.assertEqual(store.retry(first, "temporary"), "retry")
            self.assertIsNone(store.claim())
        with patch.object(jobs.time, "time", return_value=1030):
            second = store.claim()
            self.assertEqual(second["attempts"], 1)
            self.assertEqual(store.retry(second, "still temporary"), "retry")
            self.assertIsNone(store.claim())
        with patch.object(jobs.time, "time", return_value=1074):
            self.assertIsNone(store.claim())
        with patch.object(jobs.time, "time", return_value=1075):
            third = store.claim()
            self.assertEqual(store.retry(third, "exhausted"), "failed")
            self.assertIsNone(store.claim())
        self.assertEqual((self.row(third)["status"],
                          self.row(third)["attempts"],
                          self.row(third)["error"]),
                         ("failed", 3, "exhausted"))

    def test_simultaneous_claims_get_distinct_jobs_once(self):
        self.enqueue()
        self.store.enqueue(43, "master", "b" * 40, "opened")
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.store.claim(), range(8)))
        self.assertEqual({claim["number"] for claim in claims if claim},
                         {42, 43})
        self.assertEqual(sum(claim is not None for claim in claims), 2)

    def test_newer_head_waits_while_another_pr_can_be_claimed(self):
        self.enqueue()
        first = self.store.claim()
        self.enqueue("b" * 40)
        self.store.enqueue(43, "master", "c" * 40, "opened")

        second = self.store.claim()
        self.assertEqual(second["number"], 43)
        self.assertIsNone(self.store.claim())
        self.assertTrue(self.store.supersede(first))
        self.assertEqual(self.store.claim()["head"], "b" * 40)

    def test_existing_global_index_is_replaced_without_losing_work(self):
        self.enqueue()
        running = self.store.claim()
        result = {"content": "Saved review"}
        self.assertTrue(self.store.save_result(running, result))
        self.store.enqueue(43, "master", "b" * 40, "opened")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP INDEX jobs_one_running_per_pr")
            db.execute("CREATE UNIQUE INDEX jobs_one_running "
                       "ON jobs(status) WHERE status = 'running'")
            db.commit()

        reopened = jobs.JobStore(self.path)
        self.assertEqual(reopened.claim()["number"], 43)
        self.assertEqual(self.row(running)["review_result"],
                         '{"content": "Saved review"}')
        with closing(sqlite3.connect(self.path)) as db:
            names = {row[1] for row in db.execute("PRAGMA index_list(jobs)")}
        self.assertIn("jobs_one_running_per_pr", names)
        self.assertNotIn("jobs_one_running", names)


if __name__ == "__main__":
    unittest.main()
