import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
import evaluate


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.prompts = bot.AUDIT_PROMPTS
        self.models = bot.MODELS
        bot.configure(
            "https://git.fish.foo/bitcoin/bitcoin.git",
            "bitcoin/bitcoin",
            "https://git.fish.foo/api/v1/repos/bitcoin/bitcoin",
        )

    def tearDown(self):
        bot.AUDIT_PROMPTS = self.prompts
        bot.MODELS = self.models

    def test_replay_writes_private_artifact_without_publishing(self):
        models = {name: "gpt-6.1-sol" if name in ("independent", "adversarial")
                  else "gpt-6-luna" for name in bot.MODEL_NAMES}
        prompts = {name: f"{name} prompt" for name in
                   ("common", "router", "adversarial", *bot.AUDIT_NAMES,
                    "verifier", "collator")}
        calls = []

        def request(token, path, method="GET", data=None):
            calls.append((path, method, data))
            self.assertEqual(method, "GET")
            self.assertIsNone(data)
            self.assertEqual(token, "forgejo-token")
            self.assertEqual(path, "/pulls/42")
            return {"title": "Use deterministic review",
                    "body": "Replay the current head.",
                    "base": {"ref": "master"},
                    "head": {"sha": "d" * 40}}

        def collect(checkout, number, base_ref, expected_head, title, description):
            self.assertEqual(checkout, Path("/state/checkout"))
            self.assertEqual(number, 42)
            self.assertEqual(base_ref, "master")
            self.assertEqual(expected_head, "a" * 40)
            self.assertEqual(title, "Use deterministic review")
            self.assertEqual(description, "Replay the current head.")
            return "b" * 40, "a" * 40, "review input", None

        def review(api_key, review_input, checkout, debug,
                   prompt=None, model=None, stage="independent"):
            self.assertEqual(api_key, "openai-key")
            self.assertEqual(checkout, Path("/state/checkout"))
            if prompt is None:
                self.assertEqual(review_input, "review input")
                return "independent output"
            if prompt == "adversarial prompt":
                self.assertEqual(review_input, "review input")
                return "adversarial output"
            if prompt == "verifier prompt":
                self.assertIn("independent output", review_input)
                return "verifier output"
            self.fail(f"unexpected prompt {prompt!r}")

        def audit(api_key, name, prompt, review_input, notes=""):
            if name == "collator":
                self.assertIn("verifier output", review_input)
                return "final review text", {
                    "name": "collator", "model": "gpt-6-luna",
                    "status": "completed", "output_truncated": False}
            return f"{name} output", {
                "name": name, "model": "gpt-6-luna",
                "status": "completed", "output_truncated": False}

        def focused(api_key, name, prompt, review_input, checkout):
            return f"{name} output", {
                "name": name, "model": "gpt-6-luna",
                "status": "completed", "output_truncated": False}

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "artifacts"
            with patch.object(bot, "forgejo_request", side_effect=request), \
                    patch.object(bot, "current_head", return_value="a" * 40), \
                    patch.object(bot, "collect_review", side_effect=collect), \
                    patch.object(bot, "audit_prompts", return_value=prompts), \
                    patch.object(bot, "stage_models", return_value=models), \
                    patch.object(bot, "audit_developer_notes", return_value="notes"), \
                    patch.object(bot, "codex_stage_review", side_effect=review), \
                    patch.object(bot, "run_audit", side_effect=audit), \
                    patch.object(bot, "run_focused_review", side_effect=focused), \
                    patch.object(bot, "route_adversarial", return_value=True), \
                    patch.object(bot, "publish_review") as publish:
                artifact = evaluate.replay_pull_request(
                    "openai-key", "forgejo-token", Path("/state/checkout"),
                    evaluate.private_dir(output_dir), 42)

            publish.assert_not_called()
            self.assertEqual(calls, [("/pulls/42", "GET", None)])
            self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
            data = json.loads(artifact.read_text(encoding="utf-8"))

        self.assertEqual(data["expected_head"], "a" * 40)
        self.assertEqual(data["api_head"], "d" * 40)
        self.assertEqual(data["base_sha"], "b" * 40)
        self.assertEqual(data["head_sha"], "a" * 40)
        self.assertEqual(data["stage_outputs"]["independent"], "independent output")
        self.assertEqual(data["stage_outputs"]["adversarial"], "adversarial output")
        self.assertEqual(data["stage_outputs"]["verifier"], "verifier output")
        self.assertEqual(data["verifier"], "verifier output")
        self.assertEqual(data["stage_outputs"]["collator"], "final review text")
        for name in bot.AUDIT_NAMES:
            self.assertEqual(data["stage_outputs"][name], f"{name} output")
        self.assertIn("final review text", data["final_comment"])
        self.assertIn("Head: `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`",
                      data["final_comment"])

    def test_main_accepts_multiple_prs_and_explicit_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "openai-key"
            token = root / "forgejo-token"
            prompt = root / "prompt.md"
            audit_dir = root / "audits"
            key.write_text("openai-key\n", encoding="utf-8")
            token.write_text("forgejo-token\n", encoding="utf-8")
            prompt.write_text("prompt\n", encoding="utf-8")
            audit_dir.mkdir()
            for name in ("common", "router", "adversarial", *bot.AUDIT_NAMES,
                         "verifier", "collator"):
                (audit_dir / f"{name}.md").write_text(f"{name}\n", encoding="utf-8")
            models = {name: "gpt-6.1-sol" if name in ("independent", "adversarial")
                      else "gpt-6-luna" for name in bot.MODEL_NAMES}
            (audit_dir / "models.json").write_text(json.dumps(models), encoding="utf-8")
            output_dir = root / "out"
            models_override = root / "models-override.json"
            override = dict(models)
            override["verifier"] = "gpt-6.1-sol"
            models_override.write_text(json.dumps(override), encoding="utf-8")
            seen = []

            def replay(api_key, forgejo_token, checkout, out, number):
                seen.append((api_key, forgejo_token, checkout, out, number))
                return out / f"pr-{number}.json"

            stdout = io.StringIO()
            with patch.object(evaluate, "replay_pull_request", side_effect=replay), \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(evaluate.main([
                    "--state-dir", str(root / "state"),
                    "--output-dir", str(output_dir),
                    "--origin", "https://git.fish.foo/bitcoin/bitcoin.git",
                    "--repository", "bitcoin/bitcoin",
                    "--forgejo-api", "https://git.fish.foo/api/v1/repos/bitcoin/bitcoin",
                    "--openai-key-file", str(key),
                    "--forgejo-token-file", str(token),
                    "--prompt-file", str(prompt),
                    "--audit-prompt-dir", str(audit_dir),
                    "--models-json", str(models_override),
                    "7", "8",
                ]), 0)

        self.assertEqual([item[-1] for item in seen], [7, 8])
        self.assertTrue(all(item[0] == "openai-key" for item in seen))
        self.assertTrue(all(item[1] == "forgejo-token" for item in seen))
        self.assertTrue(all(item[2].name == "checkout" for item in seen))
        self.assertEqual(bot.stage_models()["verifier"], "gpt-6.1-sol")


if __name__ == "__main__":
    unittest.main()
