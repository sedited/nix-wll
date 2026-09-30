import json
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

from forgejo_review_bot import model, trace
from forgejo_review_bot.spend import BudgetExceeded, RequestBudget


class Response:
    def __init__(self, result):
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, *_args):
        return json.dumps(self.result).encode()


class FakeBudget:
    def __init__(self):
        self.events = []

    def reserve(self, stage, data):
        self.events.append(("reserve", stage, data["model"]))
        return "token"

    def settle(self, token, response):
        self.events.append(("settle", token, response.get("id")))

    def fail(self, token, charged_unknown=True):
        self.events.append(("fail", token, charged_unknown))


class ModelBudgetTests(unittest.TestCase):
    def setUp(self):
        self.models = {name: "gpt-6-luna" for name in
                       ("independent", "adversarial", "state", "public_contract",
                        "tests", "developer_notes", "design", "verifier", "collator")}
        self.prompt_config = SimpleNamespace(instructions="Review.", models=self.models)
        self.snapshot = SimpleNamespace(checkout=".", head_files=[],
                                        base_files=[], changed_paths=[],
                                        merge_base="base", head_sha="head")

    def test_settles_before_callback_and_refunds_only_definite_http_rejections(self):
        budget = FakeBudget()
        events = budget.events
        result = {"id": "response-1", "model": "gpt-6-luna", "status": "completed",
                  "output": [], "usage": {"input_tokens": 10, "output_tokens": 4}}

        def callback(stage, _payload, _response):
            events.append(("callback", stage))

        with patch.object(model.urllib.request, "urlopen", return_value=Response(result)):
            model.request_response("secret", {"model": "gpt-6-luna"}, "tests",
                                  callback, budget)
        self.assertEqual([event[0] for event in events], ["reserve", "settle", "callback"])

        for code, expected_unknown in ((400, False), (429, False), (500, True)):
            budget.events.clear()
            error = urllib.error.HTTPError("url", code, "rejected", {}, None)
            with patch.object(model.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(urllib.error.HTTPError):
                    model.request_response("secret", {"model": "gpt-6-luna"},
                                          "tests", budget=budget)
            self.assertEqual(budget.events[-1], ("fail", "token", expected_unknown))

    def test_schema_effort_and_full_audit_output_are_recorded(self):
        raw = "Finding. " * 1000
        result = {"id": "response-schema", "model": "gpt-6-luna", "status": "completed",
                  "output": [{"type": "message", "content": [
                      {"type": "output_text", "text": raw}]}],
                  "usage": {"input_tokens": 100, "output_tokens": 200}}
        debug = {}
        schema = {"type": "object", "properties": {"finding": {"type": "string"}},
                  "required": ["finding"], "additionalProperties": False}
        with patch.object(model.urllib.request, "urlopen", return_value=Response(result)) as send:
            answer, record = model.run_audit(
                "secret", "state", "Inspect state", "PR text", self.prompt_config,
                response_schema=schema, reasoning_effort="medium", debug=debug)
        request = json.loads(send.call_args.args[0].data)
        self.assertEqual(request["reasoning"]["effort"], "medium")
        self.assertEqual(request["text"]["format"], {
            "type": "json_schema", "name": "state", "strict": True, "schema": schema})
        self.assertEqual(answer, raw)
        self.assertEqual(record["raw_output"], raw)
        self.assertEqual(debug["raw_output"], raw)

    def test_discussions_can_be_hidden_and_unadvertised_calls_are_rejected(self):
        call = {"type": "function_call", "call_id": "call-1",
                "name": "search_discussions", "arguments": '{"query":"x"}'}
        results = [
            {"id": "response-tool", "model": "gpt-6-luna", "status": "completed",
             "output": [call], "usage": {"input_tokens": 10, "output_tokens": 2}},
            {"id": "response-final", "model": "gpt-6-luna", "status": "completed",
             "output": [{"type": "message", "content": [
                 {"type": "output_text", "text": "No findings."}]}],
             "usage": {"input_tokens": 15, "output_tokens": 3}},
        ]
        with patch.object(model.urllib.request, "urlopen",
                          side_effect=[Response(item) for item in results]) as send, \
                patch.object(model.forgejo, "search_discussions") as lookup:
            answer = model.openai_review(
                "secret", "patch", self.snapshot, SimpleNamespace(), self.prompt_config,
                allow_discussions=False)
        lookup.assert_not_called()
        self.assertEqual(answer, "No findings.")
        sent = json.loads(send.call_args_list[0].args[0].data)
        self.assertNotIn("search_discussions", {tool["name"] for tool in sent["tools"]})

    def test_stale_review_stops_before_reserving_or_sending(self):
        budget = FakeBudget()
        with patch.object(model.urllib.request, "urlopen") as send:
            with self.assertRaises(model.StaleReview):
                model.request_response("secret", {"model": "gpt-6-luna"}, "audit",
                                      budget=budget, is_current=lambda: False)
        self.assertEqual(budget.events, [])
        send.assert_not_called()

    def test_inspection_limit_returns_final_answer_and_records_skipped_calls(self):
        calls = [{"type": "function_call", "call_id": f"call-{i}",
                  "name": "read_file", "arguments": '{"path":"src/a.cpp","start_line":1}'}
                 for i in range(3)]
        raw = '{"coverage":{"status":"partial","limitations":["Caller not inspected"]}}'
        results = [
            {"id": "tools", "status": "completed", "output": calls},
            {"id": "final", "status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": raw}]}]},
        ]
        debug = {}
        budget = FakeBudget()
        with patch.object(model.urllib.request, "urlopen",
                          side_effect=[Response(item) for item in results]) as send, \
                patch.object(model, "read_file", return_value="1: evidence") as read:
            answer = model.openai_review(
                "secret", "patch", self.snapshot, SimpleNamespace(), self.prompt_config,
                debug, max_tool_calls=2, budget=budget,
                reasoning_effort="xhigh", max_output_tokens=25_000)
        self.assertEqual(answer, raw)
        self.assertEqual(read.call_count, 2)
        final_request = json.loads(send.call_args_list[-1].args[0].data)
        self.assertEqual(final_request["tool_choice"], "none")
        self.assertEqual(final_request["reasoning"], {"effort": "xhigh"})
        self.assertEqual(final_request["max_output_tokens"], 25_000)
        self.assertIn("Inspection limit reached", final_request["input"][-1]["output"])
        self.assertEqual(debug["max_tool_calls"], 2)
        self.assertEqual(debug["reasoning_effort"], "xhigh")
        self.assertEqual(debug["max_output_tokens"], 25_000)
        self.assertEqual(debug["tools"][-1]["skipped"], "inspection_limit")
        self.assertNotIn("skipped", debug["tools"][0])
        self.assertEqual([event[0] for event in budget.events],
                         ["reserve", "settle", "reserve", "settle"])

    def test_sequential_inspections_can_use_allowance_before_final_answer(self):
        results = [{"status": "completed", "output": [
            {"type": "function_call", "call_id": f"call-{i}", "name": "read_file",
             "arguments": '{"path":"src/a.cpp","start_line":1}'}]} for i in range(12)]
        results.append({"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Review complete."}]}]})
        with patch.object(model.urllib.request, "urlopen",
                          side_effect=[Response(item) for item in results]) as send, \
                patch.object(model, "read_file", return_value="1: evidence") as read:
            answer = model.openai_review(
                "secret", "patch", self.snapshot, SimpleNamespace(), self.prompt_config,
                max_tool_calls=12)
        self.assertEqual(answer, "Review complete.")
        self.assertEqual(read.call_count, 12)
        choices = [json.loads(call.args[0].data)["tool_choice"] for call in send.call_args_list]
        self.assertEqual(choices, ["required"] + ["auto"] * 11 + ["none"])

    def test_stale_review_between_turns_does_not_reserve_again(self):
        call = {"type": "function_call", "call_id": "call-1",
                "name": "read_file", "arguments": '{"path":"src/a.cpp","start_line":1}'}
        first = {"id": "response-tool", "model": "gpt-6-luna", "status": "completed",
                 "output": [call], "usage": {"input_tokens": 10, "output_tokens": 2}}
        budget = FakeBudget()
        states = iter([True, False])
        with patch.object(model.urllib.request, "urlopen", return_value=Response(first)) as send, \
                patch.object(model, "read_file", return_value="1: content"):
            with self.assertRaises(model.StaleReview):
                model.openai_review(
                    "secret", "patch", self.snapshot, SimpleNamespace(), self.prompt_config,
                    budget=budget, is_current=lambda: next(states))
        send.assert_called_once()
        self.assertEqual([event[0] for event in budget.events], ["reserve", "settle"])

    def test_incomplete_audit_keeps_raw_output_and_reason(self):
        raw = "Partial answer." * 500
        response = {"id": "response-partial", "model": "gpt-6-luna",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "message", "content": [
                        {"type": "output_text", "text": raw}]}],
                    "usage": {"input_tokens": 100, "output_tokens": 4000}}
        stage = {"model": "gpt-6-luna", "status": "running", "turns": [], "tools": []}
        with patch.object(model.urllib.request, "urlopen", return_value=Response(response)):
            answer, record = model.run_audit(
                "secret", "state", "Inspect", "patch", self.prompt_config, debug=stage)
        self.assertEqual(answer, raw)
        self.assertEqual(record["raw_output"], raw)
        self.assertEqual(record["incomplete_reason"], "max_output_tokens")
        self.assertEqual(stage["raw_output"], raw)
        self.assertEqual(stage["turns"][0]["response_id"], "response-partial")

    def test_trace_prices_each_turn_and_clips_public_raw_output(self):
        raw = "X" * (trace.MAX_PUBLIC_STAGE_OUTPUT_BYTES + 1)
        stage = {"model": "gpt-6-luna", "status": "completed",
                 "raw_output": raw, "tools": [], "turns": [
                     {"model": "gpt-6-luna-2026-09-29", "response_id": "r1",
                      "status": "completed", "request_bytes": 12,
                      "input_tokens": 100, "cached_tokens": 10,
                      "cache_write_tokens": None, "output_tokens": 20,
                      "elapsed_seconds": 1.0},
                     {"model": "gpt-6-luna", "status": "failed",
                      "request_bytes": 10, "input_tokens": None,
                      "output_tokens": None, "elapsed_seconds": None} ]}
        debug = {"stages": {"state": stage}, "routing": {"mode": "enabled"},
                 "coverage": {"status": "partial"}, "budget": {"unknown": 1}}
        metrics = trace.review_metrics(debug, self.prompt_config)
        public = trace.review_trace(debug, self.prompt_config)
        self.assertEqual(metrics["known_usage_count"], 1)
        self.assertEqual(metrics["unknown_usage_count"], 1)
        self.assertEqual(metrics["incomplete_usage_count"], 1)
        self.assertFalse(metrics["usage_complete"])
        self.assertEqual(len(public["stages"]["state"]["raw_output"].encode()),
                         trace.MAX_PUBLIC_STAGE_OUTPUT_BYTES)
        self.assertTrue(public["stages"]["state"]["raw_output_truncated"])
        self.assertEqual(public["routing"], debug["routing"])
        self.assertEqual(public["coverage"], debug["coverage"])
        self.assertEqual(public["budget"], debug["budget"])

    def test_budget_rejection_has_no_request_turn(self):
        class RejectedBudget:
            def __init__(self):
                self.choices = []

            def reserve(self, _stage, _data):
                self.choices.append(_data["tool_choice"])
                raise BudgetExceeded("limit")

        budget = RejectedBudget()
        stage = {"model": "gpt-6-luna", "status": "running", "turns": [], "tools": []}
        with patch.object(model.urllib.request, "urlopen") as send:
            with self.assertRaises(BudgetExceeded):
                model.openai_review("secret", "patch", self.snapshot, SimpleNamespace(),
                                    self.prompt_config, stage, budget=budget)
        send.assert_not_called()
        self.assertEqual(budget.choices, ["required"])
        self.assertEqual(stage["turns"], [])
        self.assertEqual(stage["status"], "budget_exhausted")

    def test_budget_rejection_after_inspection_finishes_without_tools(self):
        class OneInspectionBudget:
            def __init__(self):
                self.events = []

            def reserve(self, stage, data):
                self.events.append(("reserve", stage, data["tool_choice"]))
                if data["tool_choice"] == "auto":
                    raise BudgetExceeded("limit")
                return "token"

            def settle(self, token, response):
                self.events.append(("settle", token, response.get("id")))

            def fail(self, token, charged_unknown=True):
                self.events.append(("fail", token, charged_unknown))

        raw = json.dumps({
            "coverage": {"status": "partial",
                         "limitations": ["Budget prevented more inspection"]},
            "findings": [],
            "requires_sensitive_review": False,
        })
        first = {"id": "first-tool", "model": "gpt-6-luna",
                 "status": "completed", "output": [
                     {"type": "function_call", "call_id": "call-1",
                      "name": "read_file",
                      "arguments": '{"path":"src/a.cpp","start_line":1}'}]}
        final = {"id": "forced-final", "model": "gpt-6-luna",
                 "status": "completed", "output": [
                     {"type": "message", "content": [
                         {"type": "output_text", "text": raw}]}]}
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        debug = {}
        budget = OneInspectionBudget()
        with patch.object(model.urllib.request, "urlopen",
                          side_effect=[Response(first), Response(final)]) as send, \
                patch.object(model, "read_file", return_value="1: evidence") as read:
            answer = model.openai_review(
                "secret", "patch", self.snapshot, SimpleNamespace(),
                self.prompt_config, debug, budget=budget, response_schema=schema,
                api_base="https://api.ppq.ai/v1")
        self.assertEqual(answer, raw)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(send.call_count, 2)
        self.assertEqual([call.args[0].full_url for call in send.call_args_list],
                         ["https://api.ppq.ai/v1/responses"] * 2)
        sent = json.loads(send.call_args_list[-1].args[0].data)
        self.assertEqual(sent["tool_choice"], "none")
        self.assertEqual(sent["input"][0]["role"], "user")
        self.assertEqual(sent["input"][1]["type"], "function_call")
        self.assertEqual(sent["input"][2]["type"], "function_call_output")
        self.assertIn("Budget is near", sent["input"][-1]["content"])
        self.assertEqual([turn["tool_choice"] for turn in debug["turns"]],
                         ["required", "none"])
        self.assertTrue(debug["turns"][1]["budget_forced_final"])
        self.assertEqual(budget.events,
                         [("reserve", "independent", "required"),
                          ("settle", "token", "first-tool"),
                          ("reserve", "independent", "auto"),
                          ("reserve", "independent", "none"),
                          ("settle", "token", "forced-final")])

    def test_request_budget_applies_scaled_stage_headroom(self):
        class LedgerSpy:
            review_limit_micros = 300_000

            def __init__(self):
                self.calls = []

            def reserve_estimate_micros(self, model, data, extra_input_tokens=0):
                return 1000 if model == "gpt-6-luna" else 10_000

            def reserve(self, stage, model, data, review_id, reserve_floor_usd=0,
                        reserve_floor_micros=0):
                self.calls.append((stage, model, review_id, reserve_floor_micros))
                return "token"

        ledger = LedgerSpy()
        budget = RequestBudget(ledger, "review-1")
        data = {"model": "gpt-6-luna", "max_output_tokens": 1}
        budget.reserve("independent", data)
        budget.reserve("verifier", data)
        budget.reserve("collator", data)
        self.assertEqual([call[3] for call in ledger.calls], [40_000, 0, 0])

        protection = budget.protect_verifier(
            {"model": "gpt-6.1-sol", "max_output_tokens": 1,
             "tools": [{"type": "function"}], "tool_choice": "required"})
        self.assertEqual(protection["protected_micros"], 20_000)
        self.assertEqual(protection["initial_usd"], 0.01)
        self.assertEqual(protection["continuation_usd"], 0.01)
        budget.reserve("independent", data)
        budget.reserve("verifier", data)
        self.assertEqual([call[3] for call in ledger.calls][-2:], [20_000, 0])


if __name__ == "__main__":
    unittest.main()
