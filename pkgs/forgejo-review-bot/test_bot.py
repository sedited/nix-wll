import hashlib
import json
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch


from forgejo_review_bot import config, forgejo, model, repository, trace


class BotTests(unittest.TestCase):
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


    def test_config_derives_repository_url_and_default_marker(self):
        self.assertEqual(self.bot_config.repository_url, "https://git.fish.foo/bitcoin/bitcoin")
        self.assertEqual(self.bot_config.comment_marker,
                         "<!-- forgejo-review-bot:bitcoin/bitcoin -->")

    def test_report_configuration_requires_a_directory_and_public_url(self):
        with self.assertRaisesRegex(ValueError, "set together"):
            replace(self.bot_config, report_dir=Path("/public"))
        for url in ("javascript:alert(1)", "https://user:secret@example.org",
                    "https://example.org/?secret=foo", "https://example.org/#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                replace(self.bot_config, report_dir=Path("/public"), report_base_url=url)

    def test_review_comment_links_to_full_report_and_keeps_commit_header(self):
        url = "https://review.example.org/traces/42-run.html"
        body = forgejo.review_body(self.bot_config, self.prompt_config,
                                   "b" * 40, "a" * 40, "Review text.",
                                   {"report_url": url,
                                    "stage_outputs": {"tests": "Preliminary candidate"}})
        self.assertIn(f"[Full review report](<{url}>)", body)
        self.assertIn(f"Head: `{'a' * 40}`", body.splitlines()[:5])
        self.assertNotIn("Review debug", body)
        self.assertNotIn("<details>", body)
        self.assertNotIn("Preliminary candidate", body)

    def test_config_requires_repository_url_when_api_url_is_not_deriveable(self):
        with self.assertRaisesRegex(ValueError, "repository_url"):
            config.BotConfig("https://git.example.org/owner/repo.git", "owner/repo",
                             "https://git.example.org/custom-api")
        configured = config.BotConfig("https://git.example.org/owner/repo.git", "owner/repo",
                                      "https://git.example.org/custom-api",
                                      "https://git.example.org/owner/repo")
        self.assertEqual(configured.repository_url, "https://git.example.org/owner/repo")

    def test_model_config_requires_adversarial_stage_and_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_dir = Path(directory)
            for name in config.PROMPT_NAMES:
                (audit_dir / f"{name}.md").write_text(f"{name} instructions\n")
            models = {name: "gpt-6.1-sol" if name in
                      ("independent", "adversarial") else "gpt-6-luna"
                      for name in config.MODEL_NAMES}
            (audit_dir / "models.json").write_text(json.dumps(models))
            loaded = config.PromptConfig.load(config.DEFAULT_PROMPT_FILE, audit_dir)
            self.assertEqual(loaded.audit_prompts["router"], "router instructions")
            self.assertEqual(loaded.audit_prompts["adversarial"],
                             "adversarial instructions")
            self.assertEqual(loaded.models["adversarial"], "gpt-6.1-sol")
            self.assertEqual(loaded.models["verifier"], "gpt-6-luna")
            del models["adversarial"]
            (audit_dir / "models.json").write_text(json.dumps(models))
            with self.assertRaisesRegex(ValueError, "each review stage"):
                config.PromptConfig.load(config.DEFAULT_PROMPT_FILE, audit_dir)


    def test_collect_review_skips_stale_head_without_model_call(self):
        outputs = iter(["", "b" * 40, "c" * 40])
        with patch.object(repository, "prepare_checkout"), patch.object(repository, "git", side_effect=lambda *a: next(outputs)):
            base, head, review, skip = repository.collect_review(
                Path("/unused"), 42, "master", "a" * 40, "Title", "Description", self.bot_config)
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

        with patch.object(repository, "prepare_checkout"), patch.object(repository, "git", side_effect=fake_git):
            result = repository.collect_review(Path("/unused"), 42, "master", head,
                                        "PR title", "Why this change is needed", self.bot_config)
        self.assertEqual(result[0:2], (base, head))
        self.assertIn("+change", result[2])
        self.assertEqual(len(result), 4)
        self.assertIn("Explain the commit rationale", result[2])
        self.assertIn("PR title: PR title", result[2])
        self.assertIn("PR description:\nWhy this change is needed", result[2])
        self.assertIn(("diff", "--no-ext-diff", "--binary", f"{merge_base}..{head}"), calls)

    def test_concurrent_review_refs_preserve_pinned_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin.git"
            work = root / "work"
            checkout = root / "checkout"
            repository.git(root, "init", "--bare", str(origin))
            repository.git(root, "init", str(work))
            repository.git(work, "config", "user.name", "Test")
            repository.git(work, "config", "user.email", "test@example.com")
            repository.git(work, "remote", "add", "origin", str(origin))
            path = work / "code.cpp"
            path.write_text("base behavior\n")
            repository.git(work, "add", "code.cpp")
            repository.git(work, "commit", "-qm", "Base")
            base = repository.git(work, "rev-parse", "HEAD").strip()
            repository.git(work, "push", "origin", "HEAD:refs/heads/master")

            path.write_text("first PR behavior\n")
            repository.git(work, "commit", "-qam", "First PR")
            first_head = repository.git(work, "rev-parse", "HEAD").strip()
            repository.git(work, "push", "origin", "HEAD:refs/pull/1/head")
            repository.git(work, "reset", "--hard", base)
            path.write_text("second PR behavior\n")
            repository.git(work, "commit", "-qam", "Second PR")
            second_head = repository.git(work, "rev-parse", "HEAD").strip()
            repository.git(work, "push", "origin", "HEAD:refs/pull/2/head")

            bot_config = config.BotConfig(str(origin), "example/repo",
                "https://git.example.org/api/v1/repos/example/repo")
            first_prefix = "refs/review-bot/jobs/1-1"
            second_prefix = "refs/review-bot/jobs/2-1"
            first = repository.collect_review(checkout, 1, "master", first_head,
                "First", "First PR", bot_config, ref_prefix=first_prefix)
            self.assertIsNone(first[3])
            first_snapshot = repository.snapshot_repository(checkout, first[0], first[1])
            second = repository.collect_review(checkout, 2, "master", second_head,
                "Second", "Second PR", bot_config, ref_prefix=second_prefix)
            self.assertIsNone(second[3])
            second_snapshot = repository.snapshot_repository(checkout, second[0], second[1])
            repository.git(checkout, "update-ref", "refs/review-bot/head", base)

            self.assertIn("first PR behavior", repository.read_file(checkout,
                first_snapshot.head_files, "code.cpp", 1))
            self.assertIn("+first PR behavior", repository.read_diff(checkout,
                first_snapshot.changed_paths, first_snapshot.merge_base,
                first_snapshot.head_sha, "code.cpp", 1))
            self.assertIn("second PR behavior", repository.read_file(checkout,
                second_snapshot.head_files, "code.cpp", 1))
            repository.git(checkout, "gc", "--prune=now")
            self.assertIn("first PR behavior", repository.read_file(checkout,
                first_snapshot.head_files, "code.cpp", 1))
            repository.git(checkout, "update-ref", "refs/review-cases/example/head", first_head)

            repository.release_review_refs(checkout, first_prefix)
            self.assertEqual(repository.git(checkout, "for-each-ref", "--format=%(refname)",
                first_prefix), "")
            self.assertEqual(repository.git(checkout, "rev-parse", f"{second_prefix}/head").strip(),
                second_head)
            self.assertEqual(repository.git(checkout, "rev-parse", f"{second_prefix}/base").strip(),
                base)
            self.assertEqual(repository.git(checkout, "rev-parse", "refs/review-bot/head").strip(),
                base)
            self.assertEqual(repository.git(checkout, "rev-parse",
                "refs/review-cases/example/head").strip(), first_head)

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

        with patch.object(repository, "prepare_checkout"), patch.object(repository, "git",
                                                                   side_effect=fake_git):
            _base, _head, review, skip = repository.collect_review(
                Path("/unused"), 42, "master", head, "Title", "Description", self.bot_config)
        self.assertIsNone(skip)
        self.assertIn("Use read_diff", review)
        self.assertIn("M\tsrc/main.cpp", review)
        self.assertNotIn("+change", review)

    def test_fetch_pr_context_from_mirrored_issue(self):
        issue = {"number": 42, "pull_request": {"html_url": "https://example.invalid/pulls/42"},
                 "title": "Fix sanitizer warning", "body": "Reproduced with an empty vector"}
        with patch.object(forgejo, "forgejo_request", return_value=issue) as request:
            self.assertEqual(forgejo.pull_request_context(self.bot_config, "token", 42),
                             (issue["title"], issue["body"]))
            issue["pull_request"] = None
            with self.assertRaisesRegex(ValueError, "invalid pull request"):
                forgejo.pull_request_context(self.bot_config, "token", 42)
        request.assert_called_with(self.bot_config, "token", "/issues/42")

    def test_public_discussion_request_sends_no_token_and_limits_response(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, size):
                return self.body[:size]

        with patch.object(urllib.request, "urlopen",
                          return_value=Response(b'{"number": 42}')) as send:
            self.assertEqual(forgejo.public_discussion_request(self.bot_config, "/issues/42"),
                             {"number": 42})
        request = send.call_args.args[0]
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.full_url,
                         "https://git.fish.foo/api/v1/repos/bitcoin/bitcoin/issues/42")
        with patch.object(urllib.request, "urlopen",
                          return_value=Response(b"x" * (forgejo.MAX_DISCUSSION_RESPONSE_BYTES + 1))):
            with self.assertRaisesRegex(ValueError, "context limit"):
                forgejo.public_discussion_request(self.bot_config, "/issues/42")

    def test_discussion_search_and_read_are_bounded_and_exclude_bot_comment(self):
        issue = {"number": 42, "pull_request": {"html_url": "ignored"},
                 "title": "Fix a fee edge case", "body": "Reason for the change"}
        comments = [{"id": i, "user": {"login": "reviewer"},
                     "body": f"Comment {i}"} for i in range(12)]
        comments.append({"id": 99, "user": {"login": "ralph"},
                         "body": self.bot_config.comment_marker + " Bot review"})

        def request(bot_config, path):
            if path.startswith("/issues?"):
                return [issue]
            if path == "/issues/42":
                return issue
            if path.startswith("/issues/42/comments?"):
                return comments
            self.fail(path)

        with patch.object(forgejo, "public_discussion_request", side_effect=request) as get:
            matches = forgejo.search_discussions(self.bot_config, "fee edge", 99)
            discussion = forgejo.read_discussion(self.bot_config, 42, 99)
        self.assertIn("PR #42: Fix a fee edge case", matches)
        self.assertIn("q=fee%20edge", get.call_args_list[0].args[1])
        self.assertIn("Reason for the change", discussion)
        self.assertIn("Selected comments (8 of 12)", discussion)
        self.assertIn("Comment 0", discussion)
        self.assertIn("Comment 11", discussion)
        self.assertNotIn("Comment 4", discussion)
        self.assertNotIn("Bot review", discussion)
        self.assertEqual(forgejo.read_discussion(self.bot_config, -1, 99), "Invalid issue or PR number.")

    def test_current_pr_is_excluded_before_any_discussion_fetch(self):
        with patch.object(forgejo, "public_discussion_request", return_value=[
                {"number": 42, "title": "Current PR", "pull_request": {}},
                {"number": 41, "title": "Earlier PR", "pull_request": {}}]) as get:
            self.assertNotIn("Current PR", forgejo.search_discussions(self.bot_config, "change", 42))
            self.assertIn("Earlier PR", forgejo.search_discussions(self.bot_config, "change", 42))
            self.assertIn("excluded", forgejo.read_discussion(self.bot_config, 42, 42))
        self.assertEqual(get.call_count, 2)

    def test_model_cannot_read_current_pr_discussion(self):
        responses = [
            {"status": "completed", "output": [{"type": "function_call",
                "id": "fc_1", "call_id": "call_1", "name": "read_discussion",
                "arguments": '{"number":42}'}]},
            {"status": "completed", "output": [{"type": "message",
                "content": [{"type": "output_text", "text": "No findings."}]}]},
        ]
        requests = []

        class Response:
            def __init__(self, value):
                self.value = value

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return json.dumps(self.value).encode()

        def send(request, timeout):
            requests.append(json.loads(request.data))
            return Response(responses.pop(0))

        with patch.object(urllib.request, "urlopen", side_effect=send), \
                patch.object(forgejo, "public_discussion_request") as fetch:
            self.assertEqual(model.openai_review("key", "patch", self.snapshot(),
                                                self.bot_config, self.prompt_config, current_pr=42), "No findings.")
        fetch.assert_not_called()
        self.assertIn("current PR's discussion is excluded",
                      requests[1]["input"][-1]["output"])

    def test_blame_and_commit_are_limited_to_base_history(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "-C", directory, "init", "-q"], check=True)
            path = checkout / "code.cpp"
            path.write_text("old guard\nunchanged\n")
            subprocess.run(["git", "-C", directory, "add", "code.cpp"], check=True)
            commit = ["git", "-C", directory, "-c", "user.name=Test",
                      "-c", "user.email=test@example.com", "commit", "-qm"]
            subprocess.run(commit + ["Guard the old case"], check=True)
            base = repository.git(checkout, "rev-parse", "HEAD").strip()
            path.write_text("new guard\nunchanged\n")
            subprocess.run(["git", "-C", directory, "add", "code.cpp"], check=True)
            subprocess.run(commit + ["PR change"], check=True)
            head = repository.git(checkout, "rev-parse", "HEAD").strip()
            base_files = repository.tracked_files_at(checkout, base)

            blame = repository.blame_base(checkout, base_files, base, "code.cpp", 1)
            self.assertIn(base, blame)
            self.assertIn("old guard", blame)
            self.assertIn("Guard the old case", blame)
            details = repository.read_commit(checkout, base_files, base, base, "code.cpp")
            self.assertIn("Guard the old case", details)
            self.assertIn("+old guard", details)
            self.assertIn("not an ancestor", repository.read_commit(
                checkout, base_files, base, head, "code.cpp"))
            self.assertIn("Invalid", repository.blame_base(
                checkout, base_files, base, "../code.cpp", 1))

    def test_openai_request_disables_storage(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return json.dumps({"status": "completed", "output": [{"type": "message",
                    "content": [{"type": "output_text", "text": "No findings."}]}]}).encode()

        with patch.object(urllib.request, "urlopen", return_value=Response()) as send:
            self.assertEqual(model.openai_review("test-key", "patch", self.snapshot(),
                                                  self.bot_config, self.prompt_config),
                             "No findings.")
        request = send.call_args.args[0]
        sent = json.loads(request.data)
        self.assertIs(sent["store"], False)
        self.assertEqual(sent["model"], self.prompt_config.models["independent"])
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
            custom_prompts = config.PromptConfig(config.load_prompt_file(prompt_file),
                                                 self.prompt_config.audit_prompts,
                                                 self.prompt_config.models)
            with patch.object(urllib.request, "urlopen", return_value=Response()) as send:
                self.assertEqual(model.openai_review("test-key", "patch", self.snapshot(),
                                                      self.bot_config, custom_prompts, debug),
                                 "No findings.")
        request = send.call_args.args[0]
        sent = json.loads(request.data)
        self.assertEqual(sent["instructions"], "Custom review prompt")
        self.assertEqual(debug["instructions"], "Custom review prompt")

    def test_audit_request_uses_configured_model_and_keeps_full_output(self):
        text = "Candidate: " + "x" * 5000

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return json.dumps({"status": "completed", "output": [{"type": "message",
                    "content": [{"type": "output_text", "text": text}]}],
                    "usage": {"input_tokens": 100, "output_tokens": 20}}).encode()

        with patch.object(urllib.request, "urlopen", return_value=Response()) as send:
            answer, record = model.run_audit("key", "public_contract", "Check policy",
                                           "PR patch", self.prompt_config)
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["model"], self.prompt_config.models["public_contract"])
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertIs(payload["store"], False)
        self.assertEqual(payload["instructions"], "Check policy")
        self.assertEqual(payload["max_output_tokens"], model.MAX_AUDIT_OUTPUT_TOKENS)
        self.assertEqual(answer, text)
        self.assertEqual(record["name"], "public_contract")
        self.assertEqual(record["input_tokens"], 100)
        self.assertFalse(record["output_truncated"])

    def test_collator_has_room_for_a_concise_final_review(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return json.dumps({"status": "completed", "output": [{"type": "message",
                    "content": [{"type": "output_text", "text": "A" * 5000}]}]}).encode()

        with patch.object(urllib.request, "urlopen", return_value=Response()) as send:
            answer, record = model.run_audit("key", "collator", "Edit review", "Leads", self.prompt_config)
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["max_output_tokens"], model.MAX_COLLATOR_OUTPUT_TOKENS)
        self.assertEqual(len(answer), 5000)
        self.assertFalse(record["output_truncated"])

    def test_large_patch_gives_tests_relevant_diff_excerpt(self):
        calls = []

        def git(checkout, *args):
            calls.append(args)
            if args[-1] == "test/functional/example.py":
                return ("diff --git a/test/functional/example.py b/"
                        "test/functional/example.py\n"
                        + "x" * repository.MAX_FOCUSED_DIFF_BYTES)
            return "diff --git a/src/net.cpp b/src/net.cpp\n"

        with patch.object(repository, "git", side_effect=git):
            review = repository.focused_review_input(
                "Patch exceeds 200000 input bytes. Use read_diff to inspect changed files.",
                repository.RepositorySnapshot(
                    Path("/unused"), "b" * 40, "a" * 40, "b" * 40,
                    {}, {}, frozenset({"test/functional/example.py", "src/net.cpp"})),
                "tests")
        self.assertIn("test/functional/example.py", review)
        self.assertNotIn("src/net.cpp", review)
        self.assertEqual([call[-1] for call in calls], ["test/functional/example.py"])

    def test_incomplete_audit_records_reason_and_usage(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, *args):
                return json.dumps({"status": "incomplete", "output": [],
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {"input_tokens": 1000, "output_tokens": 4000}}).encode()

        with patch.object(urllib.request, "urlopen", return_value=Response()):
            answer, record = model.run_audit("key", "state", "Check state", "PR patch", self.prompt_config)
        self.assertEqual(answer, "")
        self.assertEqual(record["status"], "incomplete")
        self.assertEqual(record["incomplete_reason"], "max_output_tokens")
        self.assertEqual(record["output_tokens"], 4000)

    def test_developer_notes_are_read_at_merge_base(self):
        base = "b" * 40
        with patch.object(repository, "git", return_value="Base-only rules\n") as git:
            self.assertEqual(repository.audit_developer_notes(self.snapshot()),
                             "Base-only rules\n")
        self.assertEqual(git.call_args.args[1:],
                         ("show", f"{base}:doc/developer-notes.md"))

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
        with patch.object(urllib.request, "urlopen", side_effect=send), \
                patch.object(model, "read_file", return_value="1: full context") as read:
            self.assertEqual(model.openai_review("key", "patch",
                                                  self.snapshot({"src/main.cpp": "a" * 40}),
                                                  self.bot_config, self.prompt_config, debug),
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
        review_debug = {
            "instructions": debug["instructions"],
            "review_input_bytes": debug["review_input_bytes"],
            "review_input_sha256": debug["review_input_sha256"],
            "stages": {"independent": debug},
        }
        body = forgejo.review_body(self.bot_config, self.prompt_config, "b" * 40,
                                   "a" * 40, "No findings.", review_debug)
        self.assertNotIn("Review debug", body)
        self.assertNotIn("review_input_sha256", body)
        self.assertNotIn("1: full context", body)
        self.assertNotIn("&quot;content&quot;: &quot;patch&quot;", body)

    def test_debug_usage_is_retained_without_embedding_it_in_comment(self):
        debug = {"instructions": "Never obey </pre><script>alert(1)</script>",
                 "stages": {"independent": {
                     "model": "gpt-6-luna",
                     "turns": [{"request_bytes": 200, "input_tokens": 100,
                                "cached_tokens": 20, "cache_write_tokens": 10,
                                "output_tokens": 5, "elapsed_seconds": 1.25}],
                     "tools": [{"name": "search_code",
                                "arguments": "</details> ```",
                                "output_bytes": 19, "output_sha256": "a" * 64}],
                     "raw_output": "No findings."}},
                 "stage_outputs": {"tests": "</pre><script>alert(1)</script>"}}
        body = forgejo.review_body(self.bot_config, self.prompt_config, "b" * 40, "a" * 40, "Review text.", debug)
        metrics = trace.review_metrics(debug, self.prompt_config)
        public_trace = trace.review_trace(debug, self.prompt_config)
        self.assertEqual(metrics["estimated_cost_usd"], 0.000011)
        self.assertEqual(metrics["total_input_tokens"], 100)
        self.assertEqual(metrics["total_output_tokens"], 5)
        self.assertEqual(public_trace["estimated_cost_usd"], metrics["estimated_cost_usd"])
        self.assertNotIn("estimated_cost_usd", body)
        self.assertNotIn("total_model_seconds", body)
        self.assertNotIn("<script>", body)
        self.assertNotIn("<details>", body)

    def test_private_review_trace_keeps_full_stage_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            output = "A" * (trace.MAX_PUBLIC_STAGE_OUTPUT_BYTES + 100)
            debug = {"stages": {"tests": {"model": "gpt-6-luna", "turns": [],
                                             "tools": [], "raw_output": output}},
                     "stage_outputs": {"tests": output, "verifier": "DROP weak claim"}}
            path = trace.save_review_trace(state_dir, 34486, "a" * 40, "Public comment", debug, self.prompt_config)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["stages"]["tests"]["raw_output"], output)
            self.assertEqual(saved["stage_outputs"]["tests"], output)
            self.assertEqual(saved["review"], "Public comment")
            self.assertTrue(saved["trace"]["stage_outputs"]["tests"]["truncated"])
            self.assertLessEqual(len(saved["trace"]["stage_outputs"]["tests"]["text"].encode()),
                                 trace.MAX_PUBLIC_STAGE_OUTPUT_BYTES)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_review_metrics_sum_stage_turns_and_price_missing_cache_write_conservatively(self):
        def turn(input_tokens, cached_tokens, cache_write_tokens,
                 output_tokens, elapsed):
            return {"request_bytes": 100, "input_tokens": input_tokens,
                    "cached_tokens": cached_tokens,
                    "cache_write_tokens": cache_write_tokens,
                    "output_tokens": output_tokens,
                    "elapsed_seconds": elapsed}

        debug = {"stages": {
            "router": {"model": "gpt-6-luna", "turns": [
                turn(100, 20, None, 5, 1.0)], "tools": []},
            "independent": {"model": "gpt-6-luna", "turns": [
                turn(100, 20, 10, 5, 2.0), turn(100, 20, 10, 5, 3.0)],
                "tools": [{"name": "read_file"}, {"name": "search_code"}]},
            "tests": {"model": "gpt-6-luna", "status": "completed",
                      "turns": [turn(100, 20, 10, 5, 6.0)], "tools": []},
            "verifier": {"model": "gpt-6-luna", "turns": [
                turn(1000, 100, 0, 200, 4.0)], "tools": []},
            "collator": {"model": "gpt-6-luna", "turns": [
                turn(500, 0, 0, 4000, 5.0)], "tools": []},
            "state": {"status": "skipped", "turns": [], "tools": []},
        }}

        metrics = trace.review_metrics(debug, self.prompt_config)
        self.assertEqual(metrics["model_turns"], 6)
        self.assertEqual(metrics["tool_calls"], 2)
        self.assertEqual(metrics["audit_calls"], 1)
        self.assertEqual(metrics["total_input_tokens"], 1900)
        self.assertEqual(metrics["total_output_tokens"], 4220)
        self.assertEqual(metrics["estimated_cost_usd"], 0.002287)
        self.assertEqual(metrics["incomplete_usage_count"], 1)
        self.assertFalse(metrics["usage_complete"])

    def test_review_body_starts_with_commit_ids(self):
        base = "b" * 40
        head = "a" * 40
        body = forgejo.review_body(self.bot_config, self.prompt_config, base, head, "No findings.")
        self.assertTrue(body.startswith(
            f"{self.bot_config.comment_marker}\nBase: `{base}`  \nHead: `{head}`\n\n"))
        self.assertNotIn("First-pass review", body)
        self.assertTrue(forgejo.comment_matches_head({"body": body}, head))

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
            files = repository.tracked_files(checkout)
            self.assertIn("code.cpp", files)
            self.assertNotIn("link.cpp", files)
            self.assertIn("2: void caller()", repository.read_file(checkout, files, "code.cpp", 2))
            self.assertIn("Only tracked", repository.read_file(checkout, files, "../code.cpp", 1))
            self.assertIn("Only tracked", repository.read_file(checkout, files, "link.cpp", 1))
            self.assertIn("HEAD:code.cpp:1:void target", repository.search_code(checkout, "HEAD", "target"))

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
            base = repository.git(checkout, "rev-parse", "HEAD").strip()
            (checkout / "code.cpp").write_text("new behavior\nunchanged\n")
            subprocess.run(["git", "-C", directory, "add", "code.cpp"], check=True)
            subprocess.run(commit + ["change"], check=True)

            files = repository.tracked_files(checkout)
            base_files = repository.tracked_files_at(checkout, base)
            changed = set(repository.git(checkout, "diff", "--name-only", "-z",
                                  f"{base}..HEAD").split("\x00"))
            self.assertEqual(repository.find_paths(files, "CODE"), "code.cpp\n")
            self.assertEqual(repository.find_paths(files, "missing"), "No matching tracked files.")
            self.assertNotIn("link.cpp", base_files)
            self.assertIn("1: old behavior", repository.read_file(checkout, base_files,
                                                           "code.cpp", 1))
            self.assertIn("1: new behavior", repository.read_file(checkout, files,
                                                           "code.cpp", 1))
            diff = repository.read_diff(checkout, changed, base, "HEAD", "code.cpp", 1)
            self.assertIn("-old behavior", diff)
            self.assertIn("+new behavior", diff)
            self.assertIn("No diff lines", repository.read_diff(checkout, changed, base, "HEAD",
                                                         "code.cpp", 100))
            self.assertIn("Invalid", repository.read_diff(checkout, changed, base, "HEAD",
                                                   "link.cpp", 1))


    def test_snapshot_retains_both_paths_of_a_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            repository.git(checkout, "init", "-q")
            (checkout / "before.cpp").write_text("unchanged content\n")
            repository.git(checkout, "add", ".")
            commit = ("-c", "user.name=Test", "-c", "user.email=test@example.com",
                      "commit", "-qm")
            repository.git(checkout, *commit, "base")
            base = repository.git(checkout, "rev-parse", "HEAD").strip()
            repository.git(checkout, "mv", "before.cpp", "after.cpp")
            repository.git(checkout, *commit, "rename")
            head = repository.git(checkout, "rev-parse", "HEAD").strip()

            snapshot = repository.snapshot_repository(checkout, base, head)
            self.assertEqual(snapshot.changed_paths, {"before.cpp", "after.cpp"})
            self.assertIn("before.cpp", snapshot.base_files)
            self.assertIn("after.cpp", snapshot.head_files)

    def test_publish_creates_then_edits_one_bot_comment(self):
        comments = [{"id": 1, "user": {"login": "someone-else"},
                     "body": "Unrelated comment"}]
        calls = []

        def request(bot_config, token, path, method="GET", data=None):
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

        with patch.object(forgejo, "forgejo_request", side_effect=request), \
                patch.object(forgejo, "current_head", return_value="a" * 40):
            args = ("token", 42, "review-bot", "b" * 40, "a" * 40)
            self.assertEqual(forgejo.publish_review(self.bot_config, self.prompt_config, *args, "First review."), "created")
            self.assertEqual(forgejo.publish_review(self.bot_config, self.prompt_config, *args, "First review."), "unchanged")
            self.assertEqual(forgejo.publish_review(self.bot_config, self.prompt_config, *args, "Updated review."), "updated")
        self.assertEqual([method for _, method, _ in calls if method != "GET"],
                         ["POST", "PATCH"])
        self.assertEqual(comments[-1]["id"], 2)
        self.assertIn("Updated review.", comments[-1]["body"])

    def test_foreign_marker_prevents_duplicate_comment(self):
        with patch.object(forgejo, "forgejo_request", return_value=[
            {"id": 1, "user": {"login": "someone-else"},
             "body": self.bot_config.comment_marker}]) as request:
            with self.assertRaisesRegex(ValueError, "another user"):
                forgejo.find_comment(self.bot_config, "token", 42, "review-bot")
        self.assertEqual(request.call_count, 1)

    def test_find_comment_stops_when_mirror_ignores_page(self):
        comments = [{"id": i, "user": {"login": "someone-else"}, "body": ""}
                    for i in range(65)]

        def request(bot_config, token, path):
            if "page=3" in path:
                self.fail("Repeated mirror page caused another request")
            return comments

        with patch.object(forgejo, "forgejo_request", side_effect=request) as get:
            self.assertIsNone(forgejo.find_comment(self.bot_config, "token", 42, "review-bot"))
        self.assertEqual(get.call_count, 2)

    def test_publish_finds_comment_on_later_page(self):
        existing = {"id": 73, "user": {"login": "review-bot"},
                    "body": self.bot_config.comment_marker + "\nOld review"}
        calls = []

        def request(bot_config, token, path, method="GET", data=None):
            calls.append((path, method))
            if "page=1" in path:
                return [{"user": {"login": "someone-else"},
                         "body": self.bot_config.comment_marker}] * 50
            if "page=2" in path:
                return [existing]
            if method == "PATCH":
                return {"id": 73}
            self.fail(f"Unexpected API call {path}")

        with patch.object(forgejo, "forgejo_request", side_effect=request), \
                patch.object(forgejo, "current_head", return_value="a" * 40):
            self.assertEqual(forgejo.publish_review(self.bot_config, self.prompt_config, "token", 42, "review-bot",
                                                "b" * 40, "a" * 40, "Review"),
                             "updated")
        self.assertIn(("/issues/comments/73", "PATCH"), calls)

    def test_stale_head_does_not_publish(self):
        calls = []

        def request(bot_config, token, path, method="GET", data=None):
            calls.append((path, method))
            if path.startswith("/issues/42/comments"):
                return []
            self.fail(f"Unexpected API call {path}")

        with patch.object(forgejo, "forgejo_request", side_effect=request), \
                patch.object(forgejo, "current_head", return_value="c" * 40):
            self.assertEqual(forgejo.publish_review(self.bot_config, self.prompt_config, "token", 42, "review-bot",
                                                "b" * 40, "a" * 40, "Review"),
                             "stale")
        self.assertTrue(all(method == "GET" for _, method in calls))

    def test_current_head_reads_fixed_git_ref(self):
        class Result:
            stdout = "a" * 40 + "\trefs/pull/42/head\n"

        with patch.object(repository.subprocess, "run", return_value=Result()) as run:
            self.assertEqual(repository.current_head(self.bot_config, 42), "a" * 40)
        self.assertEqual(run.call_args.args[0],
                         ["git", "ls-remote", self.bot_config.origin, "refs/pull/42/head"])


if __name__ == "__main__":
    unittest.main()
