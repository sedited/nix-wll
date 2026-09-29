"""Choose bounded review work from repository evidence and conservative rules."""

import json
import re

from .config import AUDIT_NAMES
from .protocol import InvalidReview, STRINGS, object_schema
from .repository import MAX_REVIEW_BYTES, is_test_path

ROUTER_SCHEMA = object_schema({
    "tier": {"type": "string", "enum": ["routine", "standard", "sensitive"]},
    "audits": {"type": "array", "items": {"type": "string", "enum": list(AUDIT_NAMES)}},
    "evidence": STRINGS,
    "missing_context": STRINGS,
})
TIERS = ("routine", "standard", "sensitive")
SENSITIVE = re.compile(
    r"^src/(consensus/|script/|wallet/|crypto/|secp256k1/|"
    r"(?:validation|net_processing|net|netbase|txmempool|coins|dbwrapper|"
    r"serialize|streams|key|pubkey|sync|scheduler|checkqueue|addrman|"
    r"blockstorage|chainstate|kernel|policy)(?:[./_]|$))"
    r"|^src/.*\.h$|^depends/|^cmake/|(^|/)CMakeLists\.txt$"
    r"|^configure\.ac$|(^|/)(?:Cargo\.lock|flake\.lock)$"
)


def minimum_tier(paths):
    if any(SENSITIVE.search(path) and not is_test_path(path) for path in paths):
        return "sensitive"
    if all(path.endswith((".md", ".rst", ".txt")) for path in paths):
        return "routine"
    return "standard"


def full_plan(reason):
    return {"tier": "sensitive", "audits": list(AUDIT_NAMES),
            "evidence": [reason], "missing_context": []}


def validate_plan(text, paths, floor):
    result = json.loads(text)
    if not isinstance(result, dict) or set(result) != set(ROUTER_SCHEMA["properties"]):
        raise InvalidReview("Invalid routing fields")
    if result["tier"] not in TIERS:
        raise InvalidReview("Invalid routing tier")
    for key in ("audits", "evidence", "missing_context"):
        if not isinstance(result[key], list) or any(not isinstance(item, str) for item in result[key]):
            raise InvalidReview("Invalid routing list")
    if set(result["audits"]) - set(AUDIT_NAMES):
        raise InvalidReview("Unknown audit role")
    if result["missing_context"]:
        return full_plan("Router reported missing context")
    result["tier"] = TIERS[max(TIERS.index(result["tier"]), TIERS.index(floor))]
    selected = set(result["audits"])
    # Keep the requested design/taste pass on substantive changes. Testing
    # quality also matters when only production behavior changes.
    if any(not path.endswith((".md", ".rst", ".txt")) for path in paths):
        selected.update(("design", "tests"))
    if result["tier"] == "sensitive":
        selected.add("state")
    result["audits"] = [name for name in AUDIT_NAMES if name in selected]
    return result


def plan_review(api_key, review, snapshot, prompt_config, debug,
                mode="enabled", budget=None, is_current=None):
    from . import model

    if mode not in {"enabled", "shadow", "full"}:
        raise ValueError("Unknown routing mode")
    paths = sorted(snapshot.changed_paths)
    floor = minimum_tier(paths)
    record = {"model": prompt_config.models["router"], "status": "skipped",
              "turns": [], "tools": []}
    debug.setdefault("stages", {})["router"] = record
    if mode == "full":
        proposed = full_plan("Full review requested")
    elif floor == "sensitive":
        proposed = full_plan("Sensitive paths require independent Sol discovery")
    elif f"Patch exceeds {MAX_REVIEW_BYTES} input bytes." in review:
        proposed = full_plan("Initial patch is incomplete")
    else:
        # The complete manifest precedes the patch; a model cannot silently
        # classify only the excerpt it happened to receive.
        router_input = json.dumps({"minimum_tier": floor, "changed_paths": paths}) + "\n" + review
        try:
            answer, response = model.run_audit(
                api_key, "router", prompt_config.audit_prompts["router"],
                router_input, prompt_config, budget=budget,
                response_schema=ROUTER_SCHEMA, is_current=is_current, debug=record)
            record["status"] = response["status"]
            record["raw_output"] = answer
            debug.setdefault("stage_outputs", {})["router"] = answer
            if response["status"] != "completed":
                raise InvalidReview("Router response incomplete")
            proposed = validate_plan(answer, paths, floor)
        except model.StaleReview:
            raise
        except Exception as exc:
            record.update(status="failed", error_type=type(exc).__name__)
            proposed = full_plan("Routing unavailable; conservative review required")
    actual = full_plan("Shadow routing retains full review") if mode == "shadow" else proposed
    debug["routing"] = {"mode": mode, "minimum_tier": floor,
                        "proposed": proposed, "selected": actual}
    return actual
