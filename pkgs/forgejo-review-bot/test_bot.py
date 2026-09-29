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

    def put(self, _job):
        # This source supplies only the scripted jobs above.
        pass

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

    def test_model_config_requires_adversarial_stage_and_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory)
            for name in ("common", "router", "adversarial", *bot.AUDIT_NAMES,
                         "verifier", "collator"):
                (audit_dir / f"{name}.md").write_text(f"{name} instructions\n")
            models = {name: "gpt-6.1-sol" if name in
                      ("independent", "adversarial") else "gpt-6-luna"
                      for name in bot.MODEL_NAMES}
            (audit_dir / "models.json").write_text(json.dumps(models))
            with patch.object(bot, "AUDIT_PROMPTS", None), \
                    patch.object(bot, "MODELS", None):
                bot.configure_audit_prompts(audit_dir)
                self.assertEqual(bot.audit_prompts()["adversarial"],
                                 "adversarial instructions")
                self.assertEqual(bot.stage_models()["adversarial"], "gpt-6.1-sol")
                self.assertEqual(bot.stage_models()["verifier"], "gpt-6-luna")
                del models["adversarial"]
                (audit_dir / "models.json").write_text(json.dumps(models))
                with self.assertRaisesRegex(ValueError, "each review stage"):
                    bot.configure_audit_prompts(audit_dir)

    def test_signature_checks_raw_body(self):
        body = b'{"action":"opened"}'
        sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(bot.valid_signature(body, sig, b"secret"))
        self.assertFalse(bot.valid_signature(body + b" ", sig, b"secret"))
        self.assertFalse(bot.valid_signature(body, "bad", b"secret"))

    def test_event_filters_and_rejects_other_repository(self):
        self.assertEqual(bot.parse_event("pull_request", payload()),
                         (42, "master", "a" * 40, "opened", False))
        self.assertEqual(bot.parse_event("pull_request", payload("synchronize")),
                         (42, "master", "a" * 40, "synchronize", False))
        forced = payload()
        forced["review_bot_force"] = True
        self.assertEqual(bot.parse_event("pull_request", forced)[-1], True)
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
            self.assertEqual(jobs.get_nowait(), (42, "master", "a" * 40, "opened", False))
            self.assertIn("review enqueue pr=42 action=opened head=aaaaaaaaaaaa",
                          "\n".join(logs.output))
            forced = payload()
            forced["review_bot_force"] = True
            body = json.dumps(forced).encode()
            sig = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
            request = urllib.request.Request(url, body, headers={
                "X-Forgejo-Signature": sig, "X-Forgejo-Event": "pull_request"})
            self.assertEqual(urllib.request.urlopen(request).status, 202)
            self.assertEqual(jobs.get_nowait(), (42, "master", "a" * 40, "opened", True))
            request.headers["X-Forgejo-Signature"] = "bad"
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 401)
            self.assertTrue(jobs.empty())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_wrong_path_log_redacts_query_and_identifies_non_webhook(self):
        jobs = Queue()
        server = ThreadingHTTPServer(("127.0.0.1", 0), bot.make_handler(b"secret", jobs))
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
            url = f"http://127.0.0.1:{server.server_port}/?api_key=do-not-log"
            with patch.object(bot.logging, "info") as info, \
                    patch.object(bot.logging, "debug") as debug:
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
        self.assertIn("Inspect the checkout diff", review)
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

    def test_codex_exec_preserves_tools_and_hides_api_key(self):
        events = [
            {"type": "item.completed", "item": {"type": "command_execution",
                "status": "completed", "command": "git status"}},
            {"type": "item.completed", "item": {"type": "agent_message",
                "text": "No findings."}},
            {"type": "turn.completed", "usage": {"input_tokens": 100,
                "cached_input_tokens": 40, "cache_write_input_tokens": 7,
                "output_tokens": 20, "reasoning_output_tokens": 5}},
        ]
        result = subprocess.CompletedProcess([], 0,
            "\n".join(json.dumps(event) for event in events), "")
        with patch.object(bot.subprocess, "run", return_value=result) as run:
            answer, turn, tools = bot.codex_review(
                "secret", "gpt-6-luna", "Review safely", "PR patch", Path("/checkout"))
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["codex", "exec", "--json", "--ephemeral"])
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("/checkout", command)
        self.assertIn("project_doc_max_bytes=0", command)
        self.assertIn('shell_environment_policy.filters.CODEX_API_KEY="exclude"', command)
        self.assertIn('developer_instructions="Review safely"', command)
        self.assertEqual(run.call_args.kwargs["input"], "PR patch")
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_API_KEY"], "secret")
        self.assertFalse(Path(run.call_args.kwargs["env"]["CODEX_HOME"]).exists())
        self.assertNotIn("secret", str(command))
        self.assertEqual(answer, "No findings.")
        self.assertEqual(turn["cached_tokens"], 40)
        self.assertEqual(turn["cache_write_tokens"], 7)
        self.assertEqual(turn["reasoning_tokens"], 5)
        self.assertEqual(len(tools), 1)

    def test_codex_logs_failed_tools_without_command_output(self):
        events = [
            {"type": "item.completed", "item": {"type": "command_execution",
                "status": "failed", "exit_code": 1, "command": "secret command",
                "aggregated_output": "secret output"}},
            {"type": "item.completed", "item": {"type": "agent_message",
                "text": "Review incomplete."}},
            {"type": "turn.completed", "usage": {}},
        ]
        result = subprocess.CompletedProcess([], 0,
            "\n".join(json.dumps(event) for event in events), "")
        with patch.object(bot.subprocess, "run", return_value=result), \
                self.assertLogs(level="INFO") as logs:
            bot.codex_review("key", "gpt-6-luna", "prompt", "patch",
                             Path("/checkout"), stage="verifier")
        entries = "\n".join(logs.output)
        self.assertIn("codex stage start stage=verifier", entries)
        self.assertIn("codex tool stage=verifier name=command_execution status=failed exit_code=1",
                      entries)
        self.assertIn("codex stage complete stage=verifier", entries)
        self.assertNotIn("secret command", entries)
        self.assertNotIn("secret output", entries)

    def test_codex_requires_completed_nonempty_output(self):
        for events, code in [([{"type": "item.completed", "item":
                {"type": "agent_message", "text": "partial"}}], 0),
                ([{"type": "turn.completed", "usage": {}}], 0),
                ([{"type": "turn.failed"}], 0),
                ([], 1)]:
            with self.subTest(events=events, code=code):
                result = subprocess.CompletedProcess([], code,
                    "\n".join(json.dumps(event) for event in events), "failure")
                with patch.object(bot.subprocess, "run", return_value=result):
                    with self.assertRaises(ValueError):
                        bot.codex_review("key", "gpt-6-luna", "prompt", "patch",
                                         Path("/checkout"))

    def test_non_code_audit_has_no_checkout_access(self):
        turn = {"input_tokens": 100, "cached_tokens": 0,
                "cache_write_tokens": None, "output_tokens": 20,
                "elapsed_seconds": 1.0}
        with patch.object(bot, "codex_review", return_value=("Candidate", turn, [])) as run:
            answer, record = bot.run_audit("key", "collator", "Edit", "Reviews")
        self.assertEqual(answer, "Candidate")
        self.assertEqual(record["input_tokens"], 100)
        checkout = run.call_args.args[-1]
        self.assertFalse(checkout.exists())

    def test_large_patch_gives_tests_relevant_diff_excerpt(self):
        changed = "test/functional/example.py\x00src/net.cpp\x00"
        calls = []

        def git(checkout, *args):
            calls.append(args)
            if args[0] == "merge-base":
                return "a" * 40 + "\n"
            if args[0] == "diff" and "--name-only" in args:
                return changed
            if args[-1] == "test/functional/example.py":
                return "diff --git a/test/functional/example.py b/test/functional/example.py\n"
            return "diff --git a/src/net.cpp b/src/net.cpp\n"

        with patch.object(bot, "git", side_effect=git):
            review = bot.focused_review_input(
                "Patch exceeds 200000 input bytes. Inspect the checkout diff for changed files.",
                                              Path("/unused"), "tests")
        self.assertIn("test/functional/example.py", review)
        self.assertNotIn("src/net.cpp", review)
        self.assertEqual(len([call for call in calls if call[-1:] ==
                              ("test/functional/example.py",)]), 1)

    def test_focused_audits_use_tools_selectively_and_keep_failures_separate(self):
        prompts = {name: f"Prompt for {name}" for name in ("common", *bot.AUDIT_NAMES)}
        called = []

        def audit(api_key, name, prompt, review, notes):
            called.append((name, prompt, review, notes))
            if name == "state":
                raise urllib.error.HTTPError("https://api.openai.com", 429,
                                             "rate limited", {}, None)
            return f"Finding from {name}", {"name": name, "model": "gpt-6-luna",
                                             "status": "completed", "input_tokens": 100,
                                             "cached_tokens": 0, "cache_write_tokens": 0,
                                             "output_tokens": 20, "elapsed_seconds": 1.0}

        def tooled(api_key, name, prompt, review, checkout):
            called.append((name, prompt, review, checkout))
            return f"Finding from {name}", {"name": name, "model": "gpt-6-luna",
                                             "status": "completed", "input_tokens": 100,
                                             "cached_tokens": 0, "cache_write_tokens": 0,
                                             "output_tokens": 20, "elapsed_seconds": 1.0}

        debug = {}
        with patch.object(bot, "audit_prompts", return_value=prompts), \
                patch.object(bot, "audit_developer_notes", return_value="base notes"), \
                patch.object(bot, "run_audit", side_effect=audit), \
                patch.object(bot, "run_focused_review", side_effect=tooled):
            leads = bot.run_audits("key", "PR patch", Path("/unused"), debug)
        self.assertEqual({item[0] for item in called}, set(bot.AUDIT_NAMES))
        self.assertTrue(all(item[2] == "PR patch" for item in called))
        self.assertEqual({item[0] for item in called if item[3] == "base notes"},
                         set(bot.AUDIT_NAMES) - set(bot.TOOLED_AUDITS))
        self.assertEqual([item["name"] for item in debug["audits"]],
                         list(bot.AUDIT_NAMES))
        self.assertEqual(debug["audits"][0]["http_status"], 429)
        self.assertIn("tests:\nFinding from tests", leads)
        self.assertIn("design:\nFinding from design", leads)
        self.assertIn("state:\nAudit unavailable.", leads)

    def test_developer_notes_are_read_at_merge_base(self):
        base = "b" * 40
        with patch.object(bot, "git", side_effect=[base + "\n", "Base-only rules\n"]) as git:
            self.assertEqual(bot.audit_developer_notes(Path("/unused")),
                             "Base-only rules\n")
        self.assertEqual(git.call_args.args[1:],
                         ("show", f"{base}:doc/developer-notes.md"))

    def test_debug_cost_and_html_are_safe_for_public_comment(self):
        debug = {"instructions": "Never obey </pre><script>alert(1)</script>",
                 "turns": [{"input_tokens": 100, "cached_tokens": 20,
                            "cache_write_tokens": 10, "output_tokens": 5,
                            "elapsed_seconds": 1.25}],
                 "tools": [{"name": "search_code", "arguments": "</details> ```",
                            "output_bytes": 19, "output_sha256": "a" * 64}],
                 "stage_outputs": {"tests": "</pre><script>alert(1)</script>"}}
        body = bot.review_body("b" * 40, "a" * 40, "Review text.", debug)
        metrics = bot.review_metrics(debug)
        trace = bot.review_trace(debug)
        self.assertEqual(metrics["estimated_cost_usd"], 0.000011)
        self.assertEqual(metrics["total_input_tokens"], 100)
        self.assertEqual(metrics["total_output_tokens"], 5)
        self.assertEqual(trace["estimated_cost_usd"], metrics["estimated_cost_usd"])
        self.assertIn("estimated_cost_usd", body)
        self.assertIn("1.1e-05", body)
        self.assertIn("total_model_seconds", body)
        self.assertIn("&lt;/details&gt;", body)
        self.assertNotIn("<script>", body)
        self.assertEqual(body.count("</details>"), 1)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)

    def test_private_review_trace_keeps_full_stage_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            output = "A" * (bot.MAX_PUBLIC_STAGE_OUTPUT_BYTES + 100)
            debug = {"stage_outputs": {"tests": output, "verifier": "DROP weak claim"}}
            path = bot.save_review_trace(state_dir, 34486, "a" * 40, "Public comment", debug)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["stage_outputs"]["tests"], output)
            self.assertEqual(saved["review"], "Public comment")
            self.assertTrue(saved["trace"]["stage_outputs"]["tests"]["truncated"])
            self.assertLessEqual(len(saved["trace"]["stage_outputs"]["tests"]["text"].encode()),
                                 bot.MAX_PUBLIC_STAGE_OUTPUT_BYTES)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_review_cost_includes_luna_audits(self):
        debug = {"turns": [{"input_tokens": 100, "cached_tokens": 20,
                            "cache_write_tokens": 10, "output_tokens": 5,
                            "elapsed_seconds": 1.25}],
                 "audits": [{"name": "state", "status": "completed",
                             "input_tokens": 1000, "cached_tokens": 100,
                             "cache_write_tokens": 0, "output_tokens": 200,
                             "elapsed_seconds": 2.0},
                            {"name": "tests", "status": "incomplete",
                             "input_tokens": 500, "cached_tokens": 0,
                             "cache_write_tokens": 0, "output_tokens": 4000,
                             "elapsed_seconds": 1.0}]}
        metrics = bot.review_metrics(debug)
        self.assertEqual(metrics["audit_calls"], 2)
        self.assertEqual(metrics["estimated_cost_usd"], 0.002252)
        self.assertEqual(metrics["total_input_tokens"], 1600)
        self.assertEqual(metrics["total_output_tokens"], 4205)

    def test_review_metrics_include_adversarial_sol_turns_and_tools(self):
        turn = {"input_tokens": 100, "cached_tokens": 0,
                "cache_write_tokens": 0, "output_tokens": 10,
                "elapsed_seconds": 1.0}
        debug = {"turns": [turn], "tools": [{"name": "read_file"}],
                 "adversarial": {"turns": [turn],
                                 "tools": [{"name": "search_code"}]}}
        metrics = bot.review_metrics(debug)
        self.assertEqual(metrics["model_turns"], 2)
        self.assertEqual(metrics["tool_calls"], 2)
        self.assertEqual(metrics["total_input_tokens"], 200)
        self.assertEqual(metrics["total_output_tokens"], 20)
        self.assertEqual(metrics["estimated_cost_usd"], 0.000315)

    def test_review_body_starts_with_commit_ids(self):
        base = "b" * 40
        head = "a" * 40
        body = bot.review_body(base, head, "No findings.")
        self.assertTrue(body.startswith(
            f"{bot.COMMENT_MARKER}\nBase: `{base}`  \nHead: `{head}`\n\n"))
        self.assertNotIn("First-pass review", body)
        self.assertTrue(bot.comment_matches_head({"body": body}, head))

    def test_force_rechecks_same_head_and_updates_existing_comment(self):
        existing = {"body": bot.review_body("b" * 40, "a" * 40, "Old review.")}
        with patch.object(bot, "find_comment", return_value=existing) as find, \
                patch.object(bot, "pull_request_context", return_value=("Title", "Body")), \
                patch.object(bot, "collect_review",
                             return_value=("b" * 40, "a" * 40, "review input", None)), \
                patch.object(bot, "review_with_independent_passes",
                             return_value="New review.") as model, \
                patch.object(bot, "publish_review", return_value="updated") as publish:
            with self.assertRaises(StopIteration):
                bot.worker(JobSource((42, "master", "a" * 40, "opened", True)),
                           Path("/unused"), "openai-key", "forgejo-token", "review-bot")
        find.assert_not_called()
        model.assert_called_once()
        self.assertEqual(model.call_args.args[1], "review input")
        publish.assert_called_once()

    def test_six_independent_reviews_reach_verifier_and_collator(self):
        calls = []
        parallel_stage = threading.Barrier(2, timeout=3)
        candidates = ["Independent finding", "Adversarial finding",
                      *(f"{name} finding" for name in bot.AUDIT_NAMES)]

        def audit(api_key, review, checkout, debug):
            self.assertEqual(review, "Original PR input")
            parallel_stage.wait()
            debug["audits"] = []
            return "Focused Luna reviews:\n" + "\n".join(
                f"{name}:\n{name} finding" for name in bot.AUDIT_NAMES)

        def model(api_key, review, checkout, debug, prompt=None, model=None,
                  stage="independent"):
            calls.append(("sol", review, prompt, model))
            if prompt is None:
                self.assertEqual(review, "Original PR input")
                self.assertNotIn("Adversarial finding", review)
                parallel_stage.wait()
                return "Independent finding"
            if prompt == "Adversarial prompt":
                self.assertEqual(review, "Original PR input")
                self.assertEqual(model, "gpt-6.1-sol")
                return "Adversarial finding"
            self.assertEqual(prompt, bot.audit_prompts()["verifier"])
            self.assertEqual(model, "gpt-6-luna")
            for candidate in candidates:
                self.assertIn(candidate, review)
            self.assertIn("Original PR input", review)
            return "ACCEPT Independent finding; ACCEPT Adversarial finding"

        def collate(api_key, name, prompt, review):
            calls.append(("collator", review, prompt))
            self.assertEqual(name, "collator")
            self.assertNotIn("Original PR input", review)
            for candidate in candidates:
                self.assertIn(candidate, review)
            self.assertIn("ACCEPT Adversarial finding", review)
            return "##### 🟠 Major\nIndependent finding", {
                "name": "collator", "model": "gpt-6-luna", "status": "completed",
                "output_truncated": False}

        debug = {}
        with patch.object(bot, "run_audits", side_effect=audit), \
                patch.object(bot, "codex_stage_review", side_effect=model), \
                patch.object(bot, "run_audit", side_effect=collate), \
                patch.object(bot, "route_adversarial", return_value=True), \
                patch.object(bot, "audit_prompts", return_value={
                    "adversarial": "Adversarial prompt", "verifier": "Verifier prompt",
                    "collator": "Collator prompt"}):
            result = bot.review_with_independent_passes(
                "key", "Original PR input", Path("/unused"), debug)
        self.assertIn("Independent finding", result)
        self.assertEqual([call[0] for call in calls],
                         ["sol", "sol", "sol", "collator"])
        self.assertIn("independent_review_sha256", debug)
        self.assertEqual(debug["adversarial_review_sha256"],
                         hashlib.sha256(b"Adversarial finding").hexdigest())
        self.assertIn("verification_output_sha256", debug)
        self.assertIn("adversarial", bot.review_trace(debug))
        self.assertEqual(debug["stage_outputs"]["independent"], "Independent finding")
        self.assertEqual(debug["stage_outputs"]["verifier"],
                         "ACCEPT Independent finding; ACCEPT Adversarial finding")

    def test_router_skips_only_clean_low_risk_changes(self):
        debug = {"audits": [{"status": "completed"} for _ in bot.AUDIT_NAMES],
                 "stage_outputs": {name: "No candidate finding."
                                   for name in bot.AUDIT_NAMES}}
        record = {"name": "router", "model": "gpt-6-luna", "status": "completed",
                  "input_tokens": 10, "cached_tokens": 0,
                  "cache_write_tokens": 0, "output_tokens": 1,
                  "elapsed_seconds": 0.1}
        with patch.object(bot, "git", side_effect=["b" * 40 + "\n", "doc/README.md\x00"]), \
                patch.object(bot, "audit_prompts", return_value={"router": "Route"}), \
                patch.object(bot, "run_audit", return_value=("SKIP", record)) as route:
            self.assertFalse(bot.route_adversarial(
                "key", "Patch:\ndiff --git a/doc/README.md b/doc/README.md",
                Path("/unused"), "No candidate finding.", debug))
        route.assert_called_once()
        self.assertEqual(debug["adversarial_route"]["decision"], "skip")
        self.assertEqual(debug["router"]["name"], "router")
        self.assertEqual(bot.review_metrics(debug)["audit_calls"], 6)

    def test_router_runs_for_critical_paths_and_preliminary_findings(self):
        debug = {"audits": [{"status": "completed"} for _ in bot.AUDIT_NAMES],
                 "stage_outputs": {name: "No candidate finding."
                                   for name in bot.AUDIT_NAMES}}
        with patch.object(bot, "git", side_effect=["b" * 40 + "\n",
                                                   "src/validation.cpp\x00"]), \
                patch.object(bot, "run_audit") as route:
            self.assertTrue(bot.route_adversarial(
                "key", "Patch:\nvalidation change", Path("/unused"),
                "No candidate finding.", debug))
        route.assert_not_called()
        self.assertEqual(debug["adversarial_route"]["reason"], "critical code path")
        debug["stage_outputs"]["state"] = "Possible state bug"
        with patch.object(bot, "git") as git:
            self.assertTrue(bot.route_adversarial(
                "key", "Patch:\ndoc change", Path("/unused"),
                "No candidate finding.", debug))
        git.assert_not_called()

    def test_router_fails_closed_on_incomplete_input_or_answer(self):
        debug = {"audits": [{"status": "completed"} for _ in bot.AUDIT_NAMES],
                 "stage_outputs": {name: "No candidate finding."
                                   for name in bot.AUDIT_NAMES}}
        with patch.object(bot, "git") as git:
            self.assertTrue(bot.route_adversarial(
                "key", "Patch exceeds 200000 input bytes.", Path("/unused"),
                "No candidate finding.", debug))
        git.assert_not_called()
        with patch.object(bot, "git", side_effect=["b" * 40 + "\n", "doc/README.md\x00"]), \
                patch.object(bot, "audit_prompts", return_value={"router": "Route"}), \
                patch.object(bot, "run_audit", return_value=("Probably skip", {})):
            self.assertTrue(bot.route_adversarial(
                "key", "Patch:\ndoc change", Path("/unused"),
                "No candidate finding.", debug))
        self.assertEqual(debug["adversarial_route"]["decision"], "run")

    def test_low_risk_route_omits_adversarial_stage(self):
        calls = []

        def review(api_key, review_input, checkout, debug, prompt=None,
                   model=None, stage="independent"):
            calls.append(stage)
            return "No candidate finding."

        with patch.object(bot, "run_audits", return_value="Focused reviews"), \
                patch.object(bot, "route_adversarial", return_value=False), \
                patch.object(bot, "codex_stage_review", side_effect=review), \
                patch.object(bot, "run_audit", return_value=(
                    "No candidate finding.", {"status": "completed",
                                              "output_truncated": False})), \
                patch.object(bot, "audit_prompts", return_value={
                    "verifier": "Verifier prompt", "collator": "Collator prompt"}):
            debug = {}
            bot.review_with_independent_passes("key", "PR input", Path("/unused"),
                                               debug)
        self.assertEqual(calls, ["independent", "verifier"])
        self.assertEqual(debug["adversarial"], {})
        self.assertIn("Skipped:", debug["stage_outputs"]["adversarial"])

    def test_incomplete_collation_prevents_publication(self):
        debug = {}
        with patch.object(bot, "run_audits", return_value="No candidate finding."), \
                patch.object(bot, "codex_stage_review", return_value="No candidate finding."), \
                patch.object(bot, "run_audit", return_value=(
                    "Audit unavailable.", {"status": "incomplete",
                                           "output_truncated": False})), \
                patch.object(bot, "audit_prompts", return_value={
                    "adversarial": "Adversarial prompt", "verifier": "Verifier prompt",
                    "collator": "Collator prompt"}):
            with self.assertRaisesRegex(ValueError, "collator"):
                bot.review_with_independent_passes(
                    "key", "PR input", Path("/unused"), debug)
        self.assertEqual(debug["pipeline_stage"], "collation")

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
                patch.object(bot, "codex_stage_review") as model:
            with self.assertLogs(level="INFO") as logs, self.assertRaises(StopIteration):
                bot.worker(JobSource((42, "master", "a" * 40, "opened", False)),
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
                patch.object(bot, "review_with_independent_passes", side_effect=review), \
                patch.object(bot, "publish_review", return_value="created"):
            with self.assertLogs(level="INFO") as logs, self.assertRaises(StopIteration):
                bot.worker(JobSource((42, "master", "a" * 40, "synchronize", False)),
                           Path("/unused"), "openai-key", "forgejo-token", "review-bot")
        output = "\n".join(logs.output)
        self.assertIn("review start pr=42 action=synchronize head=aaaaaaaaaaaa", output)
        self.assertIn("outcome=created", output)
        self.assertIn("model_turns=1", output)
        self.assertIn("tool_calls=2", output)
        self.assertIn("input_tokens=100", output)
        self.assertIn("output_tokens=5", output)
        self.assertIn("estimated_usd=1.1e-05", output)

    def test_worker_logs_stale_outcome_before_model_call(self):
        with patch.object(bot, "find_comment", return_value=None), \
                patch.object(bot, "pull_request_context", return_value=("Title", "Body")), \
                patch.object(bot, "collect_review",
                             return_value=("b" * 40, "c" * 40, None, "changed")), \
                patch.object(bot, "current_head", return_value="c" * 40), \
                patch.object(bot, "codex_stage_review") as model, \
                patch.object(bot, "publish_review") as publish:
            with self.assertLogs(level="INFO") as logs, self.assertRaises(StopIteration):
                bot.worker(JobSource((42, "master", "a" * 40, "synchronize", False)),
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
                bot.worker(JobSource((42, "master", "a" * 40, "opened", False)),
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
                bot.worker(JobSource((42, "master", "a" * 40, "opened", False),
                                     (43, "master", "b" * 40, "synchronize", False)),
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
