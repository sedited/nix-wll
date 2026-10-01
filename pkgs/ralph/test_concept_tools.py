import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ralph import model


class ConceptToolTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = SimpleNamespace(checkout="checkout", head_files={"new.cpp": "head"},
                                        base_files={"old.cpp": "base"}, changed_paths={"new.cpp"},
                                        merge_base="base-sha", head_sha="head-sha")
        self.prompts = SimpleNamespace(instructions="Review.", models={"independent": "gpt-6-luna"})

    def run_tools(self, calls, **kwargs):
        responses = [
            {"status": "completed", "output": [
                {"type": "function_call", "call_id": str(index), "name": name,
                 "arguments": json.dumps(args)} for index, (name, args) in enumerate(calls)]},
            {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "Assessment complete."}]}]},
        ]
        requests = []

        def request(_key, data, *_args, **_kwargs):
            requests.append(json.loads(json.dumps(data)))
            return responses.pop(0), json.dumps(data).encode(), 0.01

        with patch.object(model, "request_response", side_effect=request):
            model.openai_review("key", "Problem and baseline.", self.snapshot,
                                SimpleNamespace(), self.prompts, current_pr=42, **kwargs)
        return requests

    def test_concept_can_inspect_actual_base_and_head_behavior(self):
        with patch.object(model, "read_file", return_value="evidence") as read:
            self.run_tools([
                ("read_file", {"path": "new.cpp", "start_line": 1}),
                ("read_base_file", {"path": "old.cpp", "start_line": 1}),
            ], tools=model.ARCHAEOLOGY_TOOLS)
        self.assertEqual([call.args[1] for call in read.call_args_list],
                         [self.snapshot.head_files, self.snapshot.base_files])

    def test_blind_tools_search_only_base_and_reject_head_and_discussions(self):
        with patch.object(model, "search_code", return_value="base matches") as search, \
                patch.object(model, "read_file", return_value="base contents") as read, \
                patch.object(model, "read_diff") as diff, \
                patch.object(model.forgejo, "read_current_pr_discussion") as discussion:
            requests = self.run_tools([
                ("find_base_paths", {"query": ".cpp"}),
                ("search_base_code", {"query": "existing facility"}),
                ("read_base_file", {"path": "old.cpp", "start_line": 1}),
                ("read_file", {"path": "new.cpp", "start_line": 1}),
                ("read_diff", {"path": "new.cpp", "start_line": 1}),
                ("read_current_pr_discussion", {"kind": "comments", "page": 1, "review_id": 0}),
            ], tools=model.BASE_TOOLS)
        search.assert_called_once_with("checkout", "base-sha", "existing facility")
        read.assert_called_once_with("checkout", self.snapshot.base_files, "old.cpp", 1)
        diff.assert_not_called()
        discussion.assert_not_called()
        outputs = [item["output"] for item in requests[-1]["input"]
                   if item.get("type") == "function_call_output"]
        self.assertEqual(outputs[0], "old.cpp\n")
        self.assertEqual(outputs[-3:], ["Unknown or unavailable tool."] * 3)

    def test_frozen_research_never_advertises_hosted_web_or_calls_live_readers(self):
        evidence = {"frozen": "fixture"}
        for allow_live in (False, True):
            with self.subTest(allow_live=allow_live), \
                    patch.object(model.research, "lookup", return_value="Frozen discussion") as lookup, \
                    patch.object(model.forgejo, "read_current_pr_discussion") as live:
                requests = self.run_tools([
                    ("read_current_pr_discussion", {"kind": "comments", "page": 1, "review_id": 0}),
                ], tools=model.ARCHAEOLOGY_TOOLS, allow_discussions=allow_live,
                    research_evidence=evidence)
            live.assert_not_called()
            lookup.assert_called_once_with(evidence, "read_current_pr_discussion",
                                           {"kind": "comments", "page": 1, "review_id": 0})
            self.assertNotIn("web_search", {tool["type"] for tool in requests[0]["tools"]})
            self.assertEqual(requests[-1]["input"][-1]["output"], "Frozen discussion")


if __name__ == "__main__":
    unittest.main()
