import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forgejo_review_bot import config, evaluate, forgejo, model, pipeline, repository, trace


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.bot_config = config.BotConfig(
            "https://git.fish.foo/bitcoin/bitcoin.git",
            "bitcoin/bitcoin",
            "https://git.fish.foo/api/v1/repos/bitcoin/bitcoin",
        )
        self.prompt_config = config.PromptConfig.load()

    def test_replay_writes_private_artifact_without_publishing(self):
        details = {"base_ref": "master", "api_head": "a" * 40,
                   "title": "Title", "description": "Body"}

        def review(*args):
            args[6]["stage_outputs"] = {"verifier": "checked"}
            return "Final review."

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            with patch.object(evaluate, "pull_request_details", return_value=details), \
                    patch.object(repository, "current_head", return_value="a" * 40), \
                    patch.object(repository, "collect_review", return_value=(
                        "b" * 40, "a" * 40, "patch", None)), \
                    patch.object(repository, "snapshot_repository"), \
                    patch.object(pipeline, "review_with_independent_passes", side_effect=review), \
                    patch.object(forgejo, "publish_review") as publish:
                artifact = evaluate.replay_pull_request(
                    "key", "token", Path("/unused"), output, 42,
                    self.bot_config, self.prompt_config)
            publish.assert_not_called()
            self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
            data = json.loads(artifact.read_text())
        self.assertEqual(data["head_sha"], "a" * 40)
        self.assertEqual(data["verifier"], "checked")
        self.assertIn("Final review.", data["final_comment"])

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
            for name in ("common", "router", "adversarial", *config.AUDIT_NAMES,
                         "verifier", "collator"):
                (audit_dir / f"{name}.md").write_text(f"{name}\n", encoding="utf-8")
            models = {name: "gpt-6.1-sol" if name in ("independent", "adversarial")
                      else "gpt-6-luna" for name in config.MODEL_NAMES}
            (audit_dir / "models.json").write_text(json.dumps(models), encoding="utf-8")
            output_dir = root / "out"
            models_override = root / "models-override.json"
            override = dict(models)
            override["verifier"] = "gpt-6.1-sol"
            models_override.write_text(json.dumps(override), encoding="utf-8")
            seen = []

            def replay(api_key, forgejo_token, checkout, out, number, bot_config, prompt_config):
                seen.append((api_key, forgejo_token, checkout, out, number,
                             bot_config, prompt_config))
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

        self.assertEqual([item[4] for item in seen], [7, 8])
        self.assertTrue(all(item[0] == "openai-key" for item in seen))
        self.assertTrue(all(item[1] == "forgejo-token" for item in seen))
        self.assertTrue(all(item[2].name == "checkout" for item in seen))
        self.assertEqual(seen[0][-1].models["verifier"], "gpt-6.1-sol")


if __name__ == "__main__":
    unittest.main()
