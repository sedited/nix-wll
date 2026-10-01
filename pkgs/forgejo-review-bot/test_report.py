import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from forgejo_review_bot import report, trace


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.prompt_config = SimpleNamespace(
            models={
                "independent": "gpt-6-luna",
                "tests": "gpt-6-luna",
                "verifier": "gpt-6-luna",
                "collator": "gpt-6-luna",
            }
        )

    def debug(self, raw_output="raw reply"):
        return {
            "instructions": "private instructions",
            "review_input": "private patch",
            "review_input_sha256": "hash",
            "private_state": {"token": "credential-marker"},
            "coverage": {"status": "partial", "limitations": ["Verification was partial."]},
            "concept_assessment": {
                "status": "verified",
                "stage": "archaeologist",
                "summary": "Fix it in the policy layer. Sources: [1](https://code.example.org/bitcoin/bitcoin/pulls/1#issuecomment-2)",
                "private": "concept-private-marker",
                "candidate": self.concept_brief(),
                "verification": {
                    "disposition": "publish",
                    "reason": "History supports publishing this concern.",
                    "assessment": self.concept_brief(),
                    "private": "verification-private-marker",
                },
            },
            "candidate_sources": {"tests:1": "tests"},
            "decisions": [{
                "candidate_ids": ["tests:1"],
                "disposition": "publish",
                "reason": "Verified from changed code.",
                "finding": {
                    "kind": "defect",
                    "severity": "major",
                    "path": "src/node.cpp",
                    "line": 7,
                    "side": "head",
                    "title": "Breaks startup",
                    "body": "The verified finding body.",
                    "private": "decision-private-marker",
                },
                "private": "decision-private-marker",
            }],
            "finding_attribution": [{
                "finding_id": "finding:1",
                "title": "Breaks startup",
                "path": "src/node.cpp",
                "line": 7,
                "side": "head",
                "candidate_ids": ["tests:1"],
                "raised_by": ["tests"],
                "raised_by_models": ["gpt-6-luna"],
                "verified_by": "verifier",
                "verified_by_model": "gpt-6-luna",
                "edited_by": "collator",
                "edited_by_model": "gpt-6-luna",
                "private": "attribution-private-marker",
            }],
            "stage_outputs": {"tests": raw_output},
            "stages": {
                "tests": {
                    "model": "gpt-6-luna",
                    "status": "completed",
                    "raw_output": raw_output,
                    "coverage": {"status": "complete", "limitations": []},
                    "turns": [{
                        "model": "gpt-6-luna",
                        "request_bytes": 123,
                        "input_tokens": 100,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "output_tokens": 20,
                        "elapsed_seconds": 1.5,
                        "input": "private turn input marker",
                        "instructions": "private turn instructions marker",
                    }],
                    "tools": [{
                        "name": "read_file",
                        "arguments": "private tool arguments marker",
                        "output": "private tool output marker",
                        "output_bytes": 50,
                        "output_sha256": "a" * 64,
                        "hosted": True,
                        "action_type": "search",
                        "retrieved_at": "2026-10-01T00:00:00Z",
                        "sources": [
                            "https://code.example.org/source",
                            "javascript:alert(1)",
                            "https://[",
                        ],
                    }],
                    "private": "stage-private-marker",
                }
            },
        }

    def concept_brief(self):
        return {
            "problem": "The PR changes peer eviction policy.",
            "baseline": "Leave the old eviction behavior alone.",
            "delivered_benefit": "Makes one operator workflow cheaper.",
            "relevant_history": "Earlier attempts stalled on unclear deployment impact.",
            "recommendation": "Use the submitted concept only if the history still applies.",
            "decisive_question": "Do current deployments still hit the old failure mode?",
            "technical_assumptions": ["The verifier still needs to check the policy path."],
            "alternatives": [{
                "name": "Do nothing",
                "provenance": "history",
                "concept": "Keep the existing policy.",
                "benefit": "No churn",
                "cost": "Known operator pain remains",
                "unresolved": "Whether the old pain still matters.",
                "citations": ["https://code.example.org/bitcoin/bitcoin/pulls/2"],
                "private": "alternative-private-marker",
            }],
            "citations": [
                "https://code.example.org/bitcoin/bitcoin/pulls/1#issuecomment-2",
                "javascript:alert(1)",
                "https://[",
            ],
            "private": "source-private-marker",
        }

    def save(self, directory, debug=None, content="Verified review text."):
        return report.save_report(
            Path(directory), 123, "a" * 40, content, debug or self.debug(),
            self.prompt_config, "123-abcdef"
        )

    def test_report_keeps_full_stage_replies_untruncated(self):
        raw = "X" * (trace.MAX_PUBLIC_STAGE_OUTPUT_BYTES + 500)
        with tempfile.TemporaryDirectory() as directory:
            html_name = self.save(directory, self.debug(raw))
            public = json.loads((Path(directory) / "123-abcdef.json").read_text())
            html = (Path(directory) / html_name).read_text()

        self.assertEqual(public["stages"]["tests"]["raw_output"], raw)
        self.assertEqual(public["stage_outputs"]["tests"], raw)
        self.assertNotIn("truncated", public["stage_outputs"])
        self.assertIn(raw, html)

    def test_json_stage_reply_renders_readable_findings_without_changing_download(self):
        raw = json.dumps({"coverage": {"status": "partial", "limitations": ["needs run"]},
                          "requires_sensitive_review": False,
                          "findings": [{
                              "kind": "defect",
                              "path": "src/node.cpp",
                              "line": 7,
                              "side": "head",
                              "title": "Startup regression",
                              "claim": "The daemon exits before loading settings.",
                              "consequence": "Nodes cannot start with this config.",
                              "evidence": "The new guard rejects the default value.",
                              "correction": "Accept the default before validation.",
                              "uncertainty": "Did not run integration tests.",
                          }, {"title": "Edited finding", "body": "Verified finding text."}]},
                         separators=(",", ":"))
        with tempfile.TemporaryDirectory() as directory:
            html_name = self.save(directory, self.debug(raw))
            public = json.loads((Path(directory) / "123-abcdef.json").read_text())
            html = (Path(directory) / html_name).read_text()

        self.assertEqual(public["stages"]["tests"]["raw_output"], raw)
        self.assertIn("<h4>Parsed Findings</h4>", html)
        self.assertIn("Startup regression", html)
        self.assertIn("<strong>Claim:</strong> The daemon exits before loading settings.", html)
        self.assertIn("<strong>Body:</strong> Verified finding text.", html)
        self.assertIn("Formatted JSON", html)
        self.assertIn('{\n  &quot;coverage&quot;: {', html)
        self.assertIn("Raw full reply", html)

    def test_concept_assessment_renders_without_becoming_a_finding(self):
        with tempfile.TemporaryDirectory() as directory:
            html_name = self.save(directory)
            public = json.loads((Path(directory) / "123-abcdef.json").read_text())
            html = (Path(directory) / html_name).read_text()

        assessment = public["concept_assessment"]
        self.assertEqual(assessment["status"], "verified")
        self.assertEqual(assessment["verification"]["disposition"], "publish")
        self.assertEqual(assessment["verification"]["assessment"]["alternatives"][0]["name"],
                         "Do nothing")
        self.assertEqual(
            assessment["verification"]["assessment"]["alternatives"][0]["concept"],
            "Keep the existing policy.")
        self.assertEqual(len(public["finding_attribution"]), 1)
        self.assertIn("Concept and Approach", html)
        self.assertIn("Earlier attempts stalled on unclear deployment impact.", html)
        self.assertIn(
            'href="https://code.example.org/bitcoin/bitcoin/pulls/1#issuecomment-2"',
            html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn('href="https://["', html)

    def test_report_escapes_hostile_markup(self):
        raw = "</pre><script>alert('x')</script><pre>"
        content = "Verified </pre><script>alert('review')</script>"
        with tempfile.TemporaryDirectory() as directory:
            html_name = self.save(directory, self.debug(raw), content)
            html = (Path(directory) / html_name).read_text()

        self.assertIn("&lt;/pre&gt;&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;&lt;pre&gt;", html)
        self.assertIn("&lt;/pre&gt;&lt;script&gt;alert(&#x27;review&#x27;)&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_public_json_whitelist_excludes_private_debug_data(self):
        with tempfile.TemporaryDirectory() as directory:
            html_name = self.save(directory)
            public_text = (Path(directory) / "123-abcdef.json").read_text()
            html = (Path(directory) / html_name).read_text()

        for private in (
            "private instructions",
            "private patch",
            "credential-marker",
            "private turn input marker",
            "private turn instructions marker",
            "private tool arguments marker",
            "private tool output marker",
            "stage-private-marker",
            "decision-private-marker",
            "attribution-private-marker",
            "concept-private-marker",
            "alternative-private-marker",
            "source-private-marker",
            "verification-private-marker",
            "review_input_sha256",
        ):
            self.assertNotIn(private, public_text)
            self.assertNotIn(private, html)
        public = json.loads(public_text)
        self.assertEqual(public["review"], "Verified review text.")
        self.assertEqual(public["metrics"]["tool_calls"], 1)
        self.assertEqual(public["stages"]["tests"]["tools"], [{
            "name": "read_file",
            "output_bytes": 50,
            "output_sha256": "a" * 64,
            "hosted": True,
            "action_type": "search",
            "retrieved_at": "2026-10-01T00:00:00Z",
            "sources": ["https://code.example.org/source"],
        }])

    def test_files_are_public_readable_and_named_from_report_id(self):
        with tempfile.TemporaryDirectory() as directory:
            html_name = self.save(directory)
            root = Path(directory)

            self.assertEqual(html_name, "123-abcdef.html")
            self.assertTrue((root / "123-abcdef.html").is_file())
            self.assertTrue((root / "123-abcdef.json").is_file())
            self.assertEqual((root / "123-abcdef.html").stat().st_mode & 0o777, 0o644)
            self.assertEqual((root / "123-abcdef.json").stat().st_mode & 0o777, 0o644)
            self.assertFalse(list(root.glob("*.tmp")))

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "safe filename"):
                report.save_report(directory, 1, "h", "content", {}, self.prompt_config, "../bad")

    def test_writes_use_temp_file_then_atomic_replace(self):
        calls = []
        real_replace = os.replace

        def replace(src, dst):
            calls.append((Path(src), Path(dst), Path(src).exists(), Path(dst).exists()))
            real_replace(src, dst)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(report.os, "replace", side_effect=replace):
            self.save(directory)

        self.assertEqual([dst.name for _, dst, _, _ in calls],
                         ["123-abcdef.json", "123-abcdef.html"])
        for src, dst, src_existed, dst_existed in calls:
            self.assertTrue(src.name.startswith(f".{dst.name}."))
            self.assertTrue(src_existed)
            self.assertFalse(dst_existed)
