import json
import unittest
from types import SimpleNamespace

from forgejo_review_bot import protocol, routing


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = SimpleNamespace(changed_paths={"src/example.cpp"},
                                        head_files={"src/example.cpp": "blob"},
                                        base_files={"src/example.cpp": "old"})
        self.finding = {"severity": "suggestion", "path": "src/example.cpp",
                        "line": 5, "side": "head", "title": "Consolidate duplicate setup",
                        "body": "Reuse the existing fixture while keeping the regression assertion."}
        self.coverage = {"status": "complete", "limitations": []}

    def test_verifier_groups_sources_and_preserves_every_candidate(self):
        candidates = [{"id": "tests:1"}, {"id": "design:1"}, {"id": "independent:1"}]
        decisions = [
            {"candidate_ids": ["tests:1", "design:1"], "disposition": "publish",
             "reason": "Same redundant fixture", "finding": self.finding},
            {"candidate_ids": ["independent:1"], "disposition": "unresolved",
             "reason": "Caller evidence missing", "finding": None},
        ]
        result, accepted = protocol.verification(
            json.dumps({"coverage": {"status": "complete", "limitations": ["Caller unavailable"]},
                        "decisions": decisions}), candidates, self.snapshot)
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(len(accepted), 1)
        self.assertEqual(result["decisions"][1]["disposition"], "unresolved")
        decisions.pop()
        with self.assertRaisesRegex(protocol.InvalidReview, "omitted"):
            protocol.verification(json.dumps({"coverage": self.coverage, "decisions": decisions}),
                                  candidates, self.snapshot)

    def test_collator_cannot_add_remove_or_duplicate_accepted_ids(self):
        accepted = [{**self.finding, "id": "finding:1"}]
        for edits in ([], [{"id": "invented", "title": "New", "body": "Claim"}],
                      [{"id": "finding:1", "title": "A", "body": "B"}] * 2):
            with self.subTest(edits=edits), self.assertRaises(protocol.InvalidReview):
                protocol.collation(json.dumps({"findings": edits}), accepted)
        edited = protocol.collation(json.dumps({"findings": [
            {"id": "finding:1", "title": "Short title", "body": "Edited wording"}]}), accepted)
        self.assertEqual(edited[0]["path"], self.finding["path"])
        self.assertEqual(edited[0]["severity"], "suggestion")
        self.assertIn("incomplete", protocol.render(edited, ["A selected audit was unavailable."]))

    def test_finding_must_refer_to_changed_file_on_correct_side(self):
        decision = {"candidate_ids": ["tests:1"], "disposition": "publish",
                    "reason": "Verified", "finding": {**self.finding, "path": "missing.cpp"}}
        result, accepted = protocol.verification(
            json.dumps({"coverage": self.coverage, "decisions": [decision]}),
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
                    json.dumps({"coverage": self.coverage, "decisions": decisions}),
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
            json.dumps({"coverage": self.coverage, "decisions": [decision]}),
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
                    json.dumps({"coverage": self.coverage, "decisions": [decision]}),
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
                        json.dumps({"coverage": self.coverage,
                                    "decisions": [valid, invalid]}),
                        candidates, self.snapshot)

    def test_router_cannot_lower_floor_and_selects_design_and_test_review(self):
        proposed = {"tier": "routine", "audits": [], "evidence": ["Small change"],
                    "missing_context": []}
        plan = routing.validate_plan(json.dumps(proposed), {"src/example.cpp"}, "standard")
        self.assertEqual(plan["tier"], "standard")
        self.assertEqual(set(plan["audits"]), {"tests", "design"})
        proposed["missing_context"] = ["Patch excerpt omitted"]
        self.assertEqual(routing.validate_plan(json.dumps(proposed), set(), "routine")["tier"],
                         "sensitive")
        self.assertEqual(routing.minimum_tier({"src/validation.cpp"}), "sensitive")
        self.assertEqual(routing.minimum_tier({"src/test/example_tests.cpp"}), "standard")
        self.assertTrue(routing.is_test_path("src/wallet/test/example_tests.cpp"))


if __name__ == "__main__":
    unittest.main()
