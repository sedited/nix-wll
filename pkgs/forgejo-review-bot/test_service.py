import hashlib
import hmac
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


from forgejo_review_bot import config, forgejo, model, pipeline, repository, service, spend
from forgejo_review_bot.jobs import JobStore


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_dir = Path(self.directory.name)
        self.jobs = JobStore(self.state_dir / "jobs.sqlite3",
                             retry_base_seconds=0)
        self.ledger = spend.Ledger(self.state_dir / "spend.sqlite3")
        self.bot_config = config.BotConfig(
            "https://git.example.org/owner/repo.git", "owner/repo",
            "https://git.example.org/api/v1/repos/owner/repo")
        self.prompt_config = config.PromptConfig("instructions", {}, {
            name: "gpt-6-luna" for name in config.MODEL_NAMES})

    def enqueue(self, head="a" * 40):
        self.jobs.enqueue(42, "master", head, "synchronize")
        return self.jobs.claim()

    def process(self, job):
        return service.process_job(job, self.jobs, self.state_dir, "api-key",
                                   "forgejo-token", "review-bot",
                                   self.bot_config, self.prompt_config,
                                   self.ledger)

    def post_webhook(self, store):
        secret = b"secret"
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            service.make_handler(secret, store, self.bot_config))
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            body = json.dumps({
                "action": "opened",
                "repository": {"full_name": "owner/repo",
                               "html_url": "https://git.example.org/owner/repo"},
                "pull_request": {"number": 42,
                                 "base": {"ref": "master"},
                                 "head": {"sha": "a" * 40}},
            }).encode()
            signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("POST", "/webhooks/forgejo", body,
                               {"Content-Type": "application/json",
                                "X-Forgejo-Event": "pull_request",
                                "X-Forgejo-Signature": "sha256=" + signature})
            response = connection.getresponse()
            response.read()
            connection.close()
            return response.status
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_webhook_202_means_job_is_durable(self):
        self.assertEqual(self.post_webhook(self.jobs), 202)
        restarted = JobStore(self.state_dir / "jobs.sqlite3")
        job = restarted.claim()
        self.assertEqual((job["number"], job["head"]), (42, "a" * 40))

    def test_webhook_rejects_failed_persistence(self):
        with patch.object(self.jobs, "enqueue", side_effect=sqlite3.OperationalError("full")):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.post_webhook(self.jobs), 503)
        self.assertIsNone(self.jobs.claim())

    def test_distinct_pr_reviews_overlap_outside_git_lock(self):
        (self.state_dir / "checkout" / ".git").mkdir(parents=True)
        for number in (42, 43):
            self.jobs.enqueue(number, "master", "a" * 40, "synchronize")
        claimed = [self.jobs.claim(), self.jobs.claim()]
        barrier = threading.Barrier(2)
        prefixes = []

        def collect(*args, ref_prefix):
            self.assertTrue(service.CHECKOUT_LOCK.locked())
            prefixes.append(ref_prefix)
            return "b" * 40, "a" * 40, "review input", None

        def snapshot(*args):
            self.assertTrue(service.CHECKOUT_LOCK.locked())
            return object()

        def review(*args, **kwargs):
            barrier.wait(timeout=5)
            return "Review content"

        with (patch.object(service.repository, "current_head", return_value="a" * 40),
              patch.object(service.forgejo, "find_comment", return_value=None),
              patch.object(service.forgejo, "pull_request_context",
                           return_value=("Title", "Description")),
              patch.object(service.repository, "collect_review", side_effect=collect),
              patch.object(service.repository, "snapshot_repository", side_effect=snapshot),
              patch.object(service.pipeline, "review_with_independent_passes",
                           side_effect=review),
              patch.object(service.forgejo, "publish_review",
                           return_value="published") as publish,
              patch.object(service.repository, "release_review_refs") as release,
              ThreadPoolExecutor(max_workers=2) as pool):
            self.assertEqual(list(pool.map(self.process, claimed)),
                             ["published", "published"])
        self.assertEqual(len(set(prefixes)), 2)
        self.assertEqual({call.args[1] for call in release.call_args_list}, set(prefixes))
        self.assertEqual({call.args[3] for call in publish.call_args_list}, {42, 43})
        self.assertIsNone(self.jobs.claim())

    def test_publication_retry_reuses_saved_result(self):
        job = self.enqueue()
        with (patch.object(service.repository, "current_head", return_value="a" * 40),
              patch.object(service.forgejo, "find_comment", return_value=None),
              patch.object(service.forgejo, "pull_request_context",
                           return_value=("Title", "Description")),
              patch.object(service.repository, "collect_review",
                           return_value=("b" * 40, "a" * 40, "review input", None)),
              patch.object(service.repository, "snapshot_repository",
                           return_value=object()),
              patch.object(service.pipeline, "review_with_independent_passes",
                           return_value="Review content") as review,
              patch.object(service.forgejo, "publish_review",
                           side_effect=urllib.error.URLError("connection lost"))):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.process(job), "retry")
            self.assertEqual(review.call_count, 1)
            self.assertEqual(review.call_args.kwargs["budget"].review_id,
                             f'pr:42:{job["id"]}:{job["generation"]}')
            self.assertEqual(review.call_args.kwargs["routing_mode"], "enabled")

        restarted = JobStore(self.state_dir / "jobs.sqlite3",
                             retry_base_seconds=0)
        retry_job = restarted.claim()
        self.assertEqual(retry_job["review_result"]["content"], "Review content")
        self.jobs = restarted
        with (patch.object(service.repository, "current_head", return_value="a" * 40),
              patch.object(service.pipeline, "review_with_independent_passes",
                           side_effect=AssertionError("model called again")),
              patch.object(service.forgejo, "publish_review",
                           return_value="unchanged") as publish):
            self.assertEqual(self.process(retry_job), "unchanged")
            self.assertEqual(publish.call_count, 1)
        self.assertIsNone(restarted.claim())

    def test_report_exists_before_publication_and_retry_reuses_url(self):
        self.bot_config = replace(self.bot_config,
                                  report_dir=self.state_dir / "public",
                                  report_base_url="https://review.example.org/traces")
        job = self.enqueue()
        # A durable result is enough to retry publication without another model call.
        job["review_result"] = {
            "base_sha": "b" * 40, "head_sha": "a" * 40,
            "content": "Review content", "debug": {}}
        self.jobs.save_result(job, job["review_result"])
        urls = []

        def publish(*args):
            debug = args[-1]
            urls.append(debug["report_url"])
            filename = urls[-1].rsplit("/", 1)[-1]
            self.assertTrue((self.bot_config.report_dir / filename).is_file())
            if len(urls) == 1:
                raise urllib.error.URLError("connection lost")
            return "published"

        with (patch.object(service.repository, "current_head", return_value="a" * 40),
              patch.object(service.forgejo, "publish_review", side_effect=publish)):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.process(job), "retry")
            retry_job = self.jobs.claim()
            self.assertEqual(self.process(retry_job), "published")
        self.assertEqual(urls[0], urls[1])

    def test_report_failure_publishes_review_without_broken_link(self):
        self.bot_config = replace(self.bot_config,
                                  report_dir=self.state_dir / "public",
                                  report_base_url="https://review.example.org/traces")
        job = self.enqueue()
        job["review_result"] = {"base_sha": "b" * 40, "head_sha": "a" * 40,
                                "content": "Review content", "debug": {}}
        with (patch.object(service.repository, "current_head", return_value="a" * 40),
              patch.object(service.report, "save_report", side_effect=OSError("disk full")),
              patch.object(service.forgejo, "publish_review", return_value="published") as publish):
            with self.assertLogs(level="WARNING"):
                self.assertEqual(self.process(job), "published")
        self.assertNotIn("report_url", publish.call_args.args[-1])

    def test_stale_review_keeps_newer_pending_head(self):
        old = self.enqueue()

        def stale_review(*_args, **_kwargs):
            self.jobs.enqueue(42, "master", "b" * 40, "synchronize")
            raise model.StaleReview("new head")

        with (patch.object(service.repository, "current_head", return_value="a" * 40),
              patch.object(service.forgejo, "find_comment", return_value=None),
              patch.object(service.forgejo, "pull_request_context",
                           return_value=("Title", "Description")),
              patch.object(service.repository, "collect_review",
                           return_value=("c" * 40, "a" * 40, "input", None)),
              patch.object(service.repository, "snapshot_repository",
                           return_value=object()),
              patch.object(service.pipeline, "review_with_independent_passes",
                           side_effect=stale_review)):
            self.assertEqual(self.process(old), "stale")
        self.assertEqual(self.jobs.claim()["head"], "b" * 40)

    def test_same_head_on_new_base_is_reviewed(self):
        job = self.enqueue()
        old_comment = {"body": ("<!-- forgejo-review-bot:owner/repo -->\n"
                                f"Base: `{'b' * 40}`  \nHead: `{'a' * 40}`\n")}
        with (patch.object(service.repository, "current_head", return_value="a" * 40),
              patch.object(service.forgejo, "find_comment", return_value=old_comment),
              patch.object(service.forgejo, "pull_request_context",
                           return_value=("Title", "Description")),
              patch.object(service.repository, "collect_review",
                           return_value=("c" * 40, "a" * 40, "input", None)),
              patch.object(service.repository, "snapshot_repository",
                           return_value=object()),
              patch.object(service.pipeline, "review_with_independent_passes",
                           return_value="New base review") as review,
              patch.object(service.forgejo, "publish_review",
                           return_value="updated")):
            self.assertEqual(self.process(job), "updated")
        self.assertEqual(review.call_count, 1)

    def test_fetch_head_change_is_queued_even_if_head_lookup_lags(self):
        old = self.enqueue()
        with (patch.object(service.repository, "current_head", return_value="a" * 40),
              patch.object(service.forgejo, "find_comment", return_value=None),
              patch.object(service.forgejo, "pull_request_context",
                           return_value=("Title", "Description")),
              patch.object(service.repository, "collect_review",
                           return_value=("c" * 40, "b" * 40, None,
                                         "PR head changed before review"))):
            self.assertEqual(self.process(old), "stale")
        self.assertEqual(self.jobs.claim()["head"], "b" * 40)

    def test_fetch_head_change_does_not_replace_newer_pending_job(self):
        old = self.enqueue()

        def collect(*_args, **_kwargs):
            self.jobs.enqueue(42, "master", "c" * 40, "synchronize")
            return "d" * 40, "b" * 40, None, "PR head changed before review"

        with (patch.object(service.repository, "current_head", return_value="a" * 40),
              patch.object(service.forgejo, "find_comment", return_value=None),
              patch.object(service.forgejo, "pull_request_context",
                           return_value=("Title", "Description")),
              patch.object(service.repository, "collect_review", side_effect=collect)):
            self.assertEqual(self.process(old), "stale")
        self.assertEqual(self.jobs.claim()["head"], "c" * 40)

    def test_stale_requeue_timeout_retries_and_then_completes(self):
        old = self.enqueue()
        with (patch.object(service.repository, "current_head",
                           side_effect=["a" * 40, TimeoutError("git timeout")]),
              patch.object(service.forgejo, "find_comment", return_value=None),
              patch.object(service.forgejo, "pull_request_context",
                           return_value=("Title", "Description")),
              patch.object(service.repository, "collect_review",
                           return_value=("c" * 40, "a" * 40, "input", None)),
              patch.object(service.repository, "snapshot_repository",
                           return_value=object()),
              patch.object(service.pipeline, "review_with_independent_passes",
                           side_effect=model.StaleReview("head changed"))):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.process(old), "retry")
        retry = self.jobs.claim()
        self.assertEqual((retry["id"], retry["attempts"]), (old["id"], 1))
        with (patch.object(service.repository, "current_head", return_value="a" * 40),
              patch.object(service.forgejo, "find_comment", return_value=None),
              patch.object(service.forgejo, "pull_request_context",
                           return_value=("Title", "Description")),
              patch.object(service.repository, "collect_review",
                           return_value=("c" * 40, "a" * 40, "input", None)),
              patch.object(service.repository, "snapshot_repository",
                           return_value=object()),
              patch.object(service.pipeline, "review_with_independent_passes",
                           return_value="Review content"),
              patch.object(service.forgejo, "publish_review", return_value="updated")):
            self.assertEqual(self.process(retry), "updated")
        self.assertIsNone(self.jobs.claim())

    def test_permanent_http_error_fails_and_transient_error_retries(self):
        self.assertFalse(service.retryable_error(urllib.error.HTTPError(
            "https://git.example.org", 400, "bad", {}, None)))
        self.assertTrue(service.retryable_error(urllib.error.HTTPError(
            "https://git.example.org", 429, "rate limit", {}, None)))
        self.assertTrue(service.retryable_error(urllib.error.HTTPError(
            "https://git.example.org", 503, "unavailable", {}, None)))
        self.assertTrue(service.retryable_error(TimeoutError("timeout")))

        job = self.enqueue()
        error = urllib.error.HTTPError("https://git.example.org", 400,
                                      "bad", {}, None)
        with (patch.object(service.repository, "current_head", return_value="a" * 40),
              patch.object(service.forgejo, "find_comment", side_effect=error)):
            with self.assertLogs(level="ERROR"):
                self.assertEqual(self.process(job), "failed")
        self.assertIsNone(self.jobs.claim())
        with closing(sqlite3.connect(self.state_dir / "jobs.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE id=?",
                                        (job["id"],)).fetchone()[0], "failed")

    def test_worker_can_stop_after_claiming_one_job(self):
        self.jobs.enqueue(42, "master", "a" * 40, "opened")
        stop_event = threading.Event()

        def process(job, *_args):
            self.assertEqual(job["number"], 42)
            stop_event.set()

        with patch.object(service, "process_job", side_effect=process) as called:
            service.worker(self.jobs, self.state_dir, "api-key", "forgejo-token",
                           "review-bot", self.bot_config, self.prompt_config,
                           self.ledger, stop_event=stop_event)
        self.assertEqual(called.call_count, 1)

    def test_worker_waits_after_claim_database_error(self):
        stop_event = threading.Event()

        def stop_after_wait(_seconds):
            stop_event.set()

        with (patch.object(self.jobs, "claim",
                           side_effect=sqlite3.OperationalError("database locked")) as claim,
              patch.object(stop_event, "wait", side_effect=stop_after_wait) as wait,
              self.assertLogs(level="ERROR")):
            service.worker(self.jobs, self.state_dir, "api-key", "forgejo-token",
                           "review-bot", self.bot_config, self.prompt_config,
                           self.ledger, stop_event=stop_event)
        self.assertEqual(claim.call_count, 1)
        wait.assert_called_once_with(1)

    def test_worker_survives_unexpected_processing_error(self):
        self.jobs.enqueue(42, "master", "a" * 40, "opened")
        stop_event = threading.Event()
        calls = []

        def process(job, *_args):
            calls.append(job)
            if len(calls) == 1:
                raise TimeoutError("worker interrupted")
            self.jobs.complete(job)
            stop_event.set()

        with (patch.object(service, "process_job", side_effect=process),
              patch.object(stop_event, "wait", return_value=None),
              self.assertLogs(level="ERROR")):
            service.worker(self.jobs, self.state_dir, "api-key", "forgejo-token",
                           "review-bot", self.bot_config, self.prompt_config,
                           self.ledger, stop_event=stop_event)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["attempts"], 1)
        self.assertIsNone(self.jobs.claim())




