"""Frozen discussion evidence for offline review replay."""

from datetime import datetime, timezone
import hashlib
import json

from . import forgejo


SCHEMA_VERSION = 1
MISSING_RESEARCH = (
    "Frozen research evidence is unavailable for this tool call. Continue "
    "without live discussion or web lookup.")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _canonical(value):
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True))


def _entry_key(name, arguments):
    payload = json.dumps([name, _canonical(arguments)], sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _args(name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("research tool arguments must be an object")
    if name == "search_discussions":
        return {"query": arguments.get("query")}
    if name == "read_discussion":
        return {"number": arguments.get("number")}
    if name == "read_current_pr_discussion":
        return {
            "kind": arguments.get("kind", "comments"),
            "page": arguments.get("page", 1),
            "review_id": arguments.get("review_id", 0),
        }
    if name == "read_github_discussion":
        return {
            "url": arguments.get("url"),
            "kind": arguments.get("kind", "comments"),
            "page": arguments.get("page", 1),
        }
    raise ValueError(f"unsupported research tool: {name}")


def validate(evidence):
    if evidence is None:
        return None
    if not isinstance(evidence, dict):
        raise ValueError("Frozen research evidence must be an object")
    if set(evidence) != {"schema_version", "captured_at", "cutoff", "entries"}:
        raise ValueError("Frozen research evidence has unexpected fields")
    if evidence["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported frozen research evidence schema")
    captured = _parse_time(evidence["captured_at"])
    cutoff = _parse_time(evidence["cutoff"])
    if captured is None or cutoff is None:
        raise ValueError("Frozen research evidence timestamps are invalid")
    if cutoff > captured:
        raise ValueError("Frozen research evidence cutoff is after capture time")
    if not isinstance(evidence["entries"], list):
        raise ValueError("Frozen research evidence entries must be a list")
    seen = set()
    for entry in evidence["entries"]:
        if not isinstance(entry, dict):
            raise ValueError("Frozen research evidence entry must be an object")
        if set(entry) != {"key", "name", "arguments", "output", "captured_at", "cutoff"}:
            raise ValueError("Frozen research evidence entry has unexpected fields")
        name = entry["name"]
        if not isinstance(name, str):
            raise ValueError("Frozen research evidence tool name is invalid")
        arguments = _args(name, entry["arguments"])
        if entry["arguments"] != arguments:
            raise ValueError("Frozen research evidence arguments are not canonical")
        if not isinstance(entry["output"], str):
            raise ValueError("Frozen research evidence output must be text")
        if entry["captured_at"] != evidence["captured_at"] or entry["cutoff"] != evidence["cutoff"]:
            raise ValueError("Frozen research evidence entry timestamps differ")
        key = _entry_key(name, arguments)
        if entry["key"] != key:
            raise ValueError("Frozen research evidence key mismatch")
        if key in seen:
            raise ValueError("Frozen research evidence contains duplicate tool calls")
        seen.add(key)
    return evidence


def lookup(evidence, name, args):
    evidence = validate(evidence)
    if evidence is None:
        return MISSING_RESEARCH
    try:
        arguments = _args(name, args)
    except ValueError:
        return MISSING_RESEARCH
    key = _entry_key(name, arguments)
    for entry in evidence["entries"]:
        if entry["key"] == key:
            return entry["output"]
    return MISSING_RESEARCH


def inventory(evidence):
    evidence = validate(evidence)
    if evidence is None:
        return "No frozen discussion evidence is available."
    lines = [
        f"Frozen discussion evidence captured at {evidence['captured_at']}.",
        f"Cutoff: {evidence['cutoff']}.",
    ]
    if not evidence["entries"]:
        lines.append("No discussion tool calls were captured.")
    for entry in evidence["entries"]:
        arguments = json.dumps(entry["arguments"], sort_keys=True,
                               separators=(",", ":"), ensure_ascii=True)
        lines.append(
            f"- {entry['name']} {arguments}: {len(entry['output'].encode())} bytes, "
            f"key {entry['key'][:12]}")
    return "\n".join(lines)


def _capture_call(bot_config, current_pr, request, cutoff, captured_at):
    if not isinstance(request, dict):
        raise ValueError("research request must be an object")
    name = request.get("name")
    arguments = _args(name, request.get("arguments", {}))
    if name == "search_discussions":
        output = forgejo.search_discussions(
            bot_config, arguments["query"], current_pr, cutoff=cutoff)
    elif name == "read_discussion":
        output = forgejo.read_discussion(
            bot_config, arguments["number"], current_pr, cutoff=cutoff)
    elif name == "read_current_pr_discussion":
        output = forgejo.read_current_pr_discussion(
            bot_config, current_pr, arguments["kind"], arguments["page"],
            arguments["review_id"], cutoff=cutoff)
    elif name == "read_github_discussion":
        output = forgejo.read_github_discussion(
            bot_config, arguments["url"], arguments["kind"], arguments["page"],
            cutoff=cutoff)
    else:
        raise ValueError(f"unsupported research tool: {name}")
    return {"key": _entry_key(name, arguments), "name": name,
            "arguments": arguments, "output": output,
            "captured_at": captured_at, "cutoff": cutoff}


def capture(bot_config, current_pr, requests=None, cutoff=None, captured_at=None):
    captured_at = captured_at or utc_now()
    cutoff = cutoff or captured_at
    captured = _parse_time(captured_at)
    cutoff_time = _parse_time(cutoff)
    if captured is None or cutoff_time is None:
        raise ValueError("Frozen research evidence timestamps are invalid")
    if cutoff_time > captured:
        raise ValueError("Frozen research evidence cutoff is after capture time")
    requests = [
        {"name": "read_current_pr_discussion",
         "arguments": {"kind": "comments", "page": 1, "review_id": 0}},
        {"name": "read_current_pr_discussion",
         "arguments": {"kind": "reviews", "page": 1, "review_id": 0}},
        *list(requests or []),
    ]
    evidence = {"schema_version": SCHEMA_VERSION, "captured_at": captured_at,
                "cutoff": cutoff, "entries": []}
    seen = set()
    for request in requests:
        entry = _capture_call(bot_config, current_pr, request, cutoff, captured_at)
        if entry["key"] in seen:
            continue
        seen.add(entry["key"])
        evidence["entries"].append(entry)
    return validate(evidence)
