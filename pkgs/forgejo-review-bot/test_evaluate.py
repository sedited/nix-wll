import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forgejo_review_bot import config, evaluate, forgejo, pipeline, spend


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin.git"
        self.work = self.root / "work"
        self.checkout = self.root / "state" / "evaluation-checkout"
        self.git(self.root, "init", "--bare", str(self.origin))
        self.git(self.root, "init", str(self.work))
        self.git(self.work, "config", "user.name", "Test")
        self.git(self.work, "config", "user.email", "test@example.com")
        self.git(self.work, "remote", "add", "origin", str(self.origin))
        (self.work / "code.txt").write_text("base\n", encoding="utf-8")
        self.git(self.work, "add", "code.txt")
        self.git(self.work, "commit", "-qm", "Base")
        self.git(self.work, "push", "origin", "HEAD:refs/heads/master")
        (self.work / "code.txt").write_text("head\n", encoding="utf-8")
        self.git(self.work, "add", "code.txt")
        self.git(self.work, "commit", "-qm", "Change")
        self.git(self.work, "push", "origin", "HEAD:refs/pull/42/head")
        self.bot_config = config.BotConfig(str(self.origin), "example/repo",
            "https://git.example.org/api/v1/repos/example/repo")
        self.prompt_config = config.PromptConfig.load()

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def capture(self):
        details = {"title": "Pinned input", "body": "Review this.",
                   "base": {"ref": "master"},
                   "head": {"sha": self.git(self.work, "rev-parse", "HEAD")}}
        with patch.object(forgejo, "forgejo_request", return_value=details) as request:
            path = evaluate.capture_case("token", self.checkout, self.root / "cases", 42,
                                         self.bot_config, self.prompt_config)
        request.assert_called_once_with(self.bot_config, "token", "/pulls/42")
        return path

    def test_capture_pins_complete_git_objects_and_frozen_configuration(self):
        path = self.capture()
        case = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(case["checkout"], str(self.checkout.resolve()))
        self.assertEqual(case["head_sha"], self.git(self.work, "rev-parse", "HEAD"))
        self.assertEqual(case["merge_base"], case["base_sha"])
        self.assertIn("Pinned input", case["review_input"])
        self.assertIn("+head", case["review_input"])
        self.assertEqual(case["title"], "Pinned input")
        self.assertEqual(case["description"], "Review this.")
        self.assertEqual(case["config"]["prompts"]["models"], self.prompt_config.models)
        self.assertEqual(case["config_sha256"], evaluate.digest(case["config"]))
        self.assertEqual(case["source_code_sha256"], evaluate.source_code_sha256())
        self.assertEqual(self.git(self.checkout, "rev-parse", case["head_pin"]),
                         case["head_sha"])
        self.assertEqual(case["manifest_sha256"], evaluate.digest({
            key: value for key, value in case.items() if key != "manifest_sha256"}))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with evaluate.offline_git():
            evaluate.verify_local_objects(self.checkout.resolve(),
                                          case["base_sha"], case["head_sha"])

    def test_replay_needs_no_forgejo_or_remote_git_and_keeps_labels_separate(self):
        manifest = self.capture()
        case = json.loads(manifest.read_text(encoding="utf-8"))
        labels = {case["case_id"]: ["Expected issue held out of model input"]}
        self.git(self.work, "commit", "--allow-empty", "-qm", "New PR head")
        self.git(self.work, "push", "--force", "origin", "HEAD:refs/pull/42/head")
        self.git(self.checkout, "fetch", "origin",
                 "+refs/pull/42/head:refs/review-bot/head")
        self.git(self.checkout, "reflog", "expire", "--expire=now", "--all")
        self.git(self.checkout, "gc", "--prune=now")
        self.assertEqual(self.git(self.checkout, "rev-parse", case["head_pin"]),
                         case["head_sha"])
        self.origin.rename(self.root / "unavailable-origin.git")
        seen = []

        def review(api_key, review_input, snapshot, bot_config, prompt_config,
                   current_pr, debug, **kwargs):
            self.assertEqual(review_input, case["review_input"])
            self.assertNotIn("Expected issue", review_input)
            self.assertEqual(snapshot.head_sha, case["head_sha"])
            self.assertEqual(snapshot.merge_base, case["merge_base"])
            self.assertEqual(prompt_config.models, self.prompt_config.models)
            self.assertIs(kwargs["allow_discussions"], False)
            self.assertEqual(kwargs["routing_mode"], "shadow")
            debug["stage_outputs"] = {"verifier": "raw verifier"}
            seen.append(snapshot)
            return "Final review."

        with patch.object(forgejo, "forgejo_request", side_effect=AssertionError("Forgejo used")), \
                patch.object(pipeline, "review_with_independent_passes", side_effect=review):
            first = evaluate.run_case(manifest, "openai-key", self.root / "runs", object(),
                                      routing_mode="shadow", labels=labels)
            second = evaluate.run_case(manifest, "openai-key", self.root / "runs", object(),
                                       routing_mode="shadow", labels=labels)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(first, second)
        self.assertIsNone(os.environ.get("GIT_NO_LAZY_FETCH"))
        for path in (first, second):
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["raw_debug"]["stage_outputs"]["verifier"],
                             "raw verifier")
            self.assertEqual(result["expected_findings"], labels[case["case_id"]])
            self.assertIn("Final review.", result["final_comment"])
            self.assertEqual(result["effective_config_sha256"],
                             evaluate.digest(result["effective_config"]))
            self.assertEqual(result["effective_config"]["routing_mode"], "shadow")
            self.assertFalse(result["code_changed_since_capture"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_missing_objects_write_private_failure_artifact(self):
        manifest = self.capture()
        case = json.loads(manifest.read_text(encoding="utf-8"))
        empty = self.root / "empty"
        self.git(self.root, "init", str(empty))
        case["checkout"] = str(empty)
        case["manifest_sha256"] = evaluate.digest({
            key: value for key, value in case.items() if key != "manifest_sha256"})
        broken = self.root / "cases" / "broken.json"
        evaluate.write_private_json(broken, case)
        with patch.object(forgejo, "forgejo_request", side_effect=AssertionError("Forgejo used")), \
                patch.object(pipeline, "review_with_independent_passes") as review:
            path = evaluate.run_case(broken, "openai-key", self.root / "runs", object())
        review.assert_not_called()
        result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("Git objects are unavailable", result["error"]["message"])
        self.assertEqual(result["raw_debug"], {})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_failed_model_call_keeps_raw_debug(self):
        manifest = self.capture()

        ledger = spend.Ledger(self.root / "state" / "spend.sqlite3")

        def fail(api_key, review_input, snapshot, bot_config, prompt_config,
                 current_pr, debug, **kwargs):
            budget = kwargs["budget"]
            token = budget.reserve("router", {"model": "gpt-6-luna",
                                              "max_output_tokens": 100})
            budget.fail(token, charged_unknown=True)
            debug["pipeline_stage"] = "verification"
            debug["stage_outputs"] = {"independent": "candidate text"}
            raise RuntimeError("model response failed")

        with patch.object(pipeline, "review_with_independent_passes", side_effect=fail):
            path = evaluate.run_case(manifest, "openai-key", self.root / "runs", ledger)
        result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["type"], "RuntimeError")
        self.assertEqual(result["raw_debug"]["pipeline_stage"], "verification")
        self.assertEqual(result["raw_debug"]["stage_outputs"]["independent"],
                         "candidate text")
        self.assertTrue(result["review_id"].startswith("eval:"))
        self.assertEqual(result["budget"]["unknown_request_count"], 1)

    def test_model_override_changes_effective_config_hash(self):
        manifest = self.capture()
        frozen = json.loads(manifest.read_text(encoding="utf-8"))
        models = dict(self.prompt_config.models)
        models["verifier"] = "gpt-6.1-sol"
        override = self.root / "models.json"
        override.write_text(json.dumps(models), encoding="utf-8")

        def review(api_key, review_input, snapshot, bot_config, prompt_config,
                   current_pr, debug, **kwargs):
            self.assertEqual(review_input, frozen["review_input"])
            self.assertEqual(prompt_config.models["verifier"], "gpt-6.1-sol")
            return "Review with override."

        with patch.object(pipeline, "review_with_independent_passes", side_effect=review):
            path = evaluate.run_case(manifest, "openai-key", self.root / "runs",
                                     object(), models_json=override)
        result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "completed")
        self.assertNotEqual(result["effective_config_sha256"], frozen["config_sha256"])
        self.assertIn(result["effective_config_sha256"][:12], path.name)

    def test_old_frozen_prompt_shape_needs_complete_explicit_override(self):
        manifest = self.capture()
        frozen = json.loads(manifest.read_text(encoding="utf-8"))
        old_prompts = frozen["config"]["prompts"]
        old_prompts["models"].pop("build", None)
        old_prompts["audit_prompts"].pop("build", None)
        old_prompts["audit_prompts"].pop("consensus", None)
        old_prompts["audit_prompts"].pop("wallet", None)
        old_prompts["audit_prompts"].pop("p2p", None)
        frozen["config_sha256"] = evaluate.digest(frozen["config"])
        frozen["manifest_sha256"] = evaluate.digest({
            key: value for key, value in frozen.items() if key != "manifest_sha256"})
        old_manifest = self.root / "cases" / "old-shape.json"
        evaluate.write_private_json(old_manifest, frozen)

        with patch.object(pipeline, "review_with_independent_passes") as review:
            failed = evaluate.run_case(old_manifest, "openai-key", self.root / "runs",
                                       object())
        review.assert_not_called()
        result = json.loads(failed.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("pass --audit-prompt-dir", result["error"]["message"])

        audit_dir = self.root / "audits"
        audit_dir.mkdir()
        for name in config.PROMPT_NAMES:
            (audit_dir / f"{name}.md").write_text(f"{name} prompt\n", encoding="utf-8")
        models_json = self.root / "models.json"
        models_json.write_text(json.dumps({
            name: "gpt-6-luna" for name in config.MODEL_NAMES}), encoding="utf-8")

        def review(api_key, review_input, snapshot, bot_config, prompt_config,
                   current_pr, debug, **kwargs):
            self.assertEqual(set(prompt_config.audit_prompts), set(config.PROMPT_NAMES))
            self.assertEqual(set(prompt_config.models), set(config.MODEL_NAMES))
            return "Review with complete override."

        with patch.object(pipeline, "review_with_independent_passes", side_effect=review):
            completed = evaluate.run_case(old_manifest, "openai-key", self.root / "runs",
                                          object(), audit_dir=audit_dir,
                                          models_json=models_json)
        result = json.loads(completed.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "completed")
        self.assertIn("Review with complete override.", result["final_comment"])

    def test_routing_budget_and_code_each_identify_a_distinct_run(self):
        manifest = self.capture()
        ledger = spend.Ledger(self.root / "state" / "spend.sqlite3",
                              review_limit_usd=0.60)
        smaller = spend.Ledger(self.root / "state" / "spend.sqlite3",
                               review_limit_usd=0.40)
        with patch.object(pipeline, "review_with_independent_passes",
                          return_value="Review."):
            standard = evaluate.run_case(manifest, "key", self.root / "runs", ledger)
            routed = evaluate.run_case(manifest, "key", self.root / "runs", ledger,
                                       routing_mode="full")
            lower_budget = evaluate.run_case(manifest, "key", self.root / "runs", smaller)
            with patch.object(evaluate, "source_code_sha256", return_value="f" * 64):
                revised_code = evaluate.run_case(manifest, "key", self.root / "runs", ledger)
        results = [json.loads(path.read_text(encoding="utf-8"))
                   for path in (standard, routed, lower_budget, revised_code)]
        self.assertEqual(len({item["effective_config_sha256"] for item in results}), 4)
        self.assertEqual(results[2]["effective_config"]["review_budget_usd"], 0.40)
        self.assertTrue(results[3]["code_changed_since_capture"])

    def test_spend_cli_reads_local_ledger_without_keys(self):
        state = self.root / "state"
        spend.Ledger(state / "spend.sqlite3")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(evaluate.main(["spend", "--state-dir", str(state)]), 0)
        self.assertIn("estimated_total_usd", json.loads(output.getvalue()))

    def test_capture_and_run_cli_keep_credentials_separate(self):
        token_file = self.root / "forgejo-token"
        key_file = self.root / "openai-key"
        token_file.write_text("forgejo-token\n", encoding="utf-8")
        key_file.write_text("openai-key\n", encoding="utf-8")
        details = {"title": "Pinned input", "body": "Review this.",
                   "base": {"ref": "master"},
                   "head": {"sha": self.git(self.work, "rev-parse", "HEAD")}}
        output = io.StringIO()
        with patch.object(forgejo, "forgejo_request", return_value=details), \
                contextlib.redirect_stdout(output):
            self.assertEqual(evaluate.main([
                "capture", "--state-dir", str(self.root / "state"),
                "--output-dir", str(self.root / "cases"),
                "--origin", str(self.origin), "--repository", "example/repo",
                "--forgejo-api", self.bot_config.forgejo_api,
                "--forgejo-token-file", str(token_file), "42",
            ]), 0)
        manifest = Path(output.getvalue().strip())
        self.origin.rename(self.root / "unavailable-origin.git")
        output = io.StringIO()
        with patch.object(forgejo, "forgejo_request", side_effect=AssertionError("Forgejo used")), \
                patch.object(pipeline, "review_with_independent_passes",
                             return_value="Local replay."), \
                contextlib.redirect_stdout(output):
            self.assertEqual(evaluate.main([
                "run", "--state-dir", str(self.root / "state"),
                "--output-dir", str(self.root / "runs"),
                "--openai-key-file", str(key_file), str(manifest),
            ]), 0)
        artifact = json.loads(Path(output.getvalue().strip()).read_text(encoding="utf-8"))
        self.assertEqual(artifact["status"], "completed")
        self.assertIn("Local replay.", artifact["final_comment"])


if __name__ == "__main__":
    unittest.main()
