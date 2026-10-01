import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from forgejo_review_bot import stats
from forgejo_review_bot.jobs import JobStore


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_dir = Path(self.directory.name)
        self.report_dir = self.state_dir / "public"
        self.jobs = JobStore(self.state_dir / "jobs.sqlite3",
                             max_attempts=2, retry_base_seconds=0)

    def enqueue(self, number, head):
        self.jobs.enqueue(number, "master", head, "synchronize")
        return self.jobs.claim()

    def save_complete(self, job, result):
        self.assertTrue(self.jobs.save_result(job, result))
        self.assertTrue(self.jobs.complete(job))

    def result(self, head="a" * 40):
        return {
            "base_sha": "b" * 40,
            "head_sha": head,
            "content": "PRIVATE review body must not leak",
            "debug": {
                "private_state": "PRIVATE debug marker",
                "coverage": {"status": "partial", "limitations": ["missing run"]},
                "concept_assessment": {
                    "status": "verified",
                    "stage": "archaeologist",
                    "summary": "Fix this in the policy layer. https://code.example.org/org/repo/pulls/1",
                    "verification": {
                        "disposition": "publish",
                        "reason": "The layering concern is supported.",
                        "assessment": {
                            "problem": "The PR changes peer eviction policy.",
                            "baseline": "Keep the current policy.",
                            "delivered_benefit": "Removes one documented operator workaround.",
                            "relevant_history": "Prior discussion asked for a narrower layer.",
                            "recommendation": "Fix it in the policy layer.",
                            "decisive_question": "Whether the old workaround is still needed.",
                            "technical_assumptions": ["Verifier checks the policy call path."],
                            "alternatives": [{
                                "name": "Do nothing",
                                "concept": "Keep the current policy.",
                                "benefit": "No behavior churn.",
                                "cost": "The workaround remains.",
                                "unresolved": "Current impact is unclear.",
                                "provenance": "agent_inference",
                                "citations": [],
                            }],
                            "citations": ["https://code.example.org/org/repo/pulls/1"],
                        },
                    },
                },
                "routing": {
                    "mode": "enabled",
                    "minimum_tier": "standard",
                    "proposed": {"tier": "standard", "audits": ["tests"],
                                 "profiles": []},
                    "selected": {"tier": "sensitive",
                                 "audits": ["tests", "design"],
                                 "profiles": ["p2p"]},
                },
                "candidate_sources": {
                    "independent:1": "independent",
                    "independent:2": "independent",
                    "adversarial:1": "adversarial",
                    "adversarial_glm:1": "adversarial_glm",
                    "adversarial_glm:2": "adversarial_glm",
                },
                "decisions": [
                    {"candidate_ids": ["independent:1", "adversarial:1",
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
                     }},
                    {"candidate_ids": ["independent:2"], "disposition": "drop",
                     "reason": "not a bug", "finding": None},
                    {"candidate_ids": ["adversarial_glm:2"],
                     "disposition": "unresolved",
                     "reason": "needs evidence", "finding": None},
                ],
                "finding_attribution": [{
                    "finding_id": "finding:1",
                    "title": "Merged finding",
                    "path": "src/node.cpp",
                    "line": 10,
                    "side": "head",
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
                        "raw_output": "PRIVATE raw output",
                        "turns": [{
                            "model": "gpt-6.1-sol",
                            "request_bytes": 100,
                            "input_tokens": 100,
                            "cached_tokens": 10,
                            "cache_write_tokens": 0,
                            "output_tokens": 20,
                            "elapsed_seconds": 1.5,
                            "input": "PRIVATE turn input",
                        }],
                        "tools": [{"name": "read_file",
                                   "output": "PRIVATE tool output"}],
                    },
                    "adversarial_glm": {
                        "model": "glm-5.3",
                        "status": "completed",
                        "turns": [{
                            "model": "z-ai/glm-5.3",
                            "input_tokens": 80,
                            "cached_tokens": 0,
                            "cache_write_tokens": 0,
                            "output_tokens": 16,
                            "elapsed_seconds": 2.25,
                        }],
                        "tools": [],
                    },
                    "adversarial": {
                        "model": "gpt-6.1-sol",
                        "status": "completed",
                        "turns": [{
                            "model": "gpt-6.1-sol",
                            "input_tokens": 60,
                            "cached_tokens": 0,
                            "cache_write_tokens": 0,
                            "output_tokens": 12,
                            "elapsed_seconds": 1.0,
                        }],
                        "tools": [],
                    },
                    "tests": {
                        "model": "gpt-6.1-sol",
                        "status": "skipped",
                        "turns": [],
                        "tools": [],
                    },
                    "archaeologist": {
                        "model": "gpt-6-luna",
                        "status": "completed",
                        "turns": [{
                            "model": "gpt-6-luna",
                            "input_tokens": 40,
                            "cached_tokens": 0,
                            "cache_write_tokens": 0,
                            "output_tokens": 10,
                            "elapsed_seconds": 0.5,
                        }],
                        "tools": [{"name": "web_search"}],
                    },
                },
            },
        }

    def create_ledger(self, filename, rows):
        path = self.state_dir / filename
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

    def test_collects_public_stats_from_jobs_and_both_ledgers(self):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        reviewed = self.enqueue(42, "a" * 40)
        self.save_complete(reviewed, self.result())
        skipped = self.enqueue(43, "c" * 40)
        self.save_complete(skipped, {
            "base_sha": "d" * 40,
            "head_sha": "c" * 40,
            "content": "Skipped: no production changes",
            "debug": {"skip": "no production changes"},
        })
        failed = self.enqueue(44, "e" * 40)
        self.assertEqual(self.jobs.retry(failed, "first failure"), "retry")
        failed_retry = self.jobs.claim()
        self.assertEqual(self.jobs.retry(failed_retry, "second failure"), "failed")
        with sqlite3.connect(self.state_dir / "jobs.sqlite3") as db:
            db.execute(
                "INSERT INTO jobs (number, generation, base_ref, head, action, "
                "force, status, attempts, review_result) "
                "VALUES (45, 1, 'master', ?, 'synchronize', 0, "
                "'complete', 0, ?)",
                ("f" * 40, "{not json}"))

        self.report_dir.mkdir()
        report_name = f'42-{"a" * 40}-{reviewed["id"]}-{reviewed["generation"]}.html'
        (self.report_dir / report_name).write_text("public report", encoding="utf-8")
        review_id = f'pr:42:{reviewed["id"]}:{reviewed["generation"]}'
        failed_id = f'pr:44:{failed["id"]}:{failed["generation"]}'
        self.create_ledger("spend.sqlite3", [
            ("openai-1", review_id, "independent", "gpt-6.1-sol", month,
             "settled", self.usage(100, 20), 100, 0),
            ("openai-2", review_id, "independent", "gpt-6.1-sol", month,
             "uncertain", None, None, 500),
            ("openai-3", failed_id, "independent", "gpt-6.1-sol", month,
             "settled", self.usage(50, 10, complete=False,
                                    cache_write_tokens=None), 200, 0),
            ("openai-4", review_id, "adversarial", "gpt-6.1-sol", month,
             "settled", self.usage(60, 12), 150, 0),
            ("openai-5", review_id, "archaeologist", "gpt-6-luna", month,
             "settled", self.usage(40, 10), 50, 0),
        ])
        self.create_ledger("ppq-spend.sqlite3", [
            ("glm-1", review_id, "adversarial_glm", "z-ai/glm-5.3", month,
             "settled", self.usage(80, 16), 300, 0),
            ("glm-2", review_id, "adversarial_glm", "z-ai/glm-5.3", month,
             "unknown", self.usage(70, 14, complete=False,
                                    cache_write_tokens=None), None, 700),
        ])

        summary = stats.collect_stats(self.state_dir, self.report_dir)
        text = json.dumps(summary, sort_keys=True)

        self.assertNotIn("PRIVATE", text)
        self.assertEqual(summary["inventory"]["jobs_total"], 4)
        self.assertEqual(summary["inventory"]["saved_results"], 2)
        self.assertEqual(summary["inventory"]["analyzed_reviews"], 1)
        self.assertEqual(summary["inventory"]["skipped_results"], 1)
        self.assertEqual(summary["inventory"]["malformed_results"], 1)
        self.assertEqual(summary["inventory"]["recorded_failed_attempts"], 2)
        self.assertEqual(summary["overview"]["saved_reviews"], 1)
        self.assertEqual(summary["overview"]["published_reviews"], 1)
        self.assertEqual(summary["overview"]["saved_findings"], 1)
        self.assertEqual(summary["overview"]["finding_kinds"], {"defect": 1})
        self.assertEqual(summary["overview"]["finding_severities"], {"high": 1})
        self.assertEqual(summary["spend"]["lifetime"]["request_count"], 7)
        self.assertEqual(summary["spend"]["lifetime"]["known_cost_micros"], 800)
        self.assertEqual(summary["spend"]["lifetime"]["reserved_micros"], 1200)
        self.assertEqual(summary["spend"]["lifetime"]["unknown_cost_request_count"], 2)
        self.assertEqual(summary["overview"]["archaeology"]["research_completed"], 1)
        self.assertEqual(summary["overview"]["archaeology"]["published_concerns"], 1)
        self.assertEqual(summary["overview"]["archaeology"]["assessment_status_counts"],
                         {"verified": 1})

        leaderboard = summary["stage_model_leaderboard"]
        self.assertFalse(any(item["stage"] == "archaeologist" for item in leaderboard))
        glm_leader = next(item for item in leaderboard
                          if item["stage"] == "adversarial_glm")
        self.assertEqual(glm_leader["configured_model"], "glm-5.3")
        self.assertEqual(glm_leader["accepted_findings"], 1)
        self.assertEqual(glm_leader["selected_candidate_findings"], 1)
        self.assertEqual(glm_leader["shared_findings"], 1)
        self.assertEqual(glm_leader["known_cost_micros"], 300)
        self.assertEqual(glm_leader["reserved_micros"], 700)
        glm_stage = next(item for item in summary["stages"]
                         if item["stage"] == "adversarial_glm")
        self.assertEqual(glm_stage["configured_model"], "glm-5.3")
        self.assertEqual(glm_stage["ledger"]["request_count"], 2)
        self.assertEqual(glm_stage["known_cost_per_accepted_finding_micros"], 300)
        archaeology = next(item for item in summary["stages"]
                           if item["stage"] == "archaeologist")
        self.assertTrue(archaeology["concept_stage"])
        self.assertEqual(archaeology["conceptual_concerns"], 1)
        self.assertEqual(archaeology["accepted_findings"], 0)
        self.assertEqual(archaeology["ledger"]["known_cost_micros"], 50)

        review = next(item for item in summary["reviews"]
                      if item["job_id"] == reviewed["id"])
        self.assertEqual(review["job_id"], reviewed["id"])
        self.assertEqual(review["report_name"], report_name)
        self.assertEqual(review["ledger"]["known_cost_micros"], 600)
        self.assertEqual(review["ledger"]["reserved_micros"], 1200)
        self.assertEqual(review["tokens"]["input"], 350)
        self.assertTrue(review["archaeology"]["research_completed"])
        self.assertTrue(review["archaeology"]["published_concern"])
        self.assertEqual(review["archaeology"]["status"], "verified")
        self.assertEqual(review["archaeology"]["citation_count"], 1)
        self.assertEqual(review["archaeology"]["alternative_count"], 1)
        self.assertEqual(summary["paired_sol_glm"]["completed_pairs"], 1)
        self.assertEqual(summary["paired_sol_glm"]["shared_accepted"]["adversarial_glm"], 1)

    def test_missing_history_does_not_create_databases_and_uses_readonly_uri(self):
        empty = self.state_dir / "empty"
        empty.mkdir()
        before = set(empty.iterdir())
        summary = stats.collect_stats(empty, empty / "reports")
        self.assertEqual(set(empty.iterdir()), before)
        self.assertTrue(summary["inventory"]["missing_sources"]["jobs_db"])
        self.assertTrue(summary["inventory"]["missing_sources"]["openai_ledger"])

        job = self.enqueue(50, "a" * 40)
        self.save_complete(job, self.result())
        real_connect = sqlite3.connect
        calls = []

        def connect(database, *args, **kwargs):
            calls.append((database, kwargs))
            return real_connect(database, *args, **kwargs)

        with patch.object(stats.sqlite3, "connect", side_effect=connect):
            stats.collect_stats(self.state_dir, self.report_dir)

        self.assertTrue(calls)
        for database, kwargs in calls:
            self.assertIn("mode=ro", database)
            self.assertTrue(kwargs["uri"])

    def test_current_month_provider_totals_do_not_reuse_lifetime_providers(self):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        old_month = "1999-01"
        self.create_ledger("spend.sqlite3", [
            ("current-openai", "pr:1:1:1", "independent", "gpt-6-luna",
             month, "settled", self.usage(100, 20), 10, 0),
        ])
        self.create_ledger("ppq-spend.sqlite3", [
            ("old-glm", "pr:2:2:1", "adversarial_glm", "z-ai/glm-5.3",
             old_month, "settled", self.usage(100, 20), 20, 0),
        ])

        summary = stats.collect_stats(self.state_dir, self.report_dir)

        self.assertEqual(
            summary["spend"]["current_month"]["providers"]["openai"][
                "known_cost_micros"], 10)
        self.assertNotIn("glm", summary["spend"]["current_month"]["providers"])
        self.assertEqual(
            summary["spend"]["lifetime"]["providers"]["glm"]["known_cost_micros"],
            20)

    def test_old_publish_decisions_without_attribution_do_not_credit_agents(self):
        job = self.enqueue(60, "a" * 40)
        self.save_complete(job, {
            "base_sha": "b" * 40,
            "head_sha": "a" * 40,
            "content": "Review body",
            "debug": {
                "coverage": {"status": "complete", "limitations": []},
                "candidate_sources": {"independent:1": "independent"},
                "decisions": [{
                    "candidate_ids": ["independent:1"],
                    "disposition": "publish",
                    "reason": "verified",
                    "finding": {
                        "kind": "defect",
                        "severity": "medium",
                        "path": "src/init.cpp",
                        "line": 5,
                        "side": "head",
                        "title": "Old finding",
                        "body": "Finding text",
                    },
                }],
                "stages": {
                    "independent": {
                        "model": "gpt-6-luna",
                        "status": "completed",
                        "turns": [],
                        "tools": [],
                    },
                },
            },
        })

        summary = stats.collect_stats(self.state_dir, self.report_dir)
        stage = next(item for item in summary["stages"]
                     if item["stage"] == "independent")
        review = next(item for item in summary["reviews"]
                      if item["job_id"] == job["id"])

        self.assertEqual(summary["overview"]["saved_findings"], 1)
        self.assertEqual(summary["overview"]["attribution_coverage"]["missing"], 1)
        self.assertEqual(stage["selected_candidate_findings"], 1)
        self.assertEqual(stage["accepted_findings"], 0)
        self.assertEqual(stage["attribution_unknown_reviews"], 1)
        self.assertFalse(review["ledger"]["cost_observed"])
        self.assertIsNone(review["ledger"]["known_cost_micros"])
        self.assertIsNone(
            summary["distributions"]["saved_review_known_cost_micros"]["median"])
        self.assertEqual(
            summary["distributions"]["saved_review_cost_missing_ledger_count"], 1)

    def test_snapshot_ledger_cost_joins_only_matching_generic_configuration(self):
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        generic = self.enqueue(70, "a" * 40)
        explicit = self.enqueue(71, "b" * 40)
        self.save_complete(generic, self.single_stage_result(
            "independent", "gpt-6.1-sol"))
        self.save_complete(explicit, self.single_stage_result(
            "independent", "gpt-6.1-sol-2026-08-01", head="b" * 40))
        generic_id = f'pr:70:{generic["id"]}:{generic["generation"]}'
        explicit_id = f'pr:71:{explicit["id"]}:{explicit["generation"]}'
        self.create_ledger("spend.sqlite3", [
            ("generic-snapshot", generic_id, "independent",
             "gpt-6.1-sol-2026-09-01", month, "settled",
             self.usage(100, 20), 111, 0),
            ("explicit-different-snapshot", explicit_id, "independent",
             "gpt-6.1-sol-2026-09-01", month, "settled",
             self.usage(100, 20), 222, 0),
        ])

        summary = stats.collect_stats(self.state_dir, self.report_dir)
        stages = {(item["stage"], item["configured_model"]): item
                  for item in summary["stages"]}
        spend_models = {(item["provider"], item["model"]): item
                        for item in summary["spend"]["models"]}

        generic_stage = stages[("independent", "gpt-6.1-sol")]
        configured_snapshot_stage = stages[
            ("independent", "gpt-6.1-sol-2026-08-01")]
        served_snapshot_stage = stages[
            ("independent", "gpt-6.1-sol-2026-09-01")]
        self.assertEqual(generic_stage["accepted_findings"], 1)
        self.assertEqual(generic_stage["ledger"]["known_cost_micros"], 111)
        self.assertEqual(
            generic_stage["known_cost_per_accepted_finding_micros"], 111)
        self.assertEqual(configured_snapshot_stage["accepted_findings"], 1)
        self.assertEqual(configured_snapshot_stage["ledger"]["known_cost_micros"], 0)
        self.assertEqual(served_snapshot_stage["accepted_findings"], 0)
        self.assertEqual(served_snapshot_stage["ledger"]["known_cost_micros"], 222)
        self.assertIn(("openai", "gpt-6.1-sol-2026-09-01"), spend_models)
        self.assertNotIn(("openai", "gpt-6.1-sol"), spend_models)

    def single_stage_result(self, stage_name, model, head="a" * 40):
        return {
            "base_sha": "b" * 40,
            "head_sha": head,
            "content": "Review body",
            "debug": {
                "coverage": {"status": "complete", "limitations": []},
                "candidate_sources": {f"{stage_name}:1": stage_name},
                "decisions": [{
                    "candidate_ids": [f"{stage_name}:1"],
                    "disposition": "publish",
                    "reason": "verified",
                    "finding": {
                        "kind": "defect",
                        "severity": "medium",
                        "path": "src/node.cpp",
                        "line": 1,
                        "side": "head",
                        "title": "Finding",
                        "body": "Finding text",
                    },
                }],
                "finding_attribution": [{
                    "finding_id": "finding:1",
                    "title": "Finding",
                    "path": "src/node.cpp",
                    "line": 1,
                    "side": "head",
                    "candidate_ids": [f"{stage_name}:1"],
                    "raised_by": [stage_name],
                    "raised_by_models": [model],
                    "verified_by": "verifier",
                    "edited_by": "collator",
                }],
                "stages": {
                    stage_name: {
                        "model": model,
                        "status": "completed",
                        "turns": [],
                        "tools": [],
                    },
                },
            },
        }

    def test_research_outcomes_and_publication_are_counted_separately(self):
        overview = {"archaeology": {"research_completed": 0, "saved_concerns": 0,
                                    "published_concerns": 0, "assessment_status_counts": {}}}
        stats._add_archaeology_counts(overview, True,
            {"status": "no_concern", "published_concern": False}, True)
        stats._add_archaeology_counts(overview, True,
            {"status": "verified", "published_concern": True}, False)
        self.assertEqual(overview["archaeology"], {
            "research_completed": 2, "saved_concerns": 1, "published_concerns": 0,
            "assessment_status_counts": {"no_concern": 1, "verified": 1}})


if __name__ == "__main__":
    unittest.main()
