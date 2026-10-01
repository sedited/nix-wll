import json
import unittest
from types import SimpleNamespace

from ralph import protocol


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = SimpleNamespace(changed_paths={"src/example.cpp"},
                                        head_files={"src/example.cpp": "blob"},
                                        base_files={"src/example.cpp": "old"})
        self.finding = {"kind": "suggestion", "severity": "suggestion", "path": "src/example.cpp",
                        "line": 5, "side": "head", "title": "Consolidate duplicate setup",
                        "body": "Reuse the existing fixture while keeping the regression assertion."}
        self.coverage = {"status": "complete", "limitations": []}

    def verification(self, decisions, coverage=None, concept=None):
        return json.dumps({
            "coverage": coverage or self.coverage,
            "concept": concept or {"disposition": "no_concern",
                                   "reason": "No material concept concern",
                                   "assessment": None,
                                   "proposed_review": "continue",
                                   "review_reason": "The verifier found no material concept concern."},
            "decisions": decisions,
        })

    def collation(self, findings, concept_summary=None):
        return json.dumps({"concept_summary": concept_summary, "findings": findings})

    def concept_assessment(self, citations=None):
        return {
            "goal": "Reduce review friction for repeated manual checks.",
            "problem": "The proposal solves a narrow workflow problem.",
            "baseline": "Maintainers can keep the current workflow.",
            "delivered_benefit": "The PR removes one manual step.",
            "relevant_history": "Prior discussion raised a layering concern.",
            "assessment": "rework_approach",
            "alternatives": [{
                "name": "Do nothing",
                "concept": "Keep the current behavior.",
                "benefit": "No extra maintenance.",
                "cost": "The manual step remains.",
                "unresolved": "Impact is not measured.",
                "provenance": "agent_inference",
                "citations": [],
            }],
            "recommendation": "The alternative is conceptually stronger.",
            "proposed_review": "would_stop",
            "review_reason": "The alternative preserves the benefit with less maintenance cost.",
            "decisive_question": "Whether the manual step affects common review paths.",
            "technical_assumptions": ["Verifier should check the call path."],
            "citations": citations or ["https://code.example.org/org/repo/pulls/1"],
        }

    def test_verifier_groups_sources_and_preserves_every_candidate(self):
        candidates = [{"id": "tests:1"}, {"id": "design:1"}, {"id": "independent:1"}]
        decisions = [
            {"candidate_ids": ["tests:1", "design:1"], "disposition": "publish",
             "reason": "Same redundant fixture", "finding": self.finding},
            {"candidate_ids": ["independent:1"], "disposition": "unresolved",
             "reason": "Caller evidence missing", "finding": None},
        ]
        result, accepted = protocol.verification(
            self.verification(decisions, {"status": "complete",
                                          "limitations": ["Caller unavailable"]}),
            candidates, self.snapshot)
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(result["decisions"][1]["disposition"], "unresolved")
        decisions.pop()
        with self.assertRaisesRegex(protocol.InvalidReview, "omitted"):
            protocol.verification(self.verification(decisions), candidates, self.snapshot)

    def test_collator_cannot_add_remove_or_duplicate_accepted_ids(self):
        accepted = [{**self.finding, "id": "finding:1"},
                    {**self.finding, "kind": "design", "id": "finding:2"}]
        for edits in ([], [{"id": "invented", "title": "New", "body": "Claim"}],
                      [{"id": "finding:1", "title": "A", "body": "B"}] * 2):
            with self.subTest(edits=edits), self.assertRaises(protocol.InvalidReview):
                protocol.collation(self.collation(edits), accepted)
        edited, concept_summary = protocol.collation(self.collation([
            {"id": "finding:1", "title": "Short title", "body": "Edited wording"},
            {"id": "finding:2", "title": "Design title", "body": "Design wording"}]), accepted)
        self.assertIsNone(concept_summary)
        self.assertEqual(edited[0]["path"], self.finding["path"])
        self.assertEqual(edited[0]["severity"], "suggestion")
        self.assertEqual(edited[1]["kind"], "design")
        self.assertIn("incomplete", protocol.render(edited, ["A selected audit was unavailable."]))

    def test_render_orders_design_after_critical_and_before_other_findings(self):
        findings = [
            {**self.finding, "kind": "suggestion", "title": "General suggestion"},
            {**self.finding, "kind": "defect", "severity": "minor", "title": "Minor bug"},
            {**self.finding, "kind": "design", "title": "Design concern"},
            {**self.finding, "kind": "defect", "severity": "critical", "title": "Critical bug"},
        ]
        rendered = protocol.render(findings)
        self.assertEqual(rendered.count("Design concern"), 1)
        self.assertLess(rendered.index("Critical bug"), rendered.index("Design and approach"))
        self.assertLess(rendered.index("Design concern"), rendered.index("Minor bug"))
        self.assertLess(rendered.index("Minor bug"), rendered.index("General suggestion"))
        self.assertNotIn("Design and approach", protocol.render([findings[0]]))

    def test_verifier_withholds_invalid_kind(self):
        decision = {"candidate_ids": ["design:1"], "disposition": "publish",
                    "reason": "Verified", "finding": {**self.finding, "kind": "other"}}
        result, accepted = protocol.verification(
            self.verification([decision]),
            [{"id": "design:1"}], self.snapshot)
        self.assertEqual(accepted, [])
        self.assertIn("Invalid finding kind", result["validation_errors"][0]["error"])

    def test_finding_must_refer_to_changed_file_on_correct_side(self):
        decision = {"candidate_ids": ["tests:1"], "disposition": "publish",
                    "reason": "Verified", "finding": {**self.finding, "path": "missing.cpp"}}
        result, accepted = protocol.verification(
            self.verification([decision]),
            [{"id": "tests:1"}], self.snapshot)
        self.assertEqual(accepted, [])
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertIn("changed file", result["decisions"][0]["reason"])
        self.assertEqual(result["decisions"][0]["disposition"], "unresolved")
        self.assertIsNone(result["decisions"][0]["finding"])
        self.assertEqual(result["validation_errors"][0]["candidate_ids"], ["tests:1"])
        self.assertIn("missing.cpp", result["validation_errors"][0]["error"])

    def test_invalid_publish_finding_does_not_discard_valid_finding(self):
        candidates = [{"id": "tests:1"}, {"id": "design:1"}]
        valid = {"candidate_ids": ["tests:1"], "disposition": "publish",
                 "reason": "Verified", "finding": self.finding}
        invalid = {"candidate_ids": ["design:1"], "disposition": "publish",
                   "reason": "Verified", "finding": {**self.finding, "path": "missing.cpp"}}
        for decisions in ([valid, invalid], [invalid, valid]):
            with self.subTest(decisions=decisions):
                result, accepted = protocol.verification(
                    self.verification(decisions),
                    candidates, self.snapshot)
                self.assertEqual(accepted, [{**self.finding, "id": "finding:1"}])
                self.assertEqual(result["coverage"]["status"], "partial")
                self.assertTrue(any("changed file" in limit
                                    for limit in result["coverage"]["limitations"]))
                withheld = next(item for item in result["decisions"]
                                if item["candidate_ids"] == ["design:1"])
                self.assertEqual(withheld["disposition"], "unresolved")
                self.assertIsNone(withheld["finding"])
                self.assertIn("changed file", withheld["reason"])
                self.assertEqual(result["validation_errors"][0]["candidate_ids"], ["design:1"])

    def test_invalid_new_finding_without_candidate_ids_is_withheld(self):
        decision = {"candidate_ids": [], "disposition": "publish", "reason": "New issue",
                    "finding": {**self.finding, "path": "missing.cpp"}}
        result, accepted = protocol.verification(
            self.verification([decision]),
            [], self.snapshot)
        self.assertEqual(accepted, [])
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["decisions"][0]["disposition"], "unresolved")
        self.assertIsNone(result["decisions"][0]["finding"])
        self.assertEqual(result["validation_errors"][0]["candidate_ids"], [])

    def test_location_errors_identify_the_failed_check(self):
        cases = (
            ({"path": "missing.cpp"}, "changed file"),
            ({"side": "base"}, "base side"),
            ({"line": 0}, "positive line"),
            ({"side": "neither"}, "location side"),
        )
        self.snapshot.base_files = {}
        for change, message in cases:
            with self.subTest(change=change):
                decision = {"candidate_ids": ["tests:1"], "disposition": "publish",
                            "reason": "Verified", "finding": {**self.finding, **change}}
                result, accepted = protocol.verification(
                    self.verification([decision]),
                    [{"id": "tests:1"}], self.snapshot)
                self.assertEqual(accepted, [])
                self.assertIn(message, result["validation_errors"][0]["error"])

    def test_invalid_candidate_ids_still_reject_entire_verification(self):
        candidates = [{"id": "tests:1"}, {"id": "design:1"}]
        valid = {"candidate_ids": ["tests:1"], "disposition": "publish",
                 "reason": "Verified", "finding": self.finding}
        for bad_ids in (["unknown:1"], ["tests:1"], []):
            with self.subTest(bad_ids=bad_ids):
                invalid = {"candidate_ids": bad_ids, "disposition": "publish",
                           "reason": "Verified", "finding": {**self.finding, "path": "missing.cpp"}}
                with self.assertRaises(protocol.InvalidReview):
                    protocol.verification(
                        self.verification([valid, invalid]),
                        candidates, self.snapshot)

    def test_concept_publish_is_separate_from_finding_decisions(self):
        concept = {"disposition": "publish", "reason": "Layering concern is supported",
                   "assessment": self.concept_assessment(),
                   "proposed_review": "would_stop",
                   "review_reason": "The verified alternative avoids the layering concern."}
        result, accepted = protocol.verification(
            self.verification([], concept=concept), [], self.snapshot,
            concept_assessment=self.concept_assessment())

        self.assertEqual(accepted, [])
        self.assertEqual(result["concept"]["disposition"], "publish")
        self.assertEqual(result["concept"]["proposed_review"], "would_stop")
        self.assertEqual(result["concept"]["assessment"]["recommendation"],
                         "The alternative is conceptually stronger.")

    def test_concept_assessment_can_have_no_alternatives(self):
        assessment = {**self.concept_assessment(), "alternatives": []}
        result = protocol.archaeology(json.dumps({
            "coverage": self.coverage,
            "assessment": assessment,
        }))

        self.assertEqual(result["assessment"]["alternatives"], [])

    def test_favorable_concept_is_valid_research_but_cannot_be_published(self):
        assessment = {**self.concept_assessment(), "assessment": "worth_pursuing",
                      "proposed_review": "continue"}
        research = protocol.archaeology(json.dumps({
            "coverage": self.coverage, "assessment": assessment,
        }))
        self.assertEqual(research["assessment"], assessment)
        decision = {"candidate_ids": ["tests:1"], "disposition": "publish",
                    "reason": "Verified", "finding": self.finding}
        concept = {"disposition": "publish", "reason": "The approach is sound.",
                   "assessment": assessment, "proposed_review": "continue",
                   "review_reason": "Continue the implementation review."}

        result, accepted = protocol.verification(
            self.verification([decision], concept=concept), [{"id": "tests:1"}],
            self.snapshot, concept_assessment=assessment)

        self.assertEqual(accepted, [{**self.finding, "id": "finding:1"}])
        self.assertEqual(result["concept"]["disposition"], "unresolved")
        self.assertIsNone(result["concept"]["assessment"])
        self.assertIsNotNone(result["concept_validation_error"])

    def test_publishing_multiple_alternatives_preserves_verified_code_finding(self):
        assessment = self.concept_assessment()
        assessment["alternatives"].append({**assessment["alternatives"][0],
                                           "name": "Reuse an existing interface"})
        decision = {"candidate_ids": ["tests:1"], "disposition": "publish",
                    "reason": "Verified", "finding": self.finding}
        concept = {"disposition": "publish", "reason": "The approach needs revision.",
                   "assessment": assessment, "proposed_review": "would_stop",
                   "review_reason": "The alternatives reduce the layering cost."}

        result, accepted = protocol.verification(
            self.verification([decision], concept=concept), [{"id": "tests:1"}],
            self.snapshot, concept_assessment=assessment)

        self.assertEqual(accepted, [{**self.finding, "id": "finding:1"}])
        self.assertEqual(result["concept"]["disposition"], "unresolved")
        self.assertIsNone(result["concept"]["assessment"])
        self.assertIsNotNone(result["concept_validation_error"])

    def test_nonpublish_concept_preserves_verified_review_proposal(self):
        concept = {"disposition": "no_concern",
                   "reason": "The concern is not established.",
                   "assessment": None,
                   "proposed_review": "continue",
                   "review_reason": "The verifier did not find a decisive concept blocker."}
        result, accepted = protocol.verification(
            self.verification([], concept=concept), [], self.snapshot,
            concept_assessment=self.concept_assessment())

        self.assertEqual(accepted, [])
        self.assertIsNone(result["concept"]["assessment"])
        self.assertEqual(result["concept"]["proposed_review"], "continue")
        self.assertEqual(result["coverage"]["status"], "complete")

    def test_would_stop_requires_rework_or_rejection_assessment(self):
        concept = {"disposition": "publish",
                   "reason": "The concern is supposedly verified.",
                   "assessment": {**self.concept_assessment(),
                                  "assessment": "worth_pursuing"},
                   "proposed_review": "would_stop",
                   "review_reason": "This should not be enough to stop review."}
        result, accepted = protocol.verification(
            self.verification([], concept=concept), [], self.snapshot,
            concept_assessment=self.concept_assessment())

        self.assertEqual(accepted, [])
        self.assertEqual(result["concept"]["disposition"], "unresolved")
        self.assertEqual(result["concept"]["proposed_review"], "undetermined")
        self.assertIn("would_stop", result["concept_validation_error"])

    def test_invalid_concept_is_withheld_without_discarding_findings(self):
        bad_concept = {"disposition": "publish", "reason": "Bad citation",
                       "assessment": self.concept_assessment(["not-a-url"]),
                       "proposed_review": "would_stop",
                       "review_reason": "The citation should fail validation."}
        decision = {"candidate_ids": ["tests:1"], "disposition": "publish",
                    "reason": "Verified", "finding": self.finding}
        result, accepted = protocol.verification(
            self.verification([decision], concept=bad_concept),
            [{"id": "tests:1"}], self.snapshot,
            concept_assessment=self.concept_assessment())

        self.assertEqual(accepted, [{**self.finding, "id": "finding:1"}])
        self.assertEqual(result["concept"]["disposition"], "unresolved")
        self.assertIsNone(result["concept"]["assessment"])
        self.assertIn("Citations", result["concept"]["reason"])

    def test_collator_must_preserve_exact_concept_citations(self):
        assessment = self.concept_assessment(["https://code.example.org/org/repo/pulls/1"])
        accepted = []
        with self.assertRaisesRegex(protocol.InvalidReview, "citation"):
            protocol.collation(self.collation(accepted, "See https://code.example.org/org/repo/pulls/10"),
                               accepted, assessment)
        edited, summary = protocol.collation(
            self.collation(accepted, "See [source](https://code.example.org/org/repo/pulls/1)"),
            accepted, assessment)
        self.assertEqual(edited, [])
        self.assertIn("pulls/1", summary)

    def test_malformed_concept_envelope_preserves_verified_findings(self):
        decision = {"candidate_ids": ["tests:1"], "disposition": "publish",
                    "reason": "Verified", "finding": self.finding}
        for concept in (None, {}, {"disposition": [], "reason": "Bad", "assessment": None}):
            with self.subTest(concept=concept):
                response = json.loads(self.verification([decision]))
                response["concept"] = concept
                result, accepted = protocol.verification(
                    json.dumps(response), [{"id": "tests:1"}], self.snapshot,
                    concept_assessment=self.concept_assessment())
                self.assertEqual(len(accepted), 1)
                self.assertEqual(result["concept"]["disposition"], "unresolved")
                self.assertEqual(result["coverage"]["status"], "partial")

    def test_concept_editor_fallback_retains_verified_reason_and_links(self):
        concept = self.concept_assessment()
        reason = "The extra state complicates recovery; can the existing path suffice?"
        summary = protocol.concept_summary(concept, reason)
        self.assertIn(reason, summary)
        self.assertIn(concept["recommendation"], summary)
        for citation in concept["citations"]:
            self.assertIn("](" + citation + ")", summary)

    def test_collator_cannot_add_unverified_concept_sources(self):
        concept = self.concept_assessment()
        summary = protocol.concept_summary(concept, "The cost outweighs the benefit.")
        response = self.collation([], summary + " https://example.invalid/invented")
        with self.assertRaisesRegex(protocol.InvalidReview, "unverified concept citation"):
            protocol.collation(response, [], concept)


if __name__ == "__main__":
    unittest.main()
