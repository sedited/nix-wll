import json
import unittest
from unittest.mock import patch

from ralph import model, pipeline, protocol
import test_pipeline as fixtures


class BlindAlternativesTests(unittest.TestCase):
    def test_default_alternatives_are_blind_and_publish_one_verified_superior_option(self):
        fixture = fixtures.PipelineTests()
        fixture.setUp()
        fixture.tier, fixture.audits = "standard", ["design"]
        concept = {**fixtures.assessment(), "assessment": "rework_approach",
                   "recommendation": "Keep the current workflow; the PR adds review cost without solving a missed check.",
                   "proposed_review": "would_stop",
                   "review_reason": "The current workflow already catches the conceptual objections."}
        option = concept["alternatives"][0]
        outputs = {}
        summary = concept["recommendation"] + " https://example.invalid/o/r/pulls/42"

        def review(*args, **kwargs):
            stage = kwargs["stage_name"]
            outputs[stage] = args[1]
            if stage == "archaeologist":
                return fixtures.archaeology(concept)
            if stage == "alternatives":
                self.assertEqual(json.loads(args[1]),
                                 {key: concept[key] for key in ("problem", "goal", "baseline")})
                self.assertEqual(kwargs["tools"], model.BASE_TOOLS)
                self.assertEqual(kwargs["model"], fixture.prompts.models["archaeologist"])
                return json.dumps({"coverage": {"status": "complete", "limitations": []},
                                   "alternatives": [option]})
            if stage == "verifier":
                data = json.loads(args[1].split("Verification input:\n")[1])
                self.assertEqual(data["blind_alternatives"], [option])
                return fixtures.verification(concept={
                    "disposition": "publish", "assessment": concept,
                    "reason": "Existing review checks already catch the repeated objections.",
                    "proposed_review": "would_stop", "review_reason": concept["review_reason"],
                })
            return fixtures.discovery()

        def edit(api_key, name, prompt, input_text, prompt_config, **kwargs):
            self.assertEqual(name, "collator")
            payload = json.loads(input_text)
            self.assertEqual(payload["concept_assessment"]["alternatives"], [option])
            return json.dumps({"concept_summary": summary, "findings": []}), {"status": "completed"}

        with patch.object(pipeline.routing, "plan_review", side_effect=fixture.plan), \
                patch.object(model, "openai_review", side_effect=review), \
                patch.object(model, "run_audit", side_effect=edit), \
                patch.object(pipeline, "audit_developer_notes", return_value="Policy"):
            content = pipeline.review_with_independent_passes(
                "key", "SECRET PR TITLE AND PATCH", fixture.snapshot, fixture.config,
                fixture.prompts, 42, fixture.debug)
        self.assertLess(list(outputs).index("alternatives"), list(outputs).index("independent"))
        for stage in ("independent", "design"):
            self.assertNotIn(option["concept"], outputs[stage])
        self.assertNotIn("SECRET", outputs["alternatives"])
        self.assertIn("Concept and approach", content)
        self.assertIn(summary, content)
        self.assertNotIn("Alternatives considered", content)
        self.assertNotIn(option["unresolved"], content)

    def test_blind_alternatives_allow_none_and_reject_claimed_thread_sources(self):
        empty = {"coverage": {"status": "complete", "limitations": []}, "alternatives": []}
        self.assertEqual(protocol.blind_alternatives(json.dumps(empty)), empty)
        for fields in ({"provenance": "current_pr"}, {"citations": ["https://example.org/pr"]}):
            option = {**fixtures.assessment()["alternatives"][0], **fields}
            with self.subTest(fields=fields), self.assertRaises(protocol.InvalidReview):
                protocol.blind_alternatives(json.dumps({**empty, "alternatives": [option]}))


if __name__ == "__main__":
    unittest.main()
