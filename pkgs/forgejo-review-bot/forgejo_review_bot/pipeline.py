"""Run selected independent reviews, verify their evidence, then edit findings."""

import json
from concurrent.futures import ThreadPoolExecutor

from . import model, protocol, routing
from .config import AUDIT_NAMES, ADVERSARIAL_PROFILES
from .repository import audit_developer_notes, focused_review_input
from .spend import BudgetExceeded

# Domain stages spend the review budget in a predictable order; the two
# adversarial models review the same evidence concurrently.
# Domain correctness precedes design; verification retains protected headroom.
DISCOVERY_ORDER = ("adversarial", "concurrency", "state", "public_contract",
                   "build", "tests", "design")
ADVERSARIAL_STAGES = ("adversarial", "adversarial_glm")
FULL_CONTEXT_STAGES = {"independent", *ADVERSARIAL_STAGES, "verifier"}
DISCOVERY_TOOL_LIMITS = {"routine": 12, "standard": 24, "sensitive": 48}


def stage_settings(name, tier):
    if name == "design":
        return "xhigh", 25_000
    if name in ADVERSARIAL_STAGES or name == "verifier" and tier == "sensitive":
        return "high", 25_000
    if name == "concurrency":
        return "medium", 8_000
    return "low", 8_000 if name == "verifier" else 4_000


