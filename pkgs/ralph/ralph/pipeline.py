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
ARCHAEOLOGY_TOOL_LIMIT = 12


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
                                  ppq_api_key=None, ppq_budget=None,
                                  research_evidence=None, blind_alternatives=True):
    if research_evidence is not None:
        allow_discussions = False
    stages = debug.setdefault("stages", {})
    outputs = debug.setdefault("stage_outputs", {})
    limitations = []
    candidates = []
    candidate_sources = debug.setdefault("candidate_sources", {})
    concept_candidate = None
    alternatives_candidate = None
    verified_concept = None
    concept_summary = None
    research_inventory = (model.research.inventory(research_evidence)
                          if research_evidence is not None else None)
    plan = {"tier": "sensitive"}

    def with_research_inventory(input_text):
        if research_inventory is None:
            return input_text
        return input_text + "\n\nFrozen research inventory:\n" + research_inventory

    def verifier_input():
        return with_research_inventory(review) + "\n\nVerification input:\n" + json.dumps({
            "candidate_findings": candidates,
            "concept_candidate": concept_candidate,
            **({"blind_alternatives": alternatives_candidate}
               if alternatives_candidate is not None else {}),
        })

    def protect_verification():
        if budget is not None:
            effort, output_tokens = stage_settings("verifier", plan["tier"])
            tools = model.available_tools(model.VERIFIER_TOOLS, allow_discussions,
                                          research_evidence)
            debug["verification_budget"] = budget.protect_verifier({
                "model": prompt_config.models["verifier"], "store": False,
                "reasoning": {"effort": effort},
                "instructions": prompt_config.audit_prompts["verifier"],
                "input": [{"role": "user", "content": verifier_input()}],
                "tools": tools, "tool_choice": "required",
                "max_tool_calls": model.MAX_WEB_SEARCH_CALLS_PER_RESPONSE,
                "max_output_tokens": output_tokens,
                "text": {"format": {"type": "json_schema", "name": "verifier",
                                    "strict": True, "schema": protocol.VERIFIER_SCHEMA}},
            })

    protect_verification()
    debug["pipeline_stage"] = "routing"
    plan = routing.plan_review(api_key, review, snapshot, prompt_config, debug,
                               routing_mode, budget, is_current)
    notes = None

    def stage_tools(name):
        tools = (model.BASE_TOOLS if name == "alternatives"
                 else model.ARCHAEOLOGY_TOOLS if name == "archaeologist"
                 else model.VERIFIER_TOOLS if name == "verifier"
                 else model.TOOLS if name in FULL_CONTEXT_STAGES
                 else model.FOCUSED_TOOLS)
        evidence = research_evidence if name in {"archaeologist", "verifier"} else None
        return model.available_tools(tools, allow_discussions, evidence)

    def run_stage(name, input_text, prompt, schema, *, tools=True):
        if name not in ADVERSARIAL_STAGES:
            debug["pipeline_stage"] = name
        stage_model = prompt_config.models["archaeologist" if name == "alternatives" else name]
        record = {"model": stage_model, "status": "running",
                  "turns": [], "tools": []}
        stages[name] = record
        if name in ADVERSARIAL_STAGES:
            record["profiles"] = list(plan["profiles"])
        try:
            if tools:
                calls = (ARCHAEOLOGY_TOOL_LIMIT if name in {"archaeologist", "alternatives"}
                         else 48 if name in {*ADVERSARIAL_STAGES, "verifier"}
                         else DISCOVERY_TOOL_LIMITS[plan["tier"]])
                effort, output_tokens = stage_settings(name, plan["tier"])
                available_tools = stage_tools(name)
                extra = ({"research_evidence": research_evidence}
                         if research_evidence is not None and name in {"archaeologist", "verifier"}
                         else {})
                answer = model.openai_review(
                    ppq_api_key if name == "adversarial_glm" else api_key,
                    input_text, snapshot, bot_config, prompt_config,
                    record, current_pr=current_pr, prompt=prompt,
                    model=stage_model,
                    tools=available_tools,
                    max_tool_calls=calls,
                    max_output_tokens=output_tokens,
                    reasoning_effort=effort,
                    first_tool_required=bool(available_tools)
                    and (name in FULL_CONTEXT_STAGES or name in {"archaeologist", "alternatives"}),
                    stage_name=name, on_response=on_response,
                    budget=ppq_budget if name == "adversarial_glm" else budget,
                    response_schema=schema, allow_discussions=allow_discussions,
                    is_current=is_current,
                    **({"api_base": "https://api.ppq.ai/v1"}
                       if name == "adversarial_glm" else {}),
                    **extra)
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

    def run_archaeologist():
        prompt = prompt_config.audit_prompts["archaeologist"]
        unavailable = (
            "\n\nDiscussion lookup is disabled for this review. Explain which "
            "history was unavailable and assess the concept from the PR rationale, "
            "patch context, and any non-discussion evidence you can inspect."
            if not allow_discussions and research_evidence is None else "")
        input_text = (
            f"Current PR: {bot_config.repository_url}/pulls/{current_pr}\n"
            f"Changed paths: {json.dumps(sorted(snapshot.changed_paths))}\n\n"
            f"{review}{unavailable}"
        )
        input_text = with_research_inventory(input_text)
        try:
            answer = run_stage("archaeologist", input_text, prompt,
                               protocol.ARCHAEOLOGY_SCHEMA)
            result = protocol.archaeology(answer)
            stages["archaeologist"]["coverage"] = result["coverage"]
            debug["concept_assessment"] = {
                "status": "candidate",
                "stage": "archaeologist",
                "candidate": result["assessment"],
                "coverage": result["coverage"],
            }
            if result["coverage"]["status"] == "partial":
                limitations.append("The archaeology review had incomplete evidence.")
            return result["assessment"]
        except model.StaleReview:
            raise
        except Exception as exc:
            record = stages["archaeologist"]
            if record["status"] == "completed":
                record.update(status="invalid", error_type=type(exc).__name__)
            if isinstance(exc, protocol.InvalidReview):
                record["validation_error"] = str(exc)
            debug["concept_assessment"] = {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
            limitations.append("The archaeology review did not complete.")
            return None

    concept_candidate = run_archaeologist()
    if blind_alternatives and concept_candidate is not None:
        protect_verification()
        # Only the goal and baseline cross this boundary, never the PR solution
        # or discussion. The tools expose only the pinned merge-base tree.
        alternative_input = json.dumps({key: concept_candidate[key]
                                        for key in ("problem", "goal", "baseline")})
        try:
            answer = run_stage("alternatives", alternative_input,
                               prompt_config.audit_prompts["alternatives"],
                               protocol.ALTERNATIVES_SCHEMA)
            result = protocol.blind_alternatives(answer)
            stages["alternatives"]["coverage"] = result["coverage"]
            alternatives_candidate = result["alternatives"]
            if result["coverage"]["status"] == "partial":
                limitations.append("The alternatives experiment had incomplete evidence.")
        except model.StaleReview:
            raise
        except Exception as exc:
            record = stages["alternatives"]
            if record["status"] == "completed":
                record.update(status="invalid", error_type=type(exc).__name__)
            limitations.append("The alternatives experiment did not complete.")
    elif blind_alternatives:
        stages["alternatives"] = {
            "model": prompt_config.models["archaeologist"], "status": "skipped",
            "reason": "No valid problem and baseline from concept review.",
            "turns": [], "tools": []}
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

    protect_verification()

    if is_current is not None and not is_current():
        raise model.StaleReview()
    accepted = []
    finding_candidates = {}
    try:
        verified = run_stage(
            "verifier", verifier_input(),
            prompt_config.audit_prompts["verifier"], protocol.VERIFIER_SCHEMA)
        result, accepted = protocol.verification(verified, candidates, snapshot,
                                                 concept_candidate)
        stages["verifier"]["coverage"] = result["coverage"]
        debug["decisions"] = result["decisions"]
        previous_concept = debug.get("concept_assessment", {})
        concept_status = ("verified" if result["concept"]["disposition"] == "publish"
                          else result["concept"]["disposition"])
        if concept_candidate is None and previous_concept.get("status") in {"skipped", "failed"}:
            concept_status = previous_concept["status"]
        debug["concept_assessment"] = {
            **previous_concept,
            "status": concept_status,
            "verification": result["concept"],
        }
        if result["concept_validation_error"]:
            stages["verifier"]["concept_validation_error"] = result["concept_validation_error"]
        verified_concept = (result["concept"]["assessment"]
                            if result["concept"]["disposition"] == "publish"
                            else None)
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
    if accepted or verified_concept is not None:
        try:
            edited = run_stage("collator", json.dumps({
                                   "findings": accepted,
                                   "concept_assessment": verified_concept,
                               }),
                               prompt_config.audit_prompts["collator"],
                               protocol.COLLATOR_SCHEMA, tools=False)
            findings, concept_summary = protocol.collation(
                edited, accepted, verified_concept)
            if concept_summary is not None:
                debug["concept_assessment"] = {
                    **debug.get("concept_assessment", {}),
                    "status": "verified",
                    "summary": concept_summary,
                    "edited_by": "collator",
                }
        except model.StaleReview:
            raise
        except Exception as exc:
            if stages["collator"]["status"] == "completed":
                stages["collator"].update(status="invalid", error_type=type(exc).__name__)
            if isinstance(exc, protocol.InvalidReview):
                stages["collator"]["validation_error"] = str(exc)
            stages["collator"]["used_verified_wording"] = True
            concept_summary = protocol.concept_summary(
                verified_concept, debug["concept_assessment"]["verification"]["reason"])
            if concept_summary is not None:
                debug["concept_assessment"] = {
                    **debug.get("concept_assessment", {}),
                    "status": "verified",
                    "summary": concept_summary,
                    "used_verified_wording": True,
                }
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
    return protocol.render(findings, limitations, concept_summary,
                           verified_concept["alternatives"] if verified_concept else ())
