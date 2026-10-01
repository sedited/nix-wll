import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from forgejo_review_bot import stats, stats_page
from forgejo_review_bot.jobs import JobStore


class StatsPageTests(unittest.TestCase):
    def save(self, summary, repository_url="https://code.example.org/org/repo",
             report_base_url="https://reports.example.org/reviews"):
        directory = tempfile.TemporaryDirectory()
        stats_page.save_stats(Path(directory.name), summary, repository_url, report_base_url)
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        return root, (root / "index.html").read_text(), json.loads((root / "stats.json").read_text())

    def empty_summary(self):
        return {
            "generated_at": "2026-10-01T00:00:00Z",
            "limitations": {"missing_timestamps": True, "notes": [
                "Bot verification is not human validation.",
            ]},
            "inventory": {
                "jobs_total": 0,
                "jobs_by_status": {},
                "saved_results": 0,
                "analyzed_reviews": 0,
                "unique_prs": 0,
                "skipped_results": 0,
                "malformed_results": 0,
                "recorded_failed_attempts": 0,
                "missing_sources": {
                    "jobs_db": False,
                    "openai_ledger": False,
                    "glm_ledger": False,
                    "report_dir": False,
                },
            },
            "overview": {
                "saved_reviews": 0,
                "published_reviews": 0,
                "saved_findings": 0,
                "published_findings": 0,
                "coverage": {"complete": 0, "partial": 0, "unknown": 0},
                "attribution_coverage": {"complete": 0, "missing": 0},
                "archaeology": {
                    "research_completed": 0,
                    "published_concerns": 0,
                    "assessment_status_counts": {},
                },
                "finding_kinds": {},
                "finding_severities": {},
            },
            "spend": {
                "current_month": self.request_totals(month="2026-10"),
                "lifetime": self.request_totals(),
                "providers": [],
                "models": [],
                "months": [],
            },
            "stage_model_leaderboard": [],
            "stages": [],
            "routing_tiers": {},
            "distributions": {
                "saved_review_known_cost_micros": {"count": 0, "median": None, "p90": None, "max": None},
                "saved_review_reserved_micros": {"count": 0, "median": None, "p90": None, "max": None},
                "saved_review_cost_missing_ledger_count": 0,
                "saved_review_tools": {"count": 0, "median": None, "p90": None, "max": None},
                "saved_review_model_seconds": {"count": 0, "median": None, "p90": None, "max": None},
                "saved_review_conceptual_concern_citations": {
                    "count": 0, "median": None, "p90": None, "max": None},
                "saved_review_conceptual_concern_alternatives": {
                    "count": 0, "median": None, "p90": None, "max": None},
            },
            "paired_sol_glm": {
                "completed_pairs": 0,
                "sole_accepted": {"adversarial": 0, "adversarial_glm": 0},
                "shared_accepted": {"adversarial": 0, "adversarial_glm": 0},
                "known_cost_micros": {"adversarial": 0, "adversarial_glm": 0},
                "reserved_micros": {"adversarial": 0, "adversarial_glm": 0},
                "note": "Observed paired completed reviews only; this is not causal recall.",
            },
            "recent_reviews": [],
            "reviews": [],
        }

    def request_totals(self, month=None, request_count=0, known_cost_micros=0,
                       reserved_micros=0, unknown_cost_request_count=0,
                       unknown_usage_count=0, incomplete_usage_count=0):
        record = {
            "request_count": request_count,
            "known_cost_micros": known_cost_micros,
            "reserved_micros": reserved_micros,
            "unknown_cost_request_count": unknown_cost_request_count,
            "unknown_usage_count": unknown_usage_count,
            "incomplete_usage_count": incomplete_usage_count,
            "status_counts": {},
            "tokens": {"input": 0, "output": 0, "cached": 0, "cache_write": 0},
        }
        if month is not None:
            record["month"] = month
        return record

    def rich_summary(self):
        summary = self.empty_summary()
        summary["inventory"].update({
            "jobs_total": 3,
            "jobs_by_status": {"complete": 2, "failed": 1},
            "saved_results": 2,
            "analyzed_reviews": 2,
            "unique_prs": 2,
            "recorded_failed_attempts": 2,
        })
        summary["overview"].update({
            "saved_reviews": 2,
            "published_reviews": 2,
            "saved_findings": 3,
            "published_findings": 3,
            "coverage": {"complete": 1, "partial": 1, "unknown": 0},
            "archaeology": {
                "research_completed": 2,
                "published_concerns": 1,
                "assessment_status_counts": {"verified": 1},
            },
        })
        summary["spend"] = {
            "current_month": {
                **self.request_totals("2026-10", 7, 1_234_000, 300_000, 1, 1, 1),
                "providers": {
                    "openai": self.request_totals(request_count=4, known_cost_micros=1_000_000,
                                                  reserved_micros=100_000,
                                                  unknown_cost_request_count=1),
                    "glm": self.request_totals(request_count=3, known_cost_micros=234_000,
                                               reserved_micros=200_000),
                },
            },
            "lifetime": self.request_totals(request_count=9, known_cost_micros=2_000_000,
                                            reserved_micros=500_000,
                                            unknown_cost_request_count=1),
            "providers": [
                {"provider": "openai", **self.request_totals(request_count=4,
                                                             known_cost_micros=1_000_000)},
                {"provider": "glm", **self.request_totals(request_count=3,
                                                          known_cost_micros=234_000)},
            ],
            "models": [{
                "provider": "openai",
                "model": "gpt-6.1-sol",
                **self.request_totals(request_count=4, known_cost_micros=1_000_000),
                "tokens": {"input": 400, "output": 80, "cached": 100, "cache_write": 40},
            }],
            "months": [
                {"month": "2026-09", **self.request_totals(request_count=2,
                                                           known_cost_micros=500_000)},
                {"month": "2026-10", **self.request_totals(request_count=7,
                                                           known_cost_micros=1_234_000)},
            ],
        }
        summary["stage_model_leaderboard"] = [
            {
                "stage": "wallet",
                "configured_model": "gpt-6.1-sol",
                "accepted_findings": 3,
                "concept_stage": False,
                "conceptual_concerns": 0,
                "selected_candidate_findings": 3,
                "sole_source_findings": 2,
                "shared_findings": 1,
                "executed_runs": 2,
                "known_cost_micros": 1_200_000,
                "reserved_micros": 100_000,
                "request_count": 5,
                "known_cost_per_accepted_finding_micros": 400_000,
                "note": "Shared findings overlap across stages and do not sum to total unique findings.",
            },
            {
                "stage": "tests",
                "configured_model": "glm-5.3",
                "accepted_findings": 1,
                "concept_stage": False,
                "conceptual_concerns": 0,
                "selected_candidate_findings": 1,
                "sole_source_findings": 1,
                "shared_findings": 0,
                "executed_runs": 1,
                "known_cost_micros": 234_000,
                "reserved_micros": 200_000,
                "request_count": 3,
                "known_cost_per_accepted_finding_micros": 234_000,
                "note": "Shared findings overlap across stages and do not sum to total unique findings.",
            },
        ]
        summary["stages"] = [
            {
                "stage": "wallet",
                "configured_model": "gpt-6.1-sol",
                "saved_result_runs": 2,
                "executed_runs": 2,
                "skipped_runs": 0,
                "status_counts": {"completed": 2},
                "candidate_counts": {"publish": 3, "drop": 1, "unresolved": 1, "undisposed": 1},
                "accepted_findings": 3,
                "concept_stage": False,
                "conceptual_concerns": 0,
                "selected_candidate_findings": 3,
                "sole_source_findings": 2,
                "shared_findings": 1,
                "attribution_unknown_reviews": 0,
                "acceptance_denominator_runs": 2,
                "accepted_per_executed_run": 1.5,
                "tool_calls": 5,
                "model_turns": 2,
                "model_seconds": 12.5,
                "saved_trace_known_estimated_cost_micros": 800_000,
                "known_cost_per_accepted_finding_micros": 400_000,
                "ledger": self.request_totals(request_count=5, known_cost_micros=1_200_000,
                                              reserved_micros=100_000),
            },
            {
                "stage": "tests",
                "configured_model": "glm-5.3",
                "saved_result_runs": 1,
                "executed_runs": 1,
                "skipped_runs": 0,
                "status_counts": {"completed": 1},
                "candidate_counts": {"publish": 1, "drop": 3, "unresolved": 0, "undisposed": 0},
                "accepted_findings": 1,
                "concept_stage": False,
                "conceptual_concerns": 0,
                "selected_candidate_findings": 1,
                "sole_source_findings": 1,
                "shared_findings": 0,
                "attribution_unknown_reviews": 0,
                "acceptance_denominator_runs": 1,
                "accepted_per_executed_run": 1,
                "tool_calls": 2,
                "model_turns": 1,
                "model_seconds": 4,
                "saved_trace_known_estimated_cost_micros": 100_000,
                "known_cost_per_accepted_finding_micros": 234_000,
                "ledger": self.request_totals(request_count=3, known_cost_micros=234_000,
                                              reserved_micros=200_000),
            },
        ]
        summary["routing_tiers"] = {"cheap": 2, "deep": 1}
        summary["distributions"] = {
            "saved_review_known_cost_micros": {"count": 2, "median": 500_000, "p90": 1_000_000, "max": 1_000_000},
            "saved_review_reserved_micros": {"count": 2, "median": 150_000, "p90": 200_000, "max": 200_000},
            "saved_review_cost_missing_ledger_count": 0,
            "saved_review_tools": {"count": 2, "median": 3, "p90": 5, "max": 5},
            "saved_review_model_seconds": {"count": 2, "median": 12.5, "p90": 20, "max": 20},
            "saved_review_conceptual_concern_citations": {
                "count": 1, "median": 2, "p90": 2, "max": 2},
            "saved_review_conceptual_concern_alternatives": {
                "count": 1, "median": 3, "p90": 3, "max": 3},
        }
        summary["paired_sol_glm"] = {
            "completed_pairs": 2,
            "sole_accepted": {"adversarial": 1, "adversarial_glm": 1},
            "shared_accepted": {"adversarial": 1, "adversarial_glm": 0},
            "known_cost_micros": {"adversarial": 1_000_000, "adversarial_glm": 234_000},
            "reserved_micros": {"adversarial": 100_000, "adversarial_glm": 200_000},
            "note": "Observed paired completed reviews only; this is not causal recall.",
        }
        summary["reviews"] = [
            {
                "job_id": 7,
                "number": 7,
                "generation": 1,
                "head": "a" * 40,
                "status": "complete",
                "job_attempts": 1,
                "recorded_failed_attempts": 1,
                "skipped": False,
                "routing": {"selected_tier": "cheap"},
                "coverage": "partial",
                "findings": {"saved": 1, "published": 1},
                "archaeology": {
                    "research_completed": True,
                    "published_concern": True,
                    "status": "verified",
                    "stage": "archaeologist",
                    "citation_count": 2,
                    "alternative_count": 3,
                },
                "ledger": self.request_totals(request_count=3, known_cost_micros=234_000,
                                              reserved_micros=200_000),
                "tokens": {"input": 10, "output": 2, "cached": 1, "cache_write": 0},
                "tool_calls": 2,
                "model_seconds": 4,
                "saved_trace_known_estimated_cost_micros": 100_000,
                "report_name": "7-deadbeef.html",
            },
            {
                "job_id": 9,
                "number": 9,
                "generation": 1,
                "head": "b" * 40,
                "status": "complete",
                "job_attempts": 1,
                "recorded_failed_attempts": 1,
                "skipped": False,
                "routing": {"selected_tier": "deep"},
                "coverage": "complete",
                "findings": {"saved": 2, "published": 2},
                "archaeology": {"research_completed": True, "published_concern": False},
                "ledger": self.request_totals(request_count=4, known_cost_micros=1_000_000,
                                              reserved_micros=100_000),
                "tokens": {"input": 20, "output": 4, "cached": 2, "cache_write": 1},
                "tool_calls": 5,
                "model_seconds": 20,
                "saved_trace_known_estimated_cost_micros": 800_000,
                "report_name": "9-feedface.html",
            },
        ]
        summary["recent_reviews"] = list(summary["reviews"])
        return summary

    def test_empty_stats_render_stable_public_files(self):
        root, html, public = self.save(self.empty_summary())

        self.assertTrue((root / "index.html").is_file())
        self.assertTrue((root / "stats.json").is_file())
        self.assertEqual((root / "index.html").stat().st_mode & 0o777, 0o644)
        self.assertEqual((root / "stats.json").stat().st_mode & 0o777, 0o644)
        self.assertEqual(public["overview"]["saved_reviews"], 0)
        self.assertIn("No saved reviews are in this summary yet.", html)
        self.assertIn("Datasets present: 4/4. Missing: none.", html)

    def test_useful_sections_put_leaderboard_before_cost_tables(self):
        _, html, _ = self.save(self.rich_summary())

        self.assertLess(html.index('class="cards"'), html.index('id="leaderboard"'))
        self.assertLess(html.index('id="leaderboard"'), html.index('id="concepts"'))
        self.assertLess(html.index('id="concepts"'), html.index('id="paired"'))
        self.assertLess(html.index('id="paired"'), html.index('id="spend"'))
        self.assertIn("Accepted finding leaderboard", html)
        self.assertIn("Conceptual concerns", html)
        self.assertIn("Median citations: 2.0; median alternatives: 3.0.", html)
        self.assertIn("research complete; no concern", html)
        self.assertIn("Shared findings are credited to each contributing agent", html)
        self.assertIn("Paired model reviews", html)

    def test_hostile_markup_is_escaped_and_not_used_as_links(self):
        summary = self.rich_summary()
        summary["generated_at"] = "</p><script>alert('date')</script>"
        summary["spend"]["current_month"]["providers"]["<script>x</script>"] = {
            **self.request_totals(request_count=1, known_cost_micros=1)
        }
        summary["stage_model_leaderboard"][0]["stage"] = "</td><script>alert('stage')</script>"
        summary["stages"][0]["stage"] = "</td><script>alert('stage')</script>"
        summary["reviews"].append({
            "job_id": 12,
            "number": "12<script>",
            "generation": 1,
            "status": "<img src=x onerror=alert(1)>",
            "coverage": "unknown",
            "findings": {"saved": 0, "published": 0},
            "ledger": self.request_totals(),
            "recorded_failed_attempts": 0,
            "report_name": "../bad<script>.html",
        })

        _, html, _ = self.save(summary, repository_url="javascript:alert(1)",
                               report_base_url="https://reports.example.org/reviews?x=<script>")

        self.assertNotIn("<script", html)
        self.assertNotIn("javascript:alert", html)
        self.assertNotIn("../bad", html)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", html)
        self.assertIn("&lt;/td&gt;&lt;script&gt;alert(&#x27;stage&#x27;)&lt;/script&gt;", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)

    def test_report_and_pr_links_are_constructed_from_safe_parts(self):
        _, html, _ = self.save(self.rich_summary())

        self.assertIn('href="https://code.example.org/org/repo/pulls/9"', html)
        self.assertIn('href="https://reports.example.org/reviews/9-feedface.html"', html)
        self.assertIn('href="https://reports.example.org/reviews/7-deadbeef.html"', html)
        self.assertLess(html.index("#9"), html.index("#7"))

    def test_displayed_formulas_match_schema_values(self):
        _, html, _ = self.save(self.rich_summary())

        self.assertIn("$1.23", html)
        self.assertIn("Median review cost", html)
        self.assertIn("matched saved-review cohort: 2", html)
        self.assertIn("<td>wallet</td><td>gpt-6.1-sol</td><td>3</td>", html)
        self.assertIn("$0.40", html)
        self.assertIn("60.0%", html)
        self.assertIn("<td>1.500</td>", html)
        self.assertIn("9 API request attempts for 3 review jobs", html)
        self.assertIn("Selected / dropped / unresolved / undisposed", html)

    def test_collect_stats_output_renders_with_nested_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            report_dir = state_dir / "reports"
            jobs = JobStore(state_dir / "jobs.sqlite3", max_attempts=2,
                            retry_base_seconds=0)
            jobs.enqueue(42, "master", "a" * 40, "synchronize")
            job = jobs.claim()
            result = self.collector_result()
            self.assertTrue(jobs.save_result(job, result))
            self.assertTrue(jobs.complete(job))
            report_dir.mkdir()
            report_name = f'42-{"a" * 40}-{job["id"]}-{job["generation"]}.html'
            (report_dir / report_name).write_text("public report", encoding="utf-8")
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            review_id = f'pr:42:{job["id"]}:{job["generation"]}'
            self.create_ledger(state_dir / "spend.sqlite3", [
                ("openai-1", review_id, "independent", "gpt-6.1-sol", month,
                 "settled", self.usage(), 100, 0),
                ("openai-2", review_id, "adversarial", "gpt-6.1-sol", month,
                 "settled", self.usage(), 150, 0),
            ])
            self.create_ledger(state_dir / "ppq-spend.sqlite3", [
                ("glm-1", review_id, "adversarial_glm", "z-ai/glm-5.3", month,
                 "unknown", self.usage(complete=False, cache_write_tokens=None), None, 700),
            ])

            summary = stats.collect_stats(state_dir, report_dir)
            root, html, public = self.save(summary)

        self.assertEqual(public["reviews"][0]["report_name"], report_name)
        self.assertIn("adversarial_glm</td><td>glm-5.3", html)
        self.assertIn("Findings saved/published", html)
        self.assertIn('href="https://reports.example.org/reviews/' + report_name + '"', html)
        self.assertTrue((root / "index.html").is_file())

    def create_ledger(self, path, rows):
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE requests (
                token TEXT PRIMARY KEY,
                review_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                model TEXT NOT NULL,
                month TEXT NOT NULL,
                response_id TEXT,
                status TEXT NOT NULL,
                usage_json TEXT,
                cost_micros INTEGER,
                reserved_micros INTEGER NOT NULL
            )""")
            db.executemany(
                "INSERT INTO requests VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                rows)

    def usage(self, input_tokens=100, output_tokens=20, complete=True,
              cache_write_tokens=0):
        return json.dumps({
            "input_tokens": input_tokens,
            "cached_tokens": 10,
            "cache_write_tokens": cache_write_tokens,
            "complete": complete,
            "output_tokens": output_tokens,
        })

    def collector_result(self):
        return {
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
            "content": "PRIVATE review body must not leak",
            "debug": {
                "coverage": {"status": "complete", "limitations": []},
                "candidate_sources": {
                    "independent:1": "independent",
                    "adversarial:1": "adversarial",
                    "adversarial_glm:1": "adversarial_glm",
                },
                "decisions": [{
                    "candidate_ids": ["independent:1", "adversarial:1",
                                      "adversarial_glm:1"],
                    "disposition": "publish",
                    "reason": "same verified bug",
                    "finding": {
                        "kind": "defect",
                        "severity": "high",
                        "path": "src/node.cpp",
                        "line": 10,
                        "side": "head",
                        "title": "Merged finding",
                        "body": "PRIVATE finding body must not leak",
                    },
                }],
                "finding_attribution": [{
                    "finding_id": "finding:1",
                    "title": "Merged finding",
                    "candidate_ids": ["independent:1", "adversarial:1",
                                      "adversarial_glm:1"],
                    "raised_by": ["independent", "adversarial", "adversarial_glm"],
                    "raised_by_models": ["gpt-6.1-sol", "glm-5.3"],
                    "verified_by": "verifier",
                    "edited_by": "collator",
                }],
                "stages": {
                    "independent": {
                        "model": "gpt-6.1-sol",
                        "status": "completed",
                        "turns": [{"elapsed_seconds": 1.5}],
                        "tools": [{"name": "read_file"}],
                    },
                    "adversarial": {
                        "model": "gpt-6.1-sol",
                        "status": "completed",
                        "turns": [{"elapsed_seconds": 1.0}],
                        "tools": [],
                    },
                    "adversarial_glm": {
                        "model": "glm-5.3",
                        "status": "completed",
                        "turns": [{"elapsed_seconds": 2.0}],
                        "tools": [],
                    },
                },
            },
        }


if __name__ == "__main__":
    unittest.main()
