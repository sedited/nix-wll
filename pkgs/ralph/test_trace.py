import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ralph import report, trace


class TraceAttributionTests(unittest.TestCase):
    def setUp(self):
        self.prompt_config = SimpleNamespace(
            instructions="Review.",
            models={"independent": "gpt-6-luna", "verifier": "gpt-6-luna",
                    "collator": "gpt-6-luna"})

    def test_stage_metrics_credit_merged_candidates_without_unique_overcount(self):
        debug = {
            "candidate_sources": {
                "independent:1": "independent",
                "adversarial:1": "adversarial",
                "state:1": "state",
                "tests:1": "tests",
            },
            "decisions": [
                {"candidate_ids": ["independent:1", "adversarial:1"],
                 "disposition": "publish", "reason": "same bug", "finding": {}},
                {"candidate_ids": ["state:1"], "disposition": "drop",
                 "reason": "not a bug", "finding": None},
                {"candidate_ids": ["tests:1"], "disposition": "unresolved",
                 "reason": "needs more evidence", "finding": None},
            ],
            "finding_attribution": [{
                "finding_id": "finding:1",
                "title": "Merged bug",
                "path": "src/node.cpp",
                "line": 12,
                "side": "head",
                "raised_by": ["adversarial", "independent"],
                "candidate_ids": ["independent:1", "adversarial:1"],
                "verified_by": "verifier",
                "edited_by": "collator",
            }],
            "stages": {
                "independent": {"model": "gpt-6-luna", "status": "completed",
                                "turns": [], "tools": []},
                "adversarial": {"model": "gpt-6-luna", "status": "completed",
                                "profiles": ["p2p"], "turns": [], "tools": []},
                "state": {"model": "gpt-6-luna", "status": "completed",
                          "turns": [], "tools": []},
                "tests": {"model": "gpt-6-luna", "status": "completed",
                          "turns": [], "tools": []},
            },
        }

        metrics = trace.stage_metrics(debug)

        self.assertEqual(metrics["independent"]["candidate_counts"]["publish"], 1)
        self.assertEqual(metrics["adversarial"]["candidate_counts"]["publish"], 1)
        self.assertEqual(metrics["state"]["candidate_counts"]["drop"], 1)
        self.assertEqual(metrics["tests"]["candidate_counts"]["unresolved"], 1)
        self.assertEqual(metrics["independent"]["accepted_findings"], 1)
        self.assertEqual(metrics["adversarial"]["accepted_findings"], 1)
        self.assertEqual(metrics["independent"]["shared_findings"], 1)
        self.assertEqual(metrics["adversarial"]["shared_findings"], 1)
        self.assertEqual(metrics["adversarial"]["profiles"], ["p2p"])

    def test_verifier_only_finding_is_credited_to_verifier(self):
        debug = {
            "decisions": [
                {"candidate_ids": [], "disposition": "publish",
                 "reason": "new finding", "finding": {}},
            ],
            "finding_attribution": [{
                "finding_id": "finding:1",
                "title": "Verifier bug",
                "path": "src/validation.cpp",
                "line": 7,
                "side": "head",
                "raised_by": ["verifier"],
                "candidate_ids": [],
                "verified_by": "verifier",
                "edited_by": "collator",
            }],
            "stages": {"verifier": {"model": "gpt-6-luna", "status": "completed",
                                    "turns": [], "tools": []}},
        }

        metrics = trace.stage_metrics(debug)

        self.assertEqual(metrics["verifier"]["candidate_counts"]["publish"], 0)
        self.assertEqual(metrics["verifier"]["accepted_findings"], 1)
        self.assertEqual(metrics["verifier"]["sole_source_findings"], 1)

    def test_collator_fallback_leaves_editor_empty_in_public_table(self):
        debug = {
            "finding_attribution": [{
                "finding_id": "finding:1",
                "title": "Verifier wording",
                "path": "src/rpc.cpp",
                "line": 3,
                "side": "head",
                "raised_by": ["independent"],
                "candidate_ids": ["independent:1"],
                "verified_by": "verifier",
                "edited_by": None,
            }],
            "stages": {},
        }

        with tempfile.TemporaryDirectory() as directory:
            filename = report.save_report(Path(directory), 42, "a" * 40,
                                          "Review.", debug, self.prompt_config, "42-run")
            rendered = (Path(directory) / filename).read_text()
        public = trace.review_trace(debug, self.prompt_config)

        self.assertIsNone(public["finding_attribution"][0]["edited_by"])
        self.assertIn("<td>independent:1</td><td>verifier</td><td></td>", rendered)

    def test_invalid_withheld_finding_has_no_attribution_but_keeps_decision_counts(self):
        debug = {
            "candidate_sources": {"independent:1": "independent"},
            "decisions": [
                {"candidate_ids": ["independent:1"], "disposition": "unresolved",
                 "reason": "Finding withheld: invalid path", "finding": None},
            ],
            "stages": {"independent": {"model": "gpt-6-luna", "status": "completed",
                                       "turns": [], "tools": []}},
        }

        public = trace.review_trace(debug, self.prompt_config)

        self.assertNotIn("finding_attribution", public)
        self.assertEqual(
            public["stage_metrics"]["independent"]["candidate_counts"]["unresolved"],
            1)
        self.assertEqual(
            public["stage_metrics"]["independent"]["candidate_counts"]["undisposed"],
            0)
        self.assertEqual(public["stage_metrics"]["independent"]["accepted_findings"], 0)

    def test_candidates_are_undisposed_when_verifier_fails_before_decisions(self):
        debug = {
            "candidate_sources": {
                "independent:1": "independent",
                "independent:2": "independent",
                "missing:1": "missing",
            },
            "stages": {"independent": {"model": "gpt-6-luna", "status": "completed",
                                       "turns": [], "tools": []}},
        }

        metrics = trace.stage_metrics(debug)

        self.assertEqual(metrics["independent"]["candidate_counts"]["undisposed"], 2)
        self.assertNotIn("missing", metrics)
        self.assertNotIn("unknown", metrics)

    def test_stage_metrics_track_unknown_and_incomplete_costs(self):
        debug = {"stages": {
            "independent": {"model": "gpt-6-luna", "status": "completed",
                            "tools": [{"name": "read_file"}],
                            "turns": [
                                {"model": "gpt-6-luna", "input_tokens": 100,
                                 "cached_tokens": 0, "cache_write_tokens": None,
                                 "output_tokens": 20},
                                {"model": "unknown-model", "input_tokens": 1,
                                 "output_tokens": 1},
                            ]},
        }}

        metrics = trace.stage_metrics(debug)

        self.assertEqual(metrics["independent"]["calls"], 2)
        self.assertEqual(metrics["independent"]["tool_calls"], 1)
        self.assertEqual(metrics["independent"]["known_estimated_cost_usd"], 0.000023)
        self.assertEqual(metrics["independent"]["unknown_usage_count"], 1)
        self.assertEqual(metrics["independent"]["incomplete_usage_count"], 1)

    def test_public_attribution_table_escapes_untrusted_text(self):
        debug = {
            "finding_attribution": [{
                "finding_id": "finding:1",
                "title": "<script>alert('x')</script> & title",
                "path": "src/<bad>.cpp",
                "line": 5,
                "side": "head",
                "raised_by": ["agent<one>"],
                "candidate_ids": ["agent<one>:1"],
                "verified_by": "verifier",
                "edited_by": "collator",
            }],
            "stages": {},
        }

        with tempfile.TemporaryDirectory() as directory:
            filename = report.save_report(Path(directory), 42, "a" * 40,
                                          "Review.", debug, self.prompt_config, "42-run")
            rendered = (Path(directory) / filename).read_text()

        table = rendered.split("<table>", 1)[1].split("</table>", 1)[0]
        self.assertIn("&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; title",
                      table)
        self.assertIn("src/&lt;bad&gt;.cpp:5", table)
        self.assertIn("agent&lt;one&gt;", table)
        self.assertNotIn("<script>alert", table)

    def test_private_trace_keeps_top_level_attribution_inputs(self):
        debug = {
            "candidate_sources": {"independent:1": "independent"},
            "decisions": [{"candidate_ids": ["independent:1"], "disposition": "drop",
                           "reason": "not a bug", "finding": None}],
            "finding_attribution": [],
            "stages": {},
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = trace.save_review_trace(Path(tmp), 123, "head", "review", debug,
                                           self.prompt_config)
            record = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(record["candidate_sources"], debug["candidate_sources"])
        self.assertEqual(record["decisions"], debug["decisions"])
        self.assertEqual(record["finding_attribution"], [])

    def test_public_trace_omits_stale_global_token_limits(self):
        public = trace.review_trace({"stages": {}}, self.prompt_config)

        self.assertNotIn("max_output_tokens", public)
        self.assertNotIn("verifier_max_output_tokens", public)
        self.assertNotIn("collator_max_output_tokens", public)


if __name__ == "__main__":
    unittest.main()
