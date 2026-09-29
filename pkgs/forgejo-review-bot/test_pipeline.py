import json
import unittest
from pathlib import Path
from unittest.mock import patch

from forgejo_review_bot import config, model, pipeline, repository

COMPLETE = {"status": "complete", "limitations": []}


def discovery(findings=(), sensitive=False):
    return json.dumps({"coverage": COMPLETE, "findings": list(findings),
                       "requires_sensitive_review": sensitive})


def candidate(title="Simplify fixture"):
    return {"kind": "suggestion", "path": "src/example.cpp", "line": 3, "side": "head",
            "title": title, "claim": "Duplicate setup", "consequence": "Two fixtures to maintain",
            "evidence": "The same setup is repeated", "correction": "Reuse the fixture",
            "uncertainty": ""}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.config = config.BotConfig("https://example.invalid/repo.git", "o/r",
                                      "https://example.invalid/api/v1/repos/o/r")
        self.prompts = config.PromptConfig.load()
        self.snapshot = repository.RepositorySnapshot(
            Path("/unused"), "b"*40, "a"*40, "b"*40,
            {"src/example.cpp": "blob"}, {"src/example.cpp": "base"},
            frozenset({"src/example.cpp"}))
        self.published = {"severity": "suggestion", "path": "src/example.cpp", "line": 3,
                          "side": "head", "title": "Simplify fixture",
                          "body": "Reuse the fixture while preserving the regression check."}
        self.debug = {}
        self.calls = []

    def plan(self, *args, **kwargs):
        result = {"tier": self.tier, "audits": self.audits, "evidence": [], "missing_context": []}
        args[4]["routing"] = {"selected": result}
        return result

    def edit(self, api_key, name, prompt, input_text, prompt_config, **kwargs):
        self.assertEqual(name, "collator")
        findings = json.loads(input_text)["findings"]
        self.assertNotIn("Rejected claim", input_text)
        return json.dumps({"findings": [
            {"id": item["id"], "title": item["title"], "body": item["body"]} for item in findings
        ]}), {"status": "completed"}

    def run_review(self, reviewer, editor=None):
        with patch.object(pipeline.routing, "plan_review", side_effect=self.plan), \
                patch.object(model, "openai_review", side_effect=reviewer), \
                patch.object(model, "run_audit", side_effect=editor or self.edit), \
                patch.object(pipeline, "audit_developer_notes", return_value="Policy"):
            return pipeline.review_with_independent_passes(
                "key", "PR diff", self.snapshot, self.config, self.prompts, 42, self.debug)

    def test_routine_review_keeps_luna_editor_and_sends_only_accepted_findings(self):
        self.tier, self.audits = "standard", ["tests", "design"]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            self.calls.append((stage, kwargs["model"]))
            if stage == "design":
                return discovery([candidate()])
            if stage == "tests":
                return discovery([candidate("Rejected claim")])
            if stage == "verifier":
                return json.dumps({"coverage": COMPLETE, "decisions": [
                    {"candidate_ids": ["design:1"], "disposition": "publish",
                     "reason": "Useful simplification", "finding": self.published},
                    {"candidate_ids": ["tests:1"], "disposition": "drop",
                     "reason": "Guard already covers it", "finding": None}]})
            return discovery()

        content = self.run_review(review)
        self.assertIn("Reuse the fixture", content)
        self.assertTrue(all(value == "gpt-6-luna" for _, value in self.calls))
        self.assertEqual([name for name, _ in self.calls], ["independent", "design", "tests", "verifier"])
        self.assertIn("Rejected claim", self.debug["stage_outputs"]["tests"])
        self.assertEqual(self.debug["coverage"]["status"], "complete")

    def test_sensitive_discovery_uses_sol_and_failed_audit_keeps_partial_usage(self):
        self.tier, self.audits = "sensitive", ["design"]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            self.calls.append((stage, kwargs["model"]))
            if stage == "design":
                args[5]["turns"].append({"input_tokens": 100, "output_tokens": 10})
                raise TimeoutError()
            if stage == "verifier":
                return json.dumps({"coverage": COMPLETE, "decisions": []})
            return discovery()

        content = self.run_review(review)
        self.assertIn(("adversarial", "gpt-6.1-sol"), self.calls)
        self.assertIn(("verifier", "gpt-6-luna"), self.calls)
        self.assertIn("incomplete", content)
        self.assertEqual(self.debug["stages"]["design"]["turns"][0]["input_tokens"], 100)

    def test_specialist_can_escalate_after_router_and_overview(self):
        self.tier, self.audits = "standard", ["public_contract"]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            self.calls.append(stage)
            if stage == "verifier":
                return json.dumps({"coverage": COMPLETE, "decisions": []})
            return discovery(sensitive=stage == "public_contract")

        self.run_review(review)
        self.assertEqual(self.calls.count("adversarial"), 1)
        self.assertIn("state", self.calls)

    def test_missing_verifier_decision_never_publishes_candidate(self):
        self.tier, self.audits = "routine", []

        def review(*args, **kwargs):
            return (json.dumps({"coverage": COMPLETE, "decisions": []})
                    if kwargs["stage_name"] == "verifier" else discovery([candidate()]))

        content = self.run_review(review)
        self.assertNotIn("Simplify fixture", content)
        self.assertIn("Verifier output failed validation", content)
        self.assertEqual(self.debug["stages"]["verifier"]["status"], "invalid")
        self.assertIn("omitted", self.debug["stages"]["verifier"]["validation_error"])

    def test_partial_specialist_and_invalid_finding_preserve_valid_suggestion(self):
        self.tier, self.audits = "standard", ["tests"]

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            if stage == "tests":
                result = json.loads(discovery([candidate()]))
                result["coverage"] = {"status": "partial", "limitations": ["Caller not inspected"]}
                return json.dumps(result)
            if stage == "verifier":
                return json.dumps({"coverage": COMPLETE, "decisions": [
                    {"candidate_ids": ["tests:1"], "disposition": "publish",
                     "reason": "Useful simplification", "finding": self.published},
                    {"candidate_ids": [], "disposition": "publish",
                     "reason": "Policy requires release notes", "finding": {
                         **self.published, "path": "doc/developer-notes.md"}}]})
            return discovery()

        content = self.run_review(review)
        self.assertIn(self.published["body"], content)
        self.assertIn("withheld", content)
        self.assertNotIn("Verification did not complete", content)
        self.assertEqual(self.debug["coverage"]["status"], "partial")
        self.assertIn("doc/developer-notes.md",
                      self.debug["stages"]["verifier"]["validation_errors"][0]["error"])
        self.assertIn("doc/developer-notes.md", self.debug["stage_outputs"]["verifier"])

    def test_invalid_editor_preserves_verified_wording(self):
        self.tier, self.audits = "routine", []

        def review(*args, **kwargs):
            return (json.dumps({"coverage": COMPLETE, "decisions": [
                {"candidate_ids": ["independent:1"], "disposition": "publish",
                 "reason": "Verified", "finding": self.published}]})
                if kwargs["stage_name"] == "verifier" else discovery([candidate()]))

        def edit(*args, **kwargs):
            return '{"findings":[]}', {"status": "completed"}

        content = self.run_review(review, edit)
        self.assertIn(self.published["body"], content)
        self.assertTrue(self.debug["stages"]["collator"]["used_verified_wording"])


if __name__ == "__main__":
    unittest.main()
