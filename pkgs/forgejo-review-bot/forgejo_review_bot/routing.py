"""Choose bounded review work from repository evidence and conservative rules."""

import json
import re

from .config import ADVERSARIAL_PROFILES, AUDIT_NAMES
from .protocol import InvalidReview, STRINGS, object_schema
from .repository import MAX_REVIEW_BYTES, is_test_path
from .spend import BudgetExceeded

ROUTER_SCHEMA = object_schema({
    "tier": {"type": "string", "enum": ["routine", "standard", "sensitive"]},
    "audits": {"type": "array", "items": {"type": "string", "enum": list(AUDIT_NAMES)}},
    "profiles": {"type": "array",
                 "items": {"type": "string", "enum": list(ADVERSARIAL_PROFILES)}},
    "evidence": STRINGS,
    "missing_context": STRINGS,
})
TIERS = ("routine", "standard", "sensitive")
DOC_EXTENSIONS = (".md", ".rst", ".txt")
BUILD = re.compile(
    r"^(?:depends/|cmake/|contrib/guix/|ci/|build-aux/|build_msvc/|"
    r"(?:CMakeLists\.txt|configure\.ac|guix\.sigs)(?:$|/)|"
    r".*/CMakeLists\.txt$|(?:Cargo|flake)\.lock$)"
)
CRYPTO = re.compile(r"^src/(?:crypto|secp256k1)(?:/|[._])")
CONCURRENCY = re.compile(
    r"^src/(?:sync|threadsafety|scheduler|checkqueue|validationinterface|"
    r"util/thread|util/threadinterrupt)(?:[./_]|$)"
)
CONSENSUS = re.compile(
    r"^src/(?:(?:consensus|primitives|script)/|"
    r"(?:validation|coins|serialize|serialization|streams|key|pubkey|pow|"
    r"chain|chainparams|undo|blockstorage|chainstate|kernel)(?:[./_]|$)|"
    r"node/(?:chainstate|blockstorage)(?:[./_]|$))"
)
P2P = re.compile(
    r"^src/(?:(?:net|policy)(?:[./_]|$)|"
    r"(?:net_processing|netbase|addrman|txmempool|mempool)(?:[./_]|$))"
)
PUBLIC_CONTRACT = re.compile(
    r"^src/(?:(?:rpc|wallet/rpc)/|qt/rpcconsole(?:[./_]|$)|"
    r"(?:bitcoin-cli|httprpc|httpserver|rest|init|common/args|util/system)"
    r"(?:[./_]|$))"
)
STATE = re.compile(
    r"^src/(?:(?:db|index|wallet/(?:bdb|db|sqlite))/|"
    r"(?:dbwrapper|txdb|blockstorage|chainstate)(?:[./_]|$)|"
    r"node/(?:chainstate|blockstorage)(?:[./_]|$))"
)
WALLET = re.compile(r"^src/wallet(?:/|[._])")


def _doc_path(path):
    return path.endswith(DOC_EXTENSIONS)


def _production_path(path):
    return not _doc_path(path) and not is_test_path(path) and not BUILD.search(path)


def domain_profiles(paths):
    profiles = set()
    for path in paths:
        if is_test_path(path):
            continue
        if CONSENSUS.search(path):
            profiles.add("consensus")
        if WALLET.search(path):
            profiles.add("wallet")
        if P2P.search(path):
            profiles.add("p2p")
    return profiles


def domain_audits(paths):
    audits = set()
    if any(is_test_path(path) or _production_path(path) for path in paths):
        audits.add("tests")
    if any(_production_path(path) for path in paths):
        audits.add("design")
    for path in paths:
        if is_test_path(path):
            continue
        if CONCURRENCY.search(path):
            audits.add("concurrency")
        if STATE.search(path):
            audits.add("state")
        if PUBLIC_CONTRACT.search(path):
            audits.add("public_contract")
        if BUILD.search(path):
            audits.add("build")
    return audits


def minimum_tier(paths):
    if domain_profiles(paths):
        return "sensitive"
    if any((CONCURRENCY.search(path) or STATE.search(path) or PUBLIC_CONTRACT.search(path)
            or BUILD.search(path) or CRYPTO.search(path))
           for path in paths if not is_test_path(path)):
        return "sensitive"
    if all(_doc_path(path) or is_test_path(path) for path in paths):
        return "routine"
    return "standard"


def full_plan(reason):
    return {"tier": "sensitive", "audits": list(AUDIT_NAMES),
            "profiles": list(ADVERSARIAL_PROFILES), "evidence": [reason],
            "missing_context": []}


def validate_plan(text, paths, floor):
    floor = TIERS[max(TIERS.index(floor), TIERS.index(minimum_tier(paths)))]
    result = json.loads(text)
    if not isinstance(result, dict) or set(result) != set(ROUTER_SCHEMA["properties"]):
        raise InvalidReview("Invalid routing fields")
    if result["tier"] not in TIERS:
        raise InvalidReview("Invalid routing tier")
    for key in ("audits", "profiles", "evidence", "missing_context"):
        if not isinstance(result[key], list) or any(not isinstance(item, str) for item in result[key]):
            raise InvalidReview("Invalid routing list")
    if set(result["audits"]) - set(AUDIT_NAMES):
        raise InvalidReview("Unknown audit role")
    if set(result["profiles"]) - set(ADVERSARIAL_PROFILES):
        raise InvalidReview("Unknown adversarial profile")
    if result["missing_context"]:
        return full_plan("Router reported missing context")
    selected_profiles = set(result["profiles"]) | domain_profiles(paths)
    if selected_profiles:
        result["tier"] = "sensitive"
    result["tier"] = TIERS[max(TIERS.index(result["tier"]), TIERS.index(floor))]
    selected = set(result["audits"]) | domain_audits(paths)
    result["audits"] = [name for name in AUDIT_NAMES if name in selected]
    result["profiles"] = [name for name in ADVERSARIAL_PROFILES if name in selected_profiles]
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
        except BudgetExceeded as exc:
            record.update(status="budget_exhausted", error_type=type(exc).__name__)
            raise
        except Exception as exc:
            record.update(status="failed", error_type=type(exc).__name__)
            proposed = full_plan("Routing unavailable; conservative review required")
    actual = full_plan("Shadow routing retains full review") if mode == "shadow" else proposed
    debug["routing"] = {"mode": mode, "minimum_tier": floor,
                        "proposed": proposed, "selected": actual}
    return actual