def payload(action="opened", head="a" * 40):
    return {"action": action,
            "repository": {"full_name": "bitcoin/bitcoin",
                           "html_url": "https://git.fish.foo/bitcoin/bitcoin"},
            "pull_request": {"number": 42, "head": {"sha": head},
                             "base": {"ref": "master"}}}


class CapturingJobs:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, *job):
        self.enqueued.append(job)
        return True


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.bot_config = config.BotConfig(
            "https://git.fish.foo/bitcoin/bitcoin.git",
            "bitcoin/bitcoin",
            "https://git.fish.foo/api/v1/repos/bitcoin/bitcoin",
        )
        self.prompt_config = config.PromptConfig.load()

    def snapshot(self, files=None, checkout=Path("/unused")):
        return repository.RepositorySnapshot(
            checkout, "b" * 40, "a" * 40, "b" * 40,
            files or {}, {}, frozenset())

    def queued_job(self, *, force=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state_dir = Path(directory.name)
        jobs = JobStore(state_dir / "jobs.sqlite3")
        jobs.enqueue(42, "master", "a" * 40, "opened", force)
        ledger = spend.Ledger(state_dir / "spend.sqlite3")
        return jobs.claim(), jobs, state_dir, ledger

    def test_signature_checks_raw_body(self):
        body = b'{"action":"opened"}'
        sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(service.valid_signature(body, sig, b"secret"))
        self.assertFalse(service.valid_signature(body + b" ", sig, b"secret"))
        self.assertFalse(service.valid_signature(body, "bad", b"secret"))

    def test_event_filters_and_rejects_other_repository(self):
        self.assertEqual(service.parse_event(self.bot_config, "pull_request", payload()),
                         (42, "master", "a" * 40, "opened", False))
        self.assertEqual(service.parse_event(self.bot_config, "pull_request", payload("synchronize")),
                         (42, "master", "a" * 40, "synchronize", False))
        forced = payload()
        forced["review_bot_force"] = True
        self.assertEqual(service.parse_event(self.bot_config, "pull_request", forced)[-1], True)
        self.assertIsNone(service.parse_event(self.bot_config, "push", payload()))
        self.assertIsNone(service.parse_event(self.bot_config, "pull_request", payload("closed")))
        wrong = payload()
        wrong["repository"]["full_name"] = "someone/bitcoin"
        with self.assertRaises(ValueError):
            service.parse_event(self.bot_config, "pull_request", wrong)

    def test_webhook_queues_only_authenticated_target_event(self):
        jobs = CapturingJobs()
        server = ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(b"secret", jobs, self.bot_config))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            body = json.dumps(payload()).encode()
            sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
            url = f"http://127.0.0.1:{server.server_port}/webhooks/forgejo"
            request = urllib.request.Request(url, body, headers={
                "X-Forgejo-Signature": sig, "X-Forgejo-Event": "pull_request"})
            with self.assertLogs(level="INFO") as logs:
                self.assertEqual(urllib.request.urlopen(request).status, 202)
            self.assertEqual(jobs.enqueued[0],
                             (42, "master", "a" * 40, "opened", False))
            self.assertIn("review enqueue pr=42 action=opened head=aaaaaaaaaaaa",
                          "\n".join(logs.output))
            forced = payload()
            forced["review_bot_force"] = True
            body = json.dumps(forced).encode()
            sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
            request = urllib.request.Request(url, body, headers={
                "X-Forgejo-Signature": sig, "X-Forgejo-Event": "pull_request"})
            self.assertEqual(urllib.request.urlopen(request).status, 202)
            self.assertEqual(jobs.enqueued[1],
                             (42, "master", "a" * 40, "opened", True))
            request.headers["X-Forgejo-Signature"] = "bad"
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 401)
            self.assertEqual(len(jobs.enqueued), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_wrong_path_log_redacts_query_and_identifies_non_webhook(self):
        jobs = CapturingJobs()
        server = ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(b"secret", jobs, self.bot_config))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            body = b"{}"
            url = f"http://127.0.0.1:{server.server_port}/?api_key=do-not-log"
            request = urllib.request.Request(url, body, headers={"X-Next-Action": "probe"})
            with self.assertLogs(level="WARNING") as logs:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 404)
            output = "\n".join(logs.output)
            self.assertIn("reason=unexpected_path path=/", output)
            self.assertIn("signature_present=False", output)
            self.assertNotIn("do-not-log", output)
            self.assertEqual(jobs.enqueued, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_browser_gets_do_not_log_info(self):
        jobs = CapturingJobs()
        server = ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(b"secret", jobs, self.bot_config))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/?api_key=do-not-log"
            with patch.object(service.logging, "info") as info, \
                    patch.object(service.logging, "debug") as debug:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url)
            self.assertEqual(error.exception.code, 501)
            info.assert_not_called()
            self.assertTrue(debug.called)
            self.assertNotIn("do-not-log", str(debug.call_args))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_force_rechecks_same_head_and_updates_existing_comment(self):
        job, jobs, state_dir, ledger = self.queued_job(force=True)
        existing = {"body": forgejo.review_body(self.bot_config, self.prompt_config, "b" * 40, "a" * 40, "Old review.")}
        with patch.object(repository, "current_head", return_value="a" * 40), \
                patch.object(forgejo, "find_comment", return_value=existing) as find, \
                patch.object(forgejo, "pull_request_context", return_value=("Title", "Body")), \
                patch.object(repository, "collect_review",
                             return_value=("b" * 40, "a" * 40, "review input", None)), \
                patch.object(repository, "snapshot_repository", return_value=self.snapshot()), \
                patch.object(pipeline, "review_with_independent_passes",
                             return_value="New review.") as review_model, \
                patch.object(forgejo, "publish_review", return_value="updated") as publish:
            self.assertEqual(service.process_job(
                job, jobs, state_dir, "openai-key", "forgejo-token", "review-bot",
                self.bot_config, self.prompt_config, ledger), "updated")
        find.assert_not_called()
        review_model.assert_called_once()
        self.assertEqual(review_model.call_args.args[1], "review input")
        publish.assert_called_once()

    def test_repeated_head_skips_model_call(self):
        job, jobs, state_dir, ledger = self.queued_job()
        existing = {"body": forgejo.review_body(self.bot_config, self.prompt_config, "b" * 40, "a" * 40, "Reviewed.")}
        with patch.object(repository, "current_head", return_value="a" * 40), \
                patch.object(forgejo, "find_comment", return_value=existing), \
                patch.object(forgejo, "pull_request_context",
                             return_value=("Title", "Body")), \
                patch.object(repository, "collect_review",
                             return_value=("b" * 40, "a" * 40, "review input", None)) as collect, \
                patch.object(repository, "snapshot_repository") as snapshot, \
                patch.object(pipeline, "review_with_independent_passes") as review_model:
            self.assertEqual(service.process_job(
                job, jobs, state_dir, "openai-key", "forgejo-token", "review-bot",
                self.bot_config, self.prompt_config, ledger), "already-reviewed")
        collect.assert_called_once()
        snapshot.assert_not_called()
        review_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
