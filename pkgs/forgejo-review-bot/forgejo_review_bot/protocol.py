"""Structured stage results and the boundary between findings and prose."""

import json


class InvalidReview(ValueError):
    pass


def object_schema(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
COVERAGE = object_schema({
    "status": {"type": "string", "enum": ["complete", "partial"]},
    "limitations": STRINGS,
})
LOCATION = {"path": STRING, "line": {"type": "integer"},
            "side": {"type": "string", "enum": ["head", "base"]}}
CANDIDATE = object_schema({
    "kind": {"type": "string", "enum": ["defect", "suggestion"]},
    **LOCATION,
    **{key: STRING for key in ("title", "claim", "consequence", "evidence",
                               "correction", "uncertainty")},
})
FINDING = object_schema({
    "severity": {"type": "string", "enum": ["critical", "major", "minor", "suggestion"]},
    **LOCATION, "title": STRING, "body": STRING,
})
DISCOVERY_SCHEMA = object_schema({
    "coverage": COVERAGE,
    "requires_sensitive_review": {"type": "boolean"},
    "findings": {"type": "array", "items": CANDIDATE},
})
VERIFIER_SCHEMA = object_schema({
    "coverage": COVERAGE,
    "decisions": {"type": "array", "items": object_schema({
        "candidate_ids": STRINGS,
        "disposition": {"type": "string", "enum": ["publish", "drop", "unresolved"]},
        "reason": STRING,
        "finding": {"anyOf": [FINDING, {"type": "null"}]},
    })},
})
COLLATOR_SCHEMA = object_schema({
    "findings": {"type": "array", "items": object_schema({
        "id": STRING, "title": STRING, "body": STRING,
    })},
})


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise InvalidReview("Unexpected review fields")


def _strings(value):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InvalidReview("Expected a list of strings")


def _text(record, keys, empty=()):
    for key in keys:
        if not isinstance(record[key], str) or (key not in empty and not record[key].strip()):
            raise InvalidReview("Missing finding text")


def _coverage(value):
    _object(value, COVERAGE["properties"])
    _strings(value["limitations"])
    if value["status"] not in {"complete", "partial"}:
        raise InvalidReview("Invalid coverage status")
    if value["limitations"]:
        value["status"] = "partial"


def _location(value, snapshot):
    if value["side"] not in {"head", "base"}:
        raise InvalidReview("Invalid location side")
    files = snapshot.head_files if value["side"] == "head" else snapshot.base_files
    if (not isinstance(value["path"], str) or value["path"] not in snapshot.changed_paths
            or value["path"] not in files or type(value["line"]) is not int
            or value["line"] < 1):
        raise InvalidReview("Finding must identify a changed file and positive line")


def discovery(text, stage, snapshot):
    result = json.loads(text)
    _object(result, DISCOVERY_SCHEMA["properties"])
    _coverage(result["coverage"])
    if type(result["requires_sensitive_review"]) is not bool or not isinstance(result["findings"], list):
        raise InvalidReview("Invalid discovery response")
    for index, finding in enumerate(result["findings"], 1):
        _object(finding, CANDIDATE["properties"])
        _location(finding, snapshot)
        if finding["kind"] not in {"defect", "suggestion"}:
            raise InvalidReview("Invalid candidate kind")
        _text(finding, ("title", "claim", "consequence", "evidence", "correction", "uncertainty"),
              empty=("correction", "uncertainty"))
        finding["id"] = f"{stage}:{index}"
    return result


def verification(text, candidates, snapshot):
    result = json.loads(text)
    _object(result, VERIFIER_SCHEMA["properties"])
    _coverage(result["coverage"])
    if not isinstance(result["decisions"], list):
        raise InvalidReview("Invalid verifier decisions")
    expected = {candidate["id"] for candidate in candidates}
    seen = set()
    accepted = []
    for decision in result["decisions"]:
        _object(decision, VERIFIER_SCHEMA["properties"]["decisions"]["items"]["properties"])
        ids = decision["candidate_ids"]
        _strings(ids)
        if len(set(ids)) != len(ids) or set(ids) - expected or seen.intersection(ids):
            raise InvalidReview("Unknown or repeated candidate ID")
        seen.update(ids)
        _text(decision, ("reason",))
        disposition = decision["disposition"]
        if disposition not in {"publish", "drop", "unresolved"}:
            raise InvalidReview("Invalid verifier disposition")
        finding = decision["finding"]
        if disposition == "publish":
            _object(finding, FINDING["properties"])
            _location(finding, snapshot)
            if finding["severity"] not in FINDING["properties"]["severity"]["enum"]:
                raise InvalidReview("Invalid severity")
            _text(finding, ("title", "body"))
            accepted.append({**finding, "id": f"finding:{len(accepted) + 1}"})
        elif finding is not None or not ids:
            raise InvalidReview("Only publish decisions may introduce a finding")
    if seen != expected:
        raise InvalidReview("Verifier omitted candidate decisions")
    return result, accepted


def collation(text, accepted):
    result = json.loads(text)
    _object(result, COLLATOR_SCHEMA["properties"])
    if not isinstance(result["findings"], list):
        raise InvalidReview("Invalid collator findings")
    originals = {finding["id"]: finding for finding in accepted}
    seen = set()
    edited = []
    for finding in result["findings"]:
        _object(finding, ("id", "title", "body"))
        _text(finding, ("id", "title", "body"))
        identifier = finding["id"]
        if identifier not in originals or identifier in seen:
            raise InvalidReview("Collator added or repeated a finding")
        seen.add(identifier)
        edited.append({**originals[identifier], **finding})
    if seen != set(originals):
        raise InvalidReview("Collator omitted an accepted finding")
    return edited


def render(findings, limitations=()):
    sections = []
    for severity, heading in (("critical", "🔴 Critical"), ("major", "🟠 Major"),
                              ("minor", "🟡 Minor"), ("suggestion", "💡 Suggestion")):
        group = [finding for finding in findings if finding["severity"] == severity]
        if group:
            sections.append(f"##### {heading}\n\n" + "\n\n".join(
                f"**{finding['title']}** ({finding['path']}:{finding['line']}, "
                f"{finding['side']})\n\n{finding['body']}" for finding in group))
    if not sections:
        sections.append("I found no actionable issues in this static review." if not limitations
                        else "No verified findings are available from this partial review.")
    if limitations:
        sections.append("Review coverage was incomplete. " + " ".join(dict.fromkeys(limitations)))
    return "\n\n".join(sections)