def review_with_independent_passes(api_key, review, snapshot, bot_config,
                                  prompt_config, current_pr, debug,
                                  on_response=None, budget=None, is_current=None,
                                  routing_mode="enabled", allow_discussions=True,
                                  ppq_api_key=None, ppq_budget=None):
    stages = debug.setdefault("stages", {})
    outputs = debug.setdefault("stage_outputs", {})
    limitations = []
    candidates = []
    candidate_sources = debug.setdefault("candidate_sources", {})
    plan = {"tier": "sensitive"}

    def verifier_input():
        return review + "\n\nCandidate findings:\n" + json.dumps(candidates)

    def protect_verification():
        if budget is not None:
            effort, output_tokens = stage_settings("verifier", plan["tier"])
            debug["verification_budget"] = budget.protect_verifier({
                "model": prompt_config.models["verifier"], "store": False,
                "reasoning": {"effort": effort},
                "instructions": prompt_config.audit_prompts["verifier"],
                "input": [{"role": "user", "content": verifier_input()}],
                "tools": model.TOOLS, "tool_choice": "required",
                "max_output_tokens": output_tokens,
                "text": {"format": {"type": "json_schema", "name": "verifier",
                                    "strict": True, "schema": protocol.VERIFIER_SCHEMA}},
            })

    protect_verification()
    debug["pipeline_stage"] = "routing"
    plan = routing.plan_review(api_key, review, snapshot, prompt_config, debug,
                               routing_mode, budget, is_current)
    notes = None

    def run_stage(name, input_text, prompt, schema, *, tools=True):
        if name not in ADVERSARIAL_STAGES:
            debug["pipeline_stage"] = name
        record = {"model": prompt_config.models[name], "status": "running",
                  "turns": [], "tools": []}
        stages[name] = record
        if name in ADVERSARIAL_STAGES:
            record["profiles"] = list(plan["profiles"])
        try:
            if tools:
                calls = (48 if name in {*ADVERSARIAL_STAGES, "verifier"}
                         else DISCOVERY_TOOL_LIMITS[plan["tier"]])
                effort, output_tokens = stage_settings(name, plan["tier"])
                answer = model.openai_review(
                    ppq_api_key if name == "adversarial_glm" else api_key,
                    input_text, snapshot, bot_config, prompt_config,
                    record, current_pr=current_pr, prompt=prompt,
                    model=prompt_config.models[name],
                    tools=model.TOOLS if name in FULL_CONTEXT_STAGES else model.FOCUSED_TOOLS,
                    max_tool_calls=calls,
                    max_output_tokens=output_tokens,
                    reasoning_effort=effort,
                    first_tool_required=name in FULL_CONTEXT_STAGES,
                    stage_name=name, on_response=on_response,
                    budget=ppq_budget if name == "adversarial_glm" else budget,
                    response_schema=schema, allow_discussions=allow_discussions,
                    is_current=is_current,
                    **({"api_base": "https://api.ppq.ai/v1"}
                       if name == "adversarial_glm" else {}))
            else:
                answer, response = model.run_audit(
                    api_key, name, prompt, input_text, prompt_config,
                    on_response=on_response, budget=budget,
                    response_schema=schema, is_current=is_current, debug=record)
                if response["status"] != "completed":
                    raise protocol.InvalidReview("Stage response was incomplete")
            outputs[name] = answer
            record["raw_output"] = answer
            record["status"] = "completed"
            return answer
        except model.StaleReview:
            record["status"] = "stale"
            raise
        except Exception as exc:
            record["status"] = "budget_exhausted" if isinstance(exc, BudgetExceeded) else "failed"
            record["error_type"] = type(exc).__name__
            if record.get("raw_output"):
                outputs[name] = record["raw_output"]
            raise

    def discover(name):
        nonlocal notes
        prompt = (prompt_config.instructions if name == "independent"
                  else prompt_config.audit_prompts[
                      "adversarial" if name == "adversarial_glm" else name])
        if name in ADVERSARIAL_STAGES:
            prompt += "\n\n" + "\n\n".join(
                prompt_config.audit_prompts[profile] for profile in plan["profiles"])
        prompt = prompt_config.audit_prompts["common"] + "\n\n" + prompt
        input_text = review if name in FULL_CONTEXT_STAGES else focused_review_input(review, snapshot, name)
        if name == "design":
            if notes is None:
                notes = audit_developer_notes(snapshot)
            input_text += "\n\nMerge-base developer notes:\n" + notes
        try:
            answer = run_stage(name, input_text, prompt, protocol.DISCOVERY_SCHEMA)
            result = protocol.discovery(answer, name, snapshot)
            stages[name]["coverage"] = result["coverage"]
            return result
        except model.StaleReview:
            raise
        except Exception as exc:
            record = stages[name]
            if record["status"] == "completed":
                record.update(status="invalid", error_type=type(exc).__name__)
            if isinstance(exc, protocol.InvalidReview):
                record["validation_error"] = str(exc)
            return None

    def collect(name, result):
        if result is None:
            limitations.append(f"The {name} review did not complete.")
            return False
        candidates.extend(result["findings"])
        candidate_sources.update({finding["id"]: name for finding in result["findings"]})
        if result["coverage"]["status"] == "partial":
            limitations.append(f"The {name} review had incomplete evidence.")
        return result["requires_sensitive_review"]

    protect_verification()
    sensitive = collect("independent", discover("independent"))
    selected = set(plan["audits"])
    if sensitive and plan["tier"] != "sensitive":
        selected.update(AUDIT_NAMES)
        plan = {**plan, "tier": "sensitive", "audits": list(AUDIT_NAMES),
                "profiles": list(ADVERSARIAL_PROFILES),
                "evidence": plan["evidence"] + ["Overview requested sensitive review"]}
        debug["routing"]["selected"] = plan
    if plan["tier"] == "sensitive":
        selected.add("adversarial")
    completed = set()
    while pending := [name for name in DISCOVERY_ORDER if name in selected - completed]:
        name = pending[0]
        protect_verification()
        if name == "adversarial" and ppq_api_key:
            debug["pipeline_stage"] = "adversarial"
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {stage: executor.submit(discover, stage)
                           for stage in ADVERSARIAL_STAGES}
                results = {stage: future.result() for stage, future in futures.items()}
            escalate = any([collect(stage, results[stage])
                            for stage in ADVERSARIAL_STAGES])
            completed.update(ADVERSARIAL_STAGES)
        else:
            escalate = collect(name, discover(name))
            completed.add(name)
        if escalate and plan["tier"] != "sensitive":
            selected.update(("adversarial", "state", "concurrency"))
            plan = {**plan, "tier": "sensitive",
                    "profiles": plan["profiles"] or list(ADVERSARIAL_PROFILES),
                    "audits": [audit for audit in AUDIT_NAMES if audit in selected],
                    "evidence": plan["evidence"] + [f"{name} requested sensitive review"]}
            debug["routing"]["selected"] = plan
            debug["routing"]["escalated_by"] = name
    for name in (*DISCOVERY_ORDER, "adversarial_glm"):
        if name not in completed:
            stages[name] = {"model": prompt_config.models[name], "status": "skipped",
                            "turns": [], "tools": []}
            if name == "adversarial_glm" and not ppq_api_key:
                stages[name]["reason"] = "PPQ API key is not configured"

    if is_current is not None and not is_current():
        raise model.StaleReview()
    accepted = []
    finding_candidates = {}
    try:
        verified = run_stage(
            "verifier", verifier_input(),
            prompt_config.audit_prompts["verifier"], protocol.VERIFIER_SCHEMA)
        result, accepted = protocol.verification(verified, candidates, snapshot)
        stages["verifier"]["coverage"] = result["coverage"]
        debug["decisions"] = result["decisions"]
        published = [decision for decision in result["decisions"]
                     if decision["disposition"] == "publish"]
        finding_candidates = {finding["id"]: decision["candidate_ids"]
                              for finding, decision in zip(accepted, published)}
        if result["validation_errors"]:
            stages["verifier"]["validation_errors"] = result["validation_errors"]
            limitations.append("Some findings failed validation and were withheld.")
        if result["coverage"]["status"] == "partial":
            limitations.append("Verification was partial.")
        if any(item["disposition"] == "unresolved" for item in result["decisions"]):
            limitations.append("Some candidate findings remain unresolved.")
    except model.StaleReview:
        raise
    except Exception as exc:
        if stages["verifier"]["status"] == "completed":
            stages["verifier"].update(status="invalid", error_type=type(exc).__name__)
        if isinstance(exc, protocol.InvalidReview):
            stages["verifier"]["validation_error"] = str(exc)
            limitations.append("Verifier output failed validation; no findings were published.")
        else:
            limitations.append("Verification did not complete; no unverified findings were published.")

    # The editor sees only accepted findings. A failed editor can use the
    # verifier's own wording, without discarding paid verification work.
    findings = accepted
    if accepted:
        try:
            edited = run_stage("collator", json.dumps({"findings": accepted}),
                               prompt_config.audit_prompts["collator"],
                               protocol.COLLATOR_SCHEMA, tools=False)
            findings = protocol.collation(edited, accepted)
        except model.StaleReview:
            raise
        except Exception as exc:
            if stages["collator"]["status"] == "completed":
                stages["collator"].update(status="invalid", error_type=type(exc).__name__)
            if isinstance(exc, protocol.InvalidReview):
                stages["collator"]["validation_error"] = str(exc)
            stages["collator"]["used_verified_wording"] = True
    else:
        stages["collator"] = {"model": prompt_config.models["collator"],
                              "status": "skipped", "turns": [], "tools": [],
                              "reason": "No accepted findings to edit"}

    debug["coverage"] = {"status": "partial" if limitations else "complete",
                         "limitations": limitations}
    was_edited = (stages["collator"]["status"] == "completed"
                  and not stages["collator"].get("used_verified_wording"))
    debug["finding_attribution"] = [
        {"finding_id": finding["id"],
         **{key: finding[key] for key in ("title", "path", "line", "side")},
         "candidate_ids": finding_candidates[finding["id"]],
         "raised_by": sorted({candidate_sources[identifier]
                              for identifier in finding_candidates[finding["id"]]}) or ["verifier"],
         "raised_by_models": sorted({prompt_config.models[candidate_sources[identifier]]
                                     for identifier in finding_candidates[finding["id"]]})
                             or [prompt_config.models["verifier"]],
         "verified_by": "verifier", "verified_by_model": prompt_config.models["verifier"],
         "edited_by": "collator" if was_edited else None,
         "edited_by_model": prompt_config.models["collator"] if was_edited else None}
        for finding in findings
    ]
    if budget is not None:
        debug["budget"] = budget.summary()
    if ppq_budget is not None:
        debug["ppq_budget"] = ppq_budget.summary()
    debug.pop("pipeline_stage", None)
    return protocol.render(findings, limitations)
