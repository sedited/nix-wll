"""Focused audits and review stage orchestration."""
import hashlib
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from .model import (FOCUSED_TOOLS, MAX_AUDIT_OUTPUT_BYTES, MAX_FOCUSED_TOOL_CALLS,
                    MAX_VERIFIER_OUTPUT_TOKENS, openai_review, run_audit)
from .repository import audit_developer_notes, focused_review_input

AUDIT_NAMES = ("state", "public_contract", "tests", "developer_notes", "design")
TOOLED_AUDITS = ("tests", "design")

def run_focused_review(api_key, name, prompt, review, snapshot, bot_config, prompt_config, on_response=None):
    stage_debug = {}
    answer = openai_review(api_key, review, snapshot, bot_config, prompt_config,
                           stage_debug, prompt=prompt, model=prompt_config.models[name],
                           tools=FOCUSED_TOOLS, max_tool_calls=MAX_FOCUSED_TOOL_CALLS,
                           first_tool_required=False, stage_name=name,
                           on_response=on_response)
    turns = stage_debug["turns"]
    record = {"name": name, "model": prompt_config.models[name], "status": "completed",
              "input_tokens": sum(turn.get("input_tokens") or 0 for turn in turns),
              "cached_tokens": sum(turn.get("cached_tokens") or 0 for turn in turns),
              "cache_write_tokens": sum(turn.get("cache_write_tokens") or 0 for turn in turns),
              "output_tokens": sum(turn.get("output_tokens") or 0 for turn in turns),
              "elapsed_seconds": sum(turn["elapsed_seconds"] for turn in turns),
              "tool_calls": len(stage_debug["tools"]),
              "response_output_sha256": hashlib.sha256(answer.encode()).hexdigest(),
              "output_truncated": len(answer.encode()) > MAX_AUDIT_OUTPUT_BYTES}
    if record["output_truncated"]:
        answer = answer.encode()[:MAX_AUDIT_OUTPUT_BYTES].decode(errors="replace")
        answer += "\n[Audit output truncated]"
    return answer, record

def run_audits(api_key, review, snapshot, bot_config, prompt_config, debug, on_response=None):
    prompts = prompt_config.audit_prompts
    models = prompt_config.models
    notes = audit_developer_notes(snapshot)
    with ThreadPoolExecutor(max_workers=len(AUDIT_NAMES)) as pool:
        futures = {}
        for name in AUDIT_NAMES:
            prompt = prompts["common"] + "\n\n" + prompts[name]
            input_text = focused_review_input(review, snapshot, name)
            if name in TOOLED_AUDITS:
                futures[name] = pool.submit(run_focused_review, api_key, name,
                                            prompt, input_text, snapshot, bot_config,
                                            prompt_config, on_response)
            else:
                futures[name] = pool.submit(run_audit, api_key, name,
                                            prompt, input_text, prompt_config, notes,
                                            on_response)
        results = []
        records = []
        stage_outputs = {}
        for name, future in futures.items():
            try:
                answer, record = future.result()
            except Exception as exc:
                answer = "Audit unavailable."
                record = {"name": name, "model": models[name], "status": "failed",
                          "error_type": type(exc).__name__}
                if isinstance(exc, urllib.error.HTTPError):
                    record["http_status"] = exc.code
            records.append(record)
            results.append(f"{name}:\n{answer}")
            stage_outputs[name] = answer
    debug["audits"] = records
    debug["stage_outputs"] = stage_outputs
    return "Focused reviews:\n" + "\n\n".join(results)

def review_with_independent_passes(api_key, review, snapshot, bot_config, prompt_config, current_pr, debug, on_response=None):
    debug["pipeline_stage"] = "independent"
    sol_debug = {}
    adversarial_debug = {}
    debug["adversarial"] = adversarial_debug
    with ThreadPoolExecutor(max_workers=3) as pool:
        audits_future = pool.submit(run_audits, api_key, review, snapshot, bot_config,
                                    prompt_config, debug, on_response)
        sol_future = pool.submit(openai_review, api_key, review, snapshot,
                                 bot_config, prompt_config, sol_debug, current_pr,
                                 on_response=on_response)
        adversarial_future = pool.submit(
            openai_review, api_key, review, snapshot, bot_config, prompt_config,
            adversarial_debug, current_pr,
            prompt=prompt_config.audit_prompts["adversarial"],
            model=prompt_config.models["adversarial"], stage_name="adversarial",
            on_response=on_response)
        sol_review = sol_future.result()
        adversarial_review = adversarial_future.result()
        luna_reviews = audits_future.result()
    debug.update(sol_debug)
    stage_outputs = debug.setdefault("stage_outputs", {})
    stage_outputs["independent"] = sol_review
    stage_outputs["adversarial"] = adversarial_review
    debug["independent_review_sha256"] = hashlib.sha256(sol_review.encode()).hexdigest()
    debug["adversarial_review_sha256"] = hashlib.sha256(
        adversarial_review.encode()).hexdigest()
    reviews = (f"Independent review:\n{sol_review}\n\n"
               f"Adversarial review:\n{adversarial_review}\n\n{luna_reviews}")

    debug["pipeline_stage"] = "verification"
    verification_debug = {}
    debug["verification"] = verification_debug
    verified = openai_review(
        api_key, review + "\n\nIndependent candidate reviews:\n" + reviews,
        snapshot, bot_config, prompt_config, verification_debug, current_pr,
        prompt=prompt_config.audit_prompts["verifier"],
        model=prompt_config.models["verifier"],
        max_output_tokens=MAX_VERIFIER_OUTPUT_TOKENS, stage_name="verifier",
        on_response=on_response)
    stage_outputs["verifier"] = verified
    debug["verification_output_sha256"] = hashlib.sha256(verified.encode()).hexdigest()

    debug["pipeline_stage"] = "collation"
    content, record = run_audit(api_key, "collator",
                                prompt_config.audit_prompts["collator"],
                                reviews + "\n\nVerification decisions:\n" + verified,
                                prompt_config, on_response=on_response)
    stage_outputs["collator"] = content
    debug["collator"] = record
    if record["status"] != "completed" or record["output_truncated"]:
        raise ValueError("collator did not return a complete comment")
    debug.pop("pipeline_stage")
    return content
