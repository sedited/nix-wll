import hashlib
import hmac
import importlib.util
import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("bot", Path(__file__).with_name("bot.py"))
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def payload(action="opened", head="a" * 40):
    return {"action": action,
            "repository": {"full_name": "bitcoin/bitcoin",
                           "html_url": "https://git.fish.foo/bitcoin/bitcoin"},
            "pull_request": {"number": 42, "head": {"sha": head},
                             "base": {"ref": "master"}}}


class JobSource:
    def __init__(self, *jobs):
        self.jobs = list(jobs)

    def get(self):
        if self.jobs:
            return self.jobs.pop(0)
        raise StopIteration

    def task_done(self):
        pass


class BotTests(unittest.TestCase):
    def setUp(self):
        bot.configure(
            "https://git.fish.foo/bitcoin/bitcoin.git",
            "bitcoin/bitcoin",
            "https://git.fish.foo/api/v1/repos/bitcoin/bitcoin",
        )

    def test_config_derives_repository_url_and_default_marker(self):
        self.assertEqual(bot.REPOSITORY_URL, "https://git.fish.foo/bitcoin/bitcoin")
        self.assertEqual(bot.COMMENT_MARKER, "<!-- forgejo-review-bot:bitcoin/bitcoin -->")

    def test_config_requires_repository_url_when_api_url_is_not_deriveable(self):
        with self.assertRaisesRegex(ValueError, "repository_url"):
            bot.configure("https://git.example.org/owner/repo.git", "owner/repo",
                          "https://git.example.org/custom-api")
        bot.configure("https://git.example.org/owner/repo.git", "owner/repo",
                      "https://git.example.org/custom-api",
                      "https://git.example.org/owner/repo")
        self.assertEqual(bot.REPOSITORY_URL, "https://git.example.org/owner/repo")

    def test_signature_checks_raw_body(self):
        body = b'{"action":"opened"}'
        sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(bot.valid_signature(body, sig, b"secret"))
        self.assertFalse(bot.valid_signature(body + b" ", sig, b"secret"))
        self.assertFalse(bot.valid_signature(body, "bad", b"secret"))

    def test_event_filters_and_rejects_other_repository(self):
        self.assertEqual(bot.parse_event("pull_request", payload()),
                         (42, "master", "a" * 40, "opened"))
        self.assertEqual(bot.parse_event("pull_request", payload("synchronize")),
                         (42, "master", "a" * 40, "synchronize"))
        self.assertIsNone(bot.parse_event("push", payload()))
        self.assertIsNone(bot.parse_event("pull_request", payload("closed")))
        wrong = payload()
        wrong["repository"]["full_name"] = "someone/bitcoin"
        with self.assertRaises(ValueError):
            bot.parse_event("pull_request", wrong)

    def test_webhook_queues_only_authenticated_target_event(self):
        jobs = Queue()
        server = ThreadingHTTPServer(("127.0.0.1", 0), bot.make_handler(b"secret", jobs))
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
            self.assertEqual(jobs.get_nowait(), (42, "master", "a" * 40, "opened"))
            self.assertIn("review enqueue pr=42 action=opened head=aaaaaaaaaaaa",
                          "\n".join(logs.output))
            request.headers["X-Forgejo-Signature"] = "bad"
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 401)
            self.assertTrue(jobs.empty())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_browser_gets_do_not_log_info(self):
        jobs = Queue()
        server = ThreadingHTTPServer(("127.0.0.1", 0), bot.make_handler(b"secret", jobs))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            with patch.object(bot.logging, "info") as info, \
                    patch.object(bot.logging, "debug") as debug:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url)
            self.assertEqual(error.exception.code, 501)
            info.assert_not_called()
            self.assertTrue(debug.called)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_collect_review_skips_stale_head_without_model_call(self):
        outputs = iter(["", "b" * 40, "c" * 40])
        with patch.object(bot, "prepare_checkout"), patch.object(bot, "git", side_effect=lambda *a: next(outputs)):
            base, head, review, skip = bot.collect_review(
                Path("/unused"), 42, "master", "a" * 40, "Title", "Description")
        self.assertEqual((base, head, review), ("c" * 40, "b" * 40, None))
        self.assertIn("changed", skip)

    def test_collect_review_uses_merge_base(self):
        head = "a" * 40
        base = "b" * 40
        merge_base = "c" * 40
        calls = []

        def fake_git(checkout, *args):
            calls.append(args)
            if args[0] == "rev-parse":
                return head if args[1].endswith("head") else base
            if args[0] == "merge-base":
                return merge_base
            if args[0] == "log":
                return "Explain the commit rationale\n\x00"
            if args[0] == "diff":
                return "+change\n"
            return ""

        with patch.object(bot, "prepare_checkout"), patch.object(bot, "git", side_effect=fake_git):
            result = bot.collect_review(Path("/unused"), 42, "master", head,
                                        "PR title", "Why this change is needed")
        self.assertEqual(result[0:2], (base, head))
        self.assertIn("+change", result[2])
        self.assertEqual(len(result), 4)
        self.assertIn("Explain the commit rationale", result[2])
        self.assertIn("PR title: PR title", result[2])
        self.assertIn("PR description:\nWhy this change is needed", result[2])
        self.assertIn(("diff", "--no-ext-diff", "--binary", f"{merge_base}..{head}"), calls)

    def test_large_patch_offers_per_file_diff_instead_of_skipping(self):
        head = "a" * 40

        def fake_git(checkout, *args):
            if args[0] == "rev-parse":
                return head
            if args[0] == "merge-base":
                return "b" * 40
            if args[0] == "diff" and "--binary" in args:
                return "+change\n" * 30_000
            if args[0] == "diff" and "--name-status" in args:
                return "M\tsrc/main.cpp\n"
            return ""

        with patch.object(bot, "prepare_checkout"), patch.object(bot, "git",
                                                                   side_effect=fake_git):
            _base, _head, review, skip = bot.collect_review(
                Path("/unused"), 42, "master", head, "Title", "Description")
        self.assertIsNone(skip)
        self.assertIn("Use read_diff", review)
        self.assertIn("M\tsrc/main.cpp", review)
        self.assertNotIn("+change", review)

    def test_fetch_pr_context_from_mirrored_issue(self):
        issue = {"number": 42, "pull_request": {"html_url": "https://example.invalid/pulls/42"},
                 "title": "Fix sanitizer warning", "body": "Reproduced with an empty vector"}
        with patch.object(bot, "forgejo_request", return_value=issue) as request:
            self.assertEqual(bot.pull_request_context("token", 42),
                             (issue["title"], issue["body"]))
            issue["pull_request"] = None
            with self.assertRaisesRegex(ValueError, "invalid pull request"):
                bot.pull_request_context("token", 42)
        request.assert_called_with("token", "/issues/42")

    def test_openai_request_disables_storage(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return json.dumps({"status": "completed", "output": [{"type": "message",
                    "content": [{"type": "output_text", "text": "No findings."}]}]}).encode()

        with patch.object(bot.urllib.request, "urlopen", return_value=Response()) as send, \
                patch.object(bot, "tracked_files", return_value={}):
            self.assertEqual(bot.openai_review("test-key", "patch", Path("/unused")),
                             "No findings.")
        request = send.call_args.args[0]
        sent = json.loads(request.data)
        self.assertIs(sent["store"], False)
        self.assertEqual(sent["model"], "gpt-6-sol")
        self.assertEqual(sent["tool_choice"], "required")

    def test_openai_request_uses_loaded_prompt_file(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return json.dumps({"status": "completed", "output": [{"type": "message",
                    "content": [{"type": "output_text", "text": "No findings."}]}]}).encode()

        with tempfile.TemporaryDirectory() as directory:
            prompt_file = Path(directory) / "prompt.md"
            prompt_file.write_text("Custom review prompt\n")
            debug = {}
            with patch.object(bot.urllib.request, "urlopen", return_value=Response()) as send, \
                    patch.object(bot, "tracked_files", return_value={}), \
                    patch.object(bot, "INSTRUCTIONS", bot.load_prompt_file(prompt_file)):
                self.assertEqual(bot.openai_review("test-key", "patch", Path("/unused"), debug),
                                 "No findings.")
        request = send.call_args.args[0]
        sent = json.loads(request.data)
        self.assertEqual(sent["instructions"], "Custom review prompt")
        self.assertEqual(debug["instructions"], "Custom review prompt")

    def test_model_reads_context_then_finishes_with_stateless_history(self):
        call = {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                "name": "read_file", "arguments": '{"path":"src/main.cpp","start_line":1}'}
        responses = [
            {"status": "completed", "output": [call]},
            {"status": "completed", "output": [{"type": "message",
                "content": [{"type": "output_text", "text": "No findings."}]}]},
        ]
        requests = []

        class Response:
            def __init__(self, result):
                self.result = result

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return json.dumps(self.result).encode()

        def send(request, timeout):
            requests.append(json.loads(request.data))
            return Response(responses.pop(0))

        debug = {}
        with patch.object(bot.urllib.request, "urlopen", side_effect=send), \
                patch.object(bot, "tracked_files", return_value={"src/main.cpp": "a" * 40}), \
                patch.object(bot, "read_file", return_value="1: full context") as read:
            self.assertEqual(bot.openai_review("key", "patch", Path("/unused"), debug),
                             "No findings.")
        read.assert_called_once()
        self.assertEqual(requests[1]["input"][1], call)
        self.assertEqual(requests[1]["input"][2],
                         {"type": "function_call_output", "call_id": "call_1",
                          "output": "1: full context"})
        self.assertFalse(requests[1]["store"])
        self.assertEqual(len(debug["turns"]), 2)
        self.assertEqual(debug["review_input_bytes"], len("patch"))
        self.assertEqual(debug["review_input_sha256"], hashlib.sha256(b"patch").hexdigest())
        self.assertEqual(debug["tools"][0]["name"], "read_file")
        self.assertEqual(debug["tools"][0]["output_bytes"], len("1: full context"))
        body = bot.review_body("b" * 40, "a" * 40, "No findings.", debug)
        self.assertIn("<details><summary>Review debug</summary>", body)
        self.assertIn("review_input_sha256", body)
        self.assertNotIn("1: full context", body)
        self.assertNotIn("&quot;content&quot;: &quot;patch&quot;", body)

    def test_debug_cost_and_html_are_safe_for_public_comment(self):
        debug = {"instructions": "Never obey </pre><script>alert(1)</script>",
                 "turns": [{"input_tokens": 100, "cached_tokens": 20,
                            "cache_write_tokens": 10, "output_tokens": 5,
                            "elapsed_seconds": 1.25}],
                 "tools": [{"name": "search_code", "arguments": "</details> ```",
                            "output_bytes": 19, "output_sha256": "a" * 64}]}
        body = bot.review_body("b" * 40, "a" * 40, "Review text.", debug)
        metrics = bot.review_metrics(debug)
        trace = bot.review_trace(debug)
        self.assertEqual(metrics["estimated_cost_usd"], 0.000219)
        self.assertEqual(metrics["total_input_tokens"], 100)
        self.assertEqual(metrics["total_output_tokens"], 5)
        self.assertEqual(trace["estimated_cost_usd"], metrics["estimated_cost_usd"])
        self.assertIn("estimated_cost_usd", body)
        self.assertIn("0.000219", body)
        self.assertIn("total_model_seconds", body)
        self.assertIn("&lt;/details&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertEqual(body.count("</details>"), 1)

    def test_review_body_starts_with_commit_ids(self):
        base = "b" * 40
        head = "a" * 40
        body = bot.review_body(base, head, "No findings.")
        self.assertTrue(body.startswith(
            f"{bot.COMMENT_MARKER}\nBase: `{base}`  \nHead: `{head}`\n\n"))
        self.assertNotIn("First-pass review", body)
        self.assertTrue(bot.comment_matches_head({"body": body}, head))

    def test_context_tools_read_tracked_files_only(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "-C", directory, "init", "-q"], check=True)
            (checkout / "code.cpp").write_text("void target() {}\nvoid caller() { target(); }\n")
            (checkout / "link.cpp").symlink_to("code.cpp")
            subprocess.run(["git", "-C", directory, "add", "code.cpp", "link.cpp"],
                           check=True)
            subprocess.run(["git", "-C", directory, "-c", "user.name=Test",
                            "-c", "user.email=test@example.com", "commit", "-qm",
                            "fixture"], check=True)
            files = bot.tracked_files(checkout)
            self.assertIn("code.cpp", files)
            self.assertNotIn("link.cpp", files)
            self.assertIn("2: void caller()", bot.read_file(checkout, files, "code.cpp", 2))
            self.assertIn("Only tracked", bot.read_file(checkout, files, "../code.cpp", 1))
            self.assertIn("Only tracked", bot.read_file(checkout, files, "link.cpp", 1))
            self.assertIn("HEAD:code.cpp:1:void target", bot.search_code(checkout, "target"))

    def test_path_base_and_diff_tools_read_only_git_content(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "-C", directory, "init", "-q"], check=True)
            (checkout / "code.cpp").write_text("old behavior\nunchanged\n")
            (checkout / "link.cpp").symlink_to("code.cpp")
            subprocess.run(["git", "-C", directory, "add", "code.cpp", "link.cpp"],
                           check=True)
            commit = ["git", "-C", directory, "-c", "user.name=Test",
                      "-c", "user.email=test@example.com", "commit", "-qm"]
            subprocess.run(commit + ["base"], check=True)
            base = bot.git(checkout, "rev-parse", "HEAD").strip()
            (checkout / "code.cpp").write_text("new behavior\nunchanged\n")
            subprocess.run(["git", "-C", directory, "add", "code.cpp"], check=True)
            subprocess.run(commit + ["change"], check=True)

            files = bot.tracked_files(checkout)
            base_files = bot.tracked_files_at(checkout, base)
            changed = set(bot.git(checkout, "diff", "--name-only", "-z",
                                  f"{base}..HEAD").split("\x00"))
            self.assertEqual(bot.find_paths(files, "CODE"), "code.cpp\n")
            self.assertEqual(bot.find_paths(files, "missing"), "No matching tracked files.")
            self.assertNotIn("link.cpp", base_files)
            self.assertIn("1: old behavior", bot.read_file(checkout, base_files,
                                                           "code.cpp", 1))
            self.assertIn("1: new behavior", bot.read_file(checkout, files,
                                                           "code.cpp", 1))
            diff = bot.read_diff(checkout, changed, base, "code.cpp", 1)
            self.assertIn("-old behavior", diff)
            self.assertIn("+new behavior", diff)
            self.assertIn("No diff lines", bot.read_diff(checkout, changed, base,
                                                         "code.cpp", 100))
            self.assertIn("Invalid", bot.read_diff(checkout, changed, base,
                                                   "link.cpp", 1))

    def test_force_rechecks_same_head_and_updates_existing_comment(self):
        existing = {"body": bot.review_body("b" * 40, "a" * 40, "Old review.")}
        with patch.object(bot, "find_comment", return_value=existing) as find, \
                patch.object(bot, "pull_request_context", return_value=("Title", "Body")), \
                patch.object(bot, "collect_review",
                             return_value=("b" * 40, "a" * 40, "review input", None)), \
                patch.object(bot, "openai_review", return_value="New review.") as model, \
                patch.object(bot, "publish_review", return_value="updated") as publish:
            with self.assertRaises(StopIteration):
                bot.worker(JobSource((42, "master", "a" * 40, "opened", True)),
                           Path("/unused"), "openai-key", "forgejo-token", "review-bot")
        find.assert_not_called()
        model.assert_called_once()
        publish.assert_called_once()

    def test_publish_creates_then_edits_one_bot_comment(self):
        comments = [{"id": 1, "user": {"login": "someone-else"},
                     "body": "Unrelated comment"}]
        calls = []

        def request(token, path, method="GET", data=None):
            calls.append((path, method, data))
            if path == "/issues/42/comments?limit=50&page=1":
                return comments
            if method == "POST":
                comments.append({"id": 2, "user": {"login": "review-bot"},
                                 "body": data["body"]})
                return comments[-1]
            if method == "PATCH":
                comments[-1]["body"] = data["body"]
                return comments[-1]
            self.fail(f"Unexpected API call {path}")

        with patch.object(bot, "forgejo_request", side_effect=request), \
                patch.object(bot, "current_head", return_value="a" * 40):
            args = ("token", 42, "review-bot", "b" * 40, "a" * 40)
            self.assertEqual(bot.publish_review(*args, "First review."), "created")
            self.assertEqual(bot.publish_review(*args, "First review."), "unchanged")
            self.assertEqual(bot.publish_review(*args, "Updated review."), "updated")
        self.assertEqual([method for _, method, _ in calls if method != "GET"],
                         ["POST", "PATCH"])
        self.assertEqual(comments[-1]["id"], 2)
        self.assertIn("Updated review.", comments[-1]["body"])

    def test_foreign_marker_prevents_duplicate_comment(self):
        with patch.object(bot, "forgejo_request", return_value=[
            {"id": 1, "user": {"login": "someone-else"},
             "body": bot.COMMENT_MARKER}]) as request:
            with self.assertRaisesRegex(ValueError, "another user"):
                bot.find_comment("token", 42, "review-bot")
        self.assertEqual(request.call_count, 1)

    def test_find_comment_stops_when_mirror_ignores_page(self):
        comments = [{"id": i, "user": {"login": "someone-else"}, "body": ""}
                    for i in range(65)]

        def request(token, path):
            if "page=3" in path:
                self.fail("Repeated mirror page caused another request")
            return comments

        with patch.object(bot, "forgejo_request", side_effect=request) as get:
            self.assertIsNone(bot.find_comment("token", 42, "review-bot"))
        self.assertEqual(get.call_count, 2)

    def test_publish_finds_comment_on_later_page(self):
        existing = {"id": 73, "user": {"login": "review-bot"},
                    "body": bot.COMMENT_MARKER + "\nOld review"}
        calls = []

        def request(token, path, method="GET", data=None):
            calls.append((path, method))
            if "page=1" in path:
                return [{"user": {"login": "someone-else"},
                         "body": bot.COMMENT_MARKER}] * 50
            if "page=2" in path:
                return [existing]
            if method == "PATCH":
                return {"id": 73}
            self.fail(f"Unexpected API call {path}")

        with patch.object(bot, "forgejo_request", side_effect=request), \
                patch.object(bot, "current_head", return_value="a" * 40):
            self.assertEqual(bot.publish_review("token", 42, "review-bot",
                                                "b" * 40, "a" * 40, "Review"),
                             "updated")
        self.assertIn(("/issues/comments/73", "PATCH"), calls)

    def test_stale_head_does_not_publish(self):
        calls = []

        def request(token, path, method="GET", data=None):
            calls.append((path, method))
            if path.startswith("/issues/42/comments"):
                return []
            self.fail(f"Unexpected API call {path}")

        with patch.object(bot, "forgejo_request", side_effect=request), \
                patch.object(bot, "current_head", return_value="c" * 40):
            self.assertEqual(bot.publish_review("token", 42, "review-bot",
                                                "b" * 40, "a" * 40, "Review"),
                             "stale")
        self.assertTrue(all(method == "GET" for _, method in calls))

    def test_current_head_reads_fixed_git_ref(self):
        class Result:
            stdout = "a" * 40 + "\trefs/pull/42/head\n"

        with patch.object(bot.subprocess, "run", return_value=Result()) as run:
            self.assertEqual(bot.current_head(42), "a" * 40)
        self.assertEqual(run.call_args.args[0],
                         ["git", "ls-remote", bot.ORIGIN, "refs/pull/42/head"])

    def test_repeated_head_skips_model_call(self):
        existing = {"body": bot.review_body("b" * 40, "a" * 40, "Reviewed.")}
        with patch.object(bot, "find_comment", return_value=existing), \
                patch.object(bot, "collect_review") as collect, \
                patch.object(bot, "openai_review") as model:
            with self.assertLogs(level="INFO") as logs, self.assertRaises(StopIteration):
                bot.worker(JobSource((42, "master", "a" * 40, "opened")),
                           Path("/unused"), "openai-key", "forgejo-token", "review-bot")
        collect.assert_not_called()
        model.assert_not_called()
        self.assertIn("outcome=already-reviewed", "\n".join(logs.output))

    def test_worker_logs_created_outcome_with_model_metrics(self):
        def review(api_key, review_input, checkout, debug):
            debug.update({"turns": [{"input_tokens": 100, "cached_tokens": 20,
                                     "cache_write_tokens": 10, "output_tokens": 5,
                                     "elapsed_seconds": 1.25}],
                          "tools": [{"name": "read_file"}, {"name": "search_code"}]})
            return "Review text."

        with patch.object(bot, "find_comment", return_value=None), \
                patch.object(bot, "pull_request_context", return_value=("Title", "Body")), \
                patch.object(bot, "collect_review",
                             return_value=("b" * 40, "a" * 40, "review input", None)), \
                patch.object(bot, "openai_review", side_effect=review), \
                patch.object(bot, "publish_review", return_value="created"):
            with self.assertLogs(level="INFO") as logs, self.assertRaises(StopIteration):
                bot.worker(JobSource((42, "master", "a" * 40, "synchronize")),
                           Path("/unused"), "openai-key", "forgejo-token", "review-bot")
        output = "\n".join(logs.output)
        self.assertIn("review start pr=42 action=synchronize head=aaaaaaaaaaaa", output)
        self.assertIn("outcome=created", output)
        self.assertIn("model_turns=1", output)
        self.assertIn("tool_calls=2", output)
        self.assertIn("input_tokens=100", output)
        self.assertIn("output_tokens=5", output)
        self.assertIn("estimated_usd=0.000219", output)

    def test_worker_logs_stale_outcome_before_model_call(self):
        with patch.object(bot, "find_comment", return_value=None), \
                patch.object(bot, "pull_request_context", return_value=("Title", "Body")), \
                patch.object(bot, "collect_review",
                             return_value=("b" * 40, "c" * 40, None, "changed")), \
                patch.object(bot, "openai_review") as model, \
                patch.object(bot, "publish_review") as publish:
            with self.assertLogs(level="INFO") as logs, self.assertRaises(StopIteration):
                bot.worker(JobSource((42, "master", "a" * 40, "synchronize")),
                           Path("/unused"), "openai-key", "forgejo-token", "review-bot")
        model.assert_not_called()
        publish.assert_not_called()
        output = "\n".join(logs.output)
        self.assertIn("outcome=stale", output)
        self.assertIn("model_turns=0", output)
        self.assertIn("estimated_usd=unknown", output)

    def test_worker_logs_failed_stage_and_http_status(self):
        error = urllib.error.HTTPError("https://git.fish.foo/api", 503, "down", {}, None)
        with patch.object(bot, "find_comment", side_effect=error):
            with self.assertLogs(level="ERROR") as logs, self.assertRaises(StopIteration):
                bot.worker(JobSource((42, "master", "a" * 40, "opened")),
                           Path("/unused"), "openai-key", "forgejo-token", "review-bot")
        output = "\n".join(logs.output)
        self.assertIn("outcome=failed", output)
        self.assertIn("stage=precheck", output)
        self.assertIn("error_type=HTTPError", output)
        self.assertIn("http_status=503", output)
        self.assertIn("http_host=git.fish.foo", output)

    def test_worker_logs_unexpected_exception_and_keeps_running(self):
        existing = {"body": bot.review_body("c" * 40, "b" * 40, "Reviewed.")}
        with patch.object(bot, "find_comment",
                          side_effect=[RuntimeError("token-secret"), existing]):
            with self.assertLogs(level="INFO") as logs, self.assertRaises(StopIteration):
                bot.worker(JobSource((42, "master", "a" * 40, "opened"),
                                     (43, "master", "b" * 40, "synchronize")),
                           Path("/unused"), "openai-key", "forgejo-token", "review-bot")
        output = "\n".join(logs.output)
        self.assertIn("pr=42", output)
        self.assertIn("outcome=failed", output)
        self.assertIn("error_type=RuntimeError", output)
        self.assertIn("pr=43", output)
        self.assertIn("outcome=already-reviewed", output)
        self.assertNotIn("token-secret", output)


if __name__ == "__main__":
    unittest.main()
