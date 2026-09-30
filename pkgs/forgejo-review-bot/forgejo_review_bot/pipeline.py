"""Run selected independent reviews, verify their evidence, then edit findings."""

import json

from . import model, protocol, routing
from .config import AUDIT_NAMES
from .repository import audit_developer_notes, focused_review_input
from .spend import BudgetExceeded

# A sequential pipeline spends the review budget in a predictable order.
# Cheap verification and formatting retain headroom in RequestBudget.
DISCOVERY_ORDER = ("adversarial", "design", "tests", "public_contract",
                   "developer_notes", "state")
FULL_CONTEXT_STAGES = {"independent", "adversarial", "verifier"}
DISCOVERY_TOOL_LIMITS = {"routine": 12, "standard": 24, "sensitive": 48}


def review_with_independent_passes(api_key, review, snapshot, bot_config,
                                  prompt_config, current_pr, debug,
                                  on_response=None, budget=None, is_current=None,
                                  routing_mode="enabled", allow_discussions=True):
    stages = debug.setdefault("stages", {})
    outputs = debug.setdefault("stage_outputs", {})
    limitations = []
    candidates = []
    debug["pipeline_stage"] = "routing"
    plan = routing.plan_review(api_key, review, snapshot, prompt_config, debug,
                               routing_mode, budget, is_current)
    notes = None

    def run_stage(name, input_text, prompt, schema, *, tools=True):
        debug["pipeline_stage"] = name
        record = {"model": prompt_config.models[name], "status": "running",
                  "turns": [], "tools": []}
        stages[name] = record
        try:
            if tools:
                calls = (48 if name in {"adversarial", "verifier"}
                         else DISCOVERY_TOOL_LIMITS[plan["tier"]])
                output_tokens = {"design": 25_000, "verifier": 8_000}.get(name, 4_000)
                answer = model.openai_review(
                    api_key, input_text, snapshot, bot_config, prompt_config,
                    record, current_pr=current_pr, prompt=prompt,
                    model=prompt_config.models[name],
                    tools=model.TOOLS if name in FULL_CONTEXT_STAGES else model.FOCUSED_TOOLS,
                    max_tool_calls=calls,
                    max_output_tokens=output_tokens,
                    reasoning_effort={"design": "xhigh", "adversarial": "high"}.get(name, "low"),
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
        prompt = prompt_config.audit_prompts["common"] + "\n\n" + prompt
        input_text = review if name in FULL_CONTEXT_STAGES else focused_review_input(review, snapshot, name)
        if name == "developer_notes":
            if notes is None:
                notes = audit_developer_notes(snapshot)
            input_text += "\n\nMerge-base developer notes:\n" + notes
        try:
            answer = run_stage(name, input_text, prompt, protocol.DISCOVERY_SCHEMA)
            result = protocol.discovery(answer, name, snapshot)
            stages[name]["coverage"] = result["coverage"]
            candidates.extend(result["findings"])
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
                "evidence": plan["evidence"] + ["Overview requested sensitive review"]}
        debug["routing"]["selected"] = plan
    if plan["tier"] == "sensitive":
        selected.add("adversarial")
    for name in DISCOVERY_ORDER:
        if name in selected:
            escalate = discover(name)
            if (escalate and name != "adversarial"
                    and stages.get("adversarial", {}).get("status") in {None, "skipped"}):
                discover("adversarial")
                selected.add("state")
                plan = {**plan, "tier": "sensitive",
                        "audits": [audit for audit in AUDIT_NAMES if audit in selected],
                        "evidence": plan["evidence"] + [f"{name} requested sensitive review"]}
                debug["routing"]["selected"] = plan
                debug["routing"]["escalated_by"] = name
        else:
            stages[name] = {"model": prompt_config.models[name], "status": "skipped",
                            "turns": [], "tools": []}

    if is_current is not None and not is_current():
        raise model.StaleReview()
    accepted = []
    try:
        verified = run_stage(
            "verifier", review + "\n\nCandidate findings:\n" + json.dumps(candidates),
            prompt_config.audit_prompts["verifier"], protocol.VERIFIER_SCHEMA)
        result, accepted = protocol.verification(verified, candidates, snapshot)
        stages["verifier"]["coverage"] = result["coverage"]
        debug["decisions"] = result["decisions"]
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
    if budget is not None:
        debug["budget"] = budget.summary()
    debug.pop("pipeline_stage", None)
    return protocol.render(findings, limitations)
