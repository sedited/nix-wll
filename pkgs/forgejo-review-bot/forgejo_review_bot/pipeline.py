"""Run selected independent reviews, verify their evidence, then edit findings."""

import json

from . import model, protocol, routing
from .config import AUDIT_NAMES, ADVERSARIAL_PROFILES
from .repository import audit_developer_notes, focused_review_input
from .spend import BudgetExceeded

# A sequential pipeline spends the review budget in a predictable order.
# Domain correctness precedes design; verification retains protected headroom.
DISCOVERY_ORDER = ("adversarial", "concurrency", "state", "public_contract",
                   "build", "tests", "design")
FULL_CONTEXT_STAGES = {"independent", "adversarial", "verifier"}
DISCOVERY_TOOL_LIMITS = {"routine": 12, "standard": 24, "sensitive": 48}


def stage_settings(name, tier):
    if name == "design":
        return "xhigh", 25_000
    if name == "adversarial" or name == "verifier" and tier == "sensitive":
        return "high", 25_000
    if name == "concurrency":
        return "medium", 8_000
    return "low", 8_000 if name == "verifier" else 4_000


def review_with_independent_passes(api_key, review, snapshot, bot_config,
                                  prompt_config, current_pr, debug,
                                  on_response=None, budget=None, is_current=None,
                                  routing_mode="enabled", allow_discussions=True):
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
        debug["pipeline_stage"] = name
        record = {"model": prompt_config.models[name], "status": "running",
                  "turns": [], "tools": []}
        stages[name] = record
        if name == "adversarial":
            record["profiles"] = list(plan["profiles"])
        try:
            if tools:
                calls = (48 if name in {"adversarial", "verifier"}
                         else DISCOVERY_TOOL_LIMITS[plan["tier"]])
                effort, output_tokens = stage_settings(name, plan["tier"])
                answer = model.openai_review(
                    api_key, input_text, snapshot, bot_config, prompt_config,
                    record, current_pr=current_pr, prompt=prompt,
                    model=prompt_config.models[name],
                    tools=model.TOOLS if name in FULL_CONTEXT_STAGES else model.FOCUSED_TOOLS,
                    max_tool_calls=calls,
                    max_output_tokens=output_tokens,
                    reasoning_effort=effort,
                    first_tool_required=name in FULL_CONTEXT_STAGES,
                    stage_name=name, on_response=on_response, budget=budget,
                    response_schema=schema, allow_discussions=allow_discussions,
                    is_current=is_current)
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
                  else prompt_config.audit_prompts[name])
        if name == "adversarial":
            prompt += "\n\n" + "\n\n".join(
                prompt_config.audit_prompts[profile] for profile in plan["profiles"])
        prompt = prompt_config.audit_prompts["common"] + "\n\n" + prompt
        input_text = review if name in FULL_CONTEXT_STAGES else focused_review_input(review, snapshot, name)
        if name == "design":
            if notes is None:
                notes = audit_developer_notes(snapshot)
            input_text += "\n\nMerge-base developer notes:\n" + notes
        try:
            protect_verification()
            answer = run_stage(name, input_text, prompt, protocol.DISCOVERY_SCHEMA)
            result = protocol.discovery(answer, name, snapshot)
            stages[name]["coverage"] = result["coverage"]
            candidates.extend(result["findings"])
            candidate_sources.update({finding["id"]: name for finding in result["findings"]})
            if result["coverage"]["status"] == "partial":
                limitations.append(f"The {name} review had incomplete evidence.")
            return result["requires_sensitive_review"]
        except model.StaleReview:
            raise
        except Exception as exc:
            record = stages[name]
            if record["status"] == "completed":
                record.update(status="invalid", error_type=type(exc).__name__)
            if isinstance(exc, protocol.InvalidReview):
                record["validation_error"] = str(exc)
            limitations.append(f"The {name} review did not complete.")
            return False

    sensitive = discover("independent")
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
        escalate = discover(name)
        completed.add(name)
        if escalate and plan["tier"] != "sensitive":
            selected.update(("adversarial", "state", "concurrency"))
            plan = {**plan, "tier": "sensitive",
                    "profiles": plan["profiles"] or list(ADVERSARIAL_PROFILES),
                    "audits": [audit for audit in AUDIT_NAMES if audit in selected],
                    "evidence": plan["evidence"] + [f"{name} requested sensitive review"]}
            debug["routing"]["selected"] = plan
            debug["routing"]["escalated_by"] = name
    for name in DISCOVERY_ORDER:
        if name not in completed:
            stages[name] = {"model": prompt_config.models[name], "status": "skipped",
                            "turns": [], "tools": []}

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
         "verified_by": "verifier", "edited_by": "collator" if was_edited else None}
        for finding in findings
    ]
    if budget is not None:
        debug["budget"] = budget.summary()
    debug.pop("pipeline_stage", None)
    return protocol.render(findings, limitations)
