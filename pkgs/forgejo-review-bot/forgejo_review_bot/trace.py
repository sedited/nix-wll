"""Review usage, public debug rendering, and private trace storage."""
import html
import json
import os
import time

from .model import MAX_OUTPUT_TOKENS, MAX_VERIFIER_OUTPUT_TOKENS, MAX_COLLATOR_OUTPUT_TOKENS

MAX_PUBLIC_STAGE_OUTPUT_BYTES = 4_000
MODEL_RATES = {"gpt-6-luna": (0.1, 0.01, 0.125, 0.5),
               "gpt-6.1-sol": (2, 0.1, 2.5, 10)}

def review_metrics(debug, prompt_config):
    models = prompt_config.models
    independent_turns = debug.get("turns", [])
    adversarial_turns = debug.get("adversarial", {}).get("turns", [])
    verifier_turns = debug.get("verification", {}).get("turns", [])
    turns = independent_turns + adversarial_turns + verifier_turns
    tools = (debug.get("tools", []) + debug.get("adversarial", {}).get("tools", [])
             + debug.get("verification", {}).get("tools", []))
    audits = debug.get("audits", [])
    calls = [(turn, MODEL_RATES.get(models["independent"]))
             for turn in independent_turns]
    calls += [(turn, MODEL_RATES.get(models["adversarial"]))
              for turn in adversarial_turns]
    calls += [(turn, MODEL_RATES.get(models["verifier"]))
              for turn in verifier_turns]
    calls += [(audit, MODEL_RATES.get(audit.get("model", models[audit["name"]])))
              for audit in audits
              if isinstance(audit.get("input_tokens"), int)
              and isinstance(audit.get("output_tokens"), int)]
    collator = debug.get("collator")
    if collator and isinstance(collator.get("input_tokens"), int) \
            and isinstance(collator.get("output_tokens"), int):
        calls.append((collator, MODEL_RATES.get(
            collator.get("model", models["collator"]))))
    known_usage = all(isinstance(call.get("input_tokens"), int)
                      and isinstance(call.get("output_tokens"), int)
                      for call, _rates in calls)
    metrics = {"model_turns": len(turns),
               "tool_calls": len(tools) + sum(audit.get("tool_calls", 0)
                                              for audit in audits),
               "audit_calls": len(audits),
               "estimated_cost_usd": None, "total_input_tokens": None,
               "total_output_tokens": None, "total_model_seconds": None}
    if known_usage and calls:
        metrics.update({
            "total_input_tokens": sum(call["input_tokens"] for call, _ in calls),
            "total_output_tokens": sum(call["output_tokens"] for call, _ in calls),
            "total_model_seconds": round(sum(call["elapsed_seconds"] for call, _ in calls), 2),
        })
        if all(rates is not None for _call, rates in calls):
            cost = 0.0
            for call, rates in calls:
                input_tokens = call["input_tokens"]
                cached = call["cached_tokens"] or 0
                written = call["cache_write_tokens"] or 0
                ordinary = max(0, input_tokens - cached - written)
                multiplier = 2 if input_tokens > 272_000 else 1
                output_multiplier = 1.5 if multiplier == 2 else 1
                cost += (ordinary * rates[0] + cached * rates[1]
                         + written * rates[2]) * multiplier / 1_000_000
                cost += call["output_tokens"] * rates[3] * output_multiplier / 1_000_000
            metrics["estimated_cost_usd"] = round(cost, 6)
    return metrics

def review_trace(debug, prompt_config):
    prompt = prompt_config.instructions
    metrics = review_metrics(debug, prompt_config)
    trace = {"models": prompt_config.models, "endpoint": "/v1/responses", "store": False,
             "max_output_tokens": MAX_OUTPUT_TOKENS,
             "verifier_max_output_tokens": MAX_VERIFIER_OUTPUT_TOKENS,
             "collator_max_output_tokens": MAX_COLLATOR_OUTPUT_TOKENS,
             "instructions": debug.get("instructions", prompt),
             "input": "PR text, patch, and commits omitted from public debug output",
             "turns": debug.get("turns", []), "tools": debug.get("tools", []),
             "adversarial": debug.get("adversarial", {}),
             "audits": debug.get("audits", []),
             "verification": debug.get("verification", {}),
             "collator": debug.get("collator")}
    if debug.get("stage_outputs"):
        trace["stage_outputs"] = {
            name: {"text": output.encode()[:MAX_PUBLIC_STAGE_OUTPUT_BYTES]
                   .decode(errors="replace"),
                   "truncated": len(output.encode()) > MAX_PUBLIC_STAGE_OUTPUT_BYTES}
            for name, output in debug["stage_outputs"].items()
        }
        trace["stage_outputs_note"] = (
            "Preliminary agent responses are unverified; only the review above "
            "is intended as a public finding. Full responses are retained in "
            "the bot's private state when trace storage succeeds.")
    if "review_input_bytes" in debug:
        trace["review_input_bytes"] = debug["review_input_bytes"]
        trace["review_input_sha256"] = debug["review_input_sha256"]
    if debug.get("skip"):
        trace["skip"] = debug["skip"]
    if debug.get("pipeline_stage"):
        trace["pipeline_stage"] = debug["pipeline_stage"]
    for key in ("independent_review_sha256", "adversarial_review_sha256",
                "verification_output_sha256"):
        if key in debug:
            trace[key] = debug[key]
    if metrics["estimated_cost_usd"] is not None:
        trace.update(metrics)
        trace["pricing_note"] = ("Estimated from token usage at configured model rates. "
                                 "Only calls with reported usage are counted; "
                                 "missing cache-write counts are treated as zero.")
    else:
        trace["estimated_cost_usd"] = None
    return trace

def debug_section(debug, prompt_config):
    trace = review_trace(debug, prompt_config)
    rendered = html.escape(json.dumps(trace, indent=2, ensure_ascii=True))
    return f"\n<details><summary>Review debug</summary>\n\n<pre>{rendered}</pre>\n</details>\n"

def save_review_trace(state_dir, number, head_sha, content, debug, prompt_config):
    trace_dir = state_dir / "review-traces"
    trace_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = trace_dir / f"{number}-{head_sha}-{time.time_ns()}.json"
    record = {"pr": number, "head": head_sha, "review": content,
              "stage_outputs": debug.get("stage_outputs", {}),
              "trace": review_trace(debug, prompt_config)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(record, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return path
