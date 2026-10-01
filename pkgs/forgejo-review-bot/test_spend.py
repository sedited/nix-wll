import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from forgejo_review_bot.spend import (MICRODOLLARS, FINAL_TOOL_OUTPUT_HEADROOM_TOKENS,
                                      BudgetExceeded, Ledger, RequestBudget,
                                      price_usd, supports_model)


class SpendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "spend.sqlite"

    def tearDown(self):
        self.directory.cleanup()

    @staticmethod
    def payload(output=100):
        return {"max_output_tokens": output, "input": "review"}

    def test_web_search_reserves_limit_and_settles_search_actions(self):
        ledger = Ledger(self.path, review_limit_usd=1, input_padding_tokens=0)
        payload = {**self.payload(), "tools": [{"type": "web_search"}],
                   "max_tool_calls": 3}
        estimate = ledger.reserve_estimate_micros("gpt-6-luna", payload)
        self.assertGreater(estimate, 30_000)
        self.assertLess(ledger.reserve_estimate_micros(
            "gpt-6-luna", {**payload, "tool_choice": "none"}), 1_000)
        with self.assertRaisesRegex(ValueError, "max_tool_calls"):
            ledger.reserve_estimate_micros(
                "gpt-6-luna", {**self.payload(), "tools": payload["tools"]})
        token = ledger.reserve("archaeologist", "gpt-6-luna", payload, "pr-1")
        response = {"id": "search-response", "model": "gpt-6-luna",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                    "output": [{"type": "web_search_call", "action": {"type": action}}
                               for action in ("search", "open_page", "find_in_page", "search")]}
        ledger.settle(token, response)
        self.assertEqual(ledger.summary(review_id="pr-1")["estimated_total_usd"],
                         float(price_usd("gpt-6-luna", {
                             **response["usage"], "web_search_calls": 2})))
        self.assertGreater(ledger.summary(review_id="pr-1")["estimated_total_usd"], 0.02)
        # Replayed provider responses do not charge the searches twice.
        ledger.settle(token, response)
        self.assertLess(ledger.summary(review_id="pr-1")["estimated_total_usd"], 0.021)

    def test_price_uses_request_model_and_long_context_rates(self):
        normal = {"input_tokens": 1_000_000,
                  "input_tokens_details": {"cached_tokens": 100_000,
                                           "cache_write_tokens": 100_000},
                  "output_tokens": 1_000_000}
        self.assertEqual(price_usd("gpt-6-luna", normal).as_tuple().exponent, -6)
        self.assertEqual(str(price_usd("gpt-6-luna", normal)), "0.937000")

        long_context = {"input_tokens": 272_001,
                        "input_tokens_details": {"cached_tokens": 0,
                                                 "cache_write_tokens": 0},
                        "output_tokens": 100}
        self.assertEqual(str(price_usd("gpt-6-luna", long_context)), "0.054476")
        self.assertEqual(str(price_usd("gpt-6.1-sol", long_context)), "1.089504")
        snapshot = "gpt-6-luna-2026-09-29"
        self.assertEqual(price_usd(snapshot, long_context),
                         price_usd("gpt-6-luna", long_context))
        self.assertIsNone(price_usd("gpt-6-luna-unrecognized", long_context))
        self.assertTrue(supports_model("gpt-6.1-sol"))
        self.assertFalse(supports_model("gpt-6-luna-unrecognized"))

    def test_glm_rates_have_no_cache_discount_or_long_context_multiplier(self):
        usage = {"input_tokens": 1_000_000,
                 "input_tokens_details": {"cached_tokens": 100_000,
                                          "cache_write_tokens": 100_000},
                 "output_tokens": 1_000_000}
        self.assertEqual(str(price_usd("glm-5.3", usage)), "6.119000")
        self.assertEqual(price_usd("z-ai/glm-5.3", usage),
                         price_usd("glm-5.3", usage))
        self.assertTrue(supports_model("glm-5.3"))
        self.assertTrue(supports_model("z-ai/glm-5.3"))
        del usage["input_tokens_details"]
        self.assertEqual(str(price_usd("glm-5.3", usage)), "6.119000")
        ledger = Ledger(self.path, review_limit_usd=10, input_padding_tokens=0)
        payload = {"max_output_tokens": 1_000_000}
        expected_input = ledger._payload_bytes(payload) + 1_000_000
        estimate = ledger.reserve_estimate_micros(
            "glm-5.3", payload, extra_input_tokens=1_000_000)
        self.assertEqual(estimate,
                         int(price_usd("glm-5.3", {"input_tokens": expected_input,
                             "output_tokens": 1_000_000}) * MICRODOLLARS))

    def test_missing_usage_is_unknown_and_missing_cache_writes_is_conservative(self):
        self.assertIsNone(price_usd("gpt-6-luna", {"input_tokens": 10}))
        usage = {"input_tokens": 1_000_000, "output_tokens": 0,
                 "input_tokens_details": {"cached_tokens": 0}}
        self.assertEqual(str(price_usd("gpt-6-luna", usage)), "0.250000")

        ledger = Ledger(self.path, review_limit_usd=1)
        token = ledger.reserve("audit", "gpt-6-luna", self.payload(), "pr-1")
        self.assertTrue(ledger.settle(token, {"id": "resp-1", "model": "gpt-6-luna",
                                             "usage": {"input_tokens": 10}}))
        summary = ledger.summary(review_id="pr-1")
        self.assertEqual(summary["unknown_request_count"], 1)
        self.assertEqual(summary["incomplete_usage_count"], 1)
        self.assertGreater(summary["reserved_total_usd"], 0)

    def test_reservations_enforce_limits_in_parallel(self):
        for monthly in (False, True):
            with self.subTest(monthly=monthly):
                ledger = Ledger(self.path.with_name(f"parallel-{monthly}.sqlite3"),
                                review_limit_usd=1 if monthly else 0.0008,
                                monthly_limit_usd=0.0008 if monthly else None,
                                input_padding_tokens=0)
                barrier = threading.Barrier(2)
                result = []

                def reserve(review_id):
                    barrier.wait(timeout=5)
                    try:
                        result.append(ledger.reserve("audit", "gpt-6-luna",
                                                     self.payload(1000), review_id))
                    except BudgetExceeded:
                        result.append("blocked")

                review_ids = ("pr-2", "pr-3" if monthly else "pr-2")
                threads = [threading.Thread(target=reserve, args=(review_id,))
                           for review_id in review_ids]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(result.count("blocked"), 1)
                self.assertEqual(len(result), 2)
                self.assertEqual(ledger.summary()["unknown_request_count"], 1)

    def test_settlement_deduplicates_response_and_known_refund_releases_reservation(self):
        ledger = Ledger(self.path, review_limit_usd=0.001, input_padding_tokens=0)
        first = ledger.reserve("audit", "gpt-6-luna", self.payload(100), "pr-3")
        second = ledger.reserve("audit", "gpt-6-luna", self.payload(100), "pr-3")
        response = {"id": "same-response", "model": "gpt-6-luna",
                    "usage": {"input_tokens": 100,
                              "input_tokens_details": {"cached_tokens": 0,
                                                       "cache_write_tokens": 0},
                              "output_tokens": 100}}
        self.assertTrue(ledger.settle(first, response))
        self.assertFalse(ledger.settle(second, response))
        self.assertFalse(ledger.settle(first, response))
        self.assertEqual(ledger.summary(review_id="pr-3")["unknown_request_count"], 0)

        third = ledger.reserve("audit", "gpt-6-luna", self.payload(), "pr-3")
        self.assertTrue(ledger.fail(third, charged_unknown=False))
        self.assertEqual(ledger.summary(review_id="pr-3")["reserved_total_usd"], 0)

    def test_restart_and_uncertain_failure_keep_the_originating_month(self):
        ledger = Ledger(self.path, review_limit_usd=1)
        token = ledger.reserve("verify", "gpt-6.1-sol", self.payload(), "pr-4")
        month = ledger.summary(review_id="pr-4")["month"]
        ledger.fail(token)

        restarted = Ledger(self.path, review_limit_usd=1)
        summary = restarted.summary(month=month, review_id="pr-4")
        self.assertEqual(summary["unknown_request_count"], 1)
        self.assertGreater(summary["reserved_total_usd"], 0)
        with self.assertRaises(ValueError):
            Ledger(self.path, review_limit_usd=1, monthly_limit_usd=0)
        with self.assertRaises(BudgetExceeded):
            Ledger(self.path, review_limit_usd=1, monthly_limit_usd=0.000001).reserve(
                "verify", "gpt-6.1-sol", self.payload(), "pr-4")

    def test_limits_are_finite_and_positive(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Ledger(self.path, review_limit_usd=value)

    def test_review_summary_and_cap_span_months(self):
        ledger = Ledger(self.path, review_limit_usd=0.001, input_padding_tokens=0)
        with patch("forgejo_review_bot.spend._month_now",
                   side_effect=["2026-09", "2026-10"]):
            token = ledger.reserve("audit", "gpt-6-luna", self.payload(1000), "pr-5")
            self.assertTrue(ledger.settle(token, {
                "id": "resp-month", "model": "gpt-6-luna",
                "usage": {"input_tokens": 1000,
                          "input_tokens_details": {"cached_tokens": 0,
                                                   "cache_write_tokens": 0},
                          "output_tokens": 1000}}))
            with self.assertRaises(BudgetExceeded):
                ledger.reserve("audit", "gpt-6-luna", self.payload(1000), "pr-5")
        summary = ledger.summary(review_id="pr-5")
        self.assertIsNone(summary["month"])
        self.assertAlmostEqual(summary["estimated_total_usd"], 0.0006)
        self.assertTrue(summary["usage_complete"])

    def test_protected_verifier_floor_preserves_start_and_final(self):
        sizing = Ledger(self.path.with_name("sizing.sqlite3"),
                        review_limit_usd=1, input_padding_tokens=0)
        verifier = {"model": "gpt-6.1-sol", "max_output_tokens": 25_000,
                    "input": "Candidate findings: []", "tools": [{"type": "function"}],
                    "tool_choice": "required"}
        discovery = {"model": "gpt-6-luna", "max_output_tokens": 1_000,
                     "input": "review"}
        initial = sizing.reserve_estimate_micros(verifier["model"], verifier)
        continuation = sizing.reserve_estimate_micros(
            verifier["model"], {**verifier, "tool_choice": "none"},
            extra_input_tokens=FINAL_TOOL_OUTPUT_HEADROOM_TOKENS)
        floor = initial + continuation
        discovery_cost = sizing.reserve_estimate_micros(discovery["model"], discovery)
        limit = (floor + discovery_cost) / MICRODOLLARS

        ledger = Ledger(self.path.with_name("protected.sqlite3"),
                        review_limit_usd=limit, input_padding_tokens=0)
        budget = RequestBudget(ledger, "pr-6")
        protection = budget.protect_verifier(verifier)

        self.assertEqual(protection["protected_micros"], floor)
        self.assertEqual(protection["initial_usd"], initial / MICRODOLLARS)
        self.assertEqual(protection["continuation_usd"], continuation / MICRODOLLARS)
        budget.reserve("independent", discovery)
        token = budget.reserve("verifier", verifier)
        self.assertTrue(token)
        self.assertEqual(ledger.summary(review_id="pr-6")["unknown_request_count"], 2)

    def test_verifier_protection_uses_configured_model_rates(self):
        ledger = Ledger(self.path, review_limit_usd=1, input_padding_tokens=0)
        budget = RequestBudget(ledger, "pr-7")
        payload = {"model": "gpt-6-luna", "max_output_tokens": 8_000,
                   "input": "Candidate findings: []"}
        luna = budget.protect_verifier(payload)["protected_micros"]
        sol = budget.protect_verifier({**payload, "model": "gpt-6.1-sol"})[
            "protected_micros"]
        self.assertGreater(sol, luna)


if __name__ == "__main__":
    unittest.main()
