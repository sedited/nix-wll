"""OpenAI response requests and bounded model tool calls."""
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request

from . import forgejo
from .repository import (find_paths, read_file, read_diff, search_code, blame_base,
                         read_commit)

MAX_TOOL_CALLS = 24
MAX_FOCUSED_TOOL_CALLS = 6
MAX_MODEL_TURNS = 10
MAX_OUTPUT_TOKENS = 6_000
MAX_VERIFIER_OUTPUT_TOKENS = 8_000
MAX_AUDIT_OUTPUT_TOKENS = 4_000
MAX_AUDIT_OUTPUT_BYTES = 4_000
MAX_COLLATOR_OUTPUT_TOKENS = 6_000
MAX_COLLATOR_OUTPUT_BYTES = 10_000
MAX_CONTEXT_CALLS = 4
MAX_HISTORY_CALLS = 4
TOOLS = [
    {"type": "function", "name": "find_paths", "strict": True,
     "description": "Find tracked file paths at the PR head containing a case-insensitive "
                    "substring. Use when you do not know a file's exact path.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Filename or path substring"}},
         "required": ["query"], "additionalProperties": False}},
    {"type": "function", "name": "read_file", "strict": True,
     "description": "Read numbered lines from a tracked text file at the PR head. "
                    "Use another call for later lines.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Repository-relative file path"},
         "start_line": {"type": "integer", "description": "First line, starting at 1"}},
         "required": ["path", "start_line"], "additionalProperties": False}},
    {"type": "function", "name": "read_base_file", "strict": True,
     "description": "Read numbered lines from a tracked text file at the PR merge base. "
                    "Use to compare behavior before the PR.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Repository-relative file path at the merge base"},
         "start_line": {"type": "integer", "description": "First line, starting at 1"}},
         "required": ["path", "start_line"], "additionalProperties": False}},
    {"type": "function", "name": "read_diff", "strict": True,
     "description": "Read numbered lines from one changed file's PR diff. "
                    "Use for large patches or to revisit a specific change. "
                    "Line numbers count lines in the diff, not the source file.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Changed repository-relative file path"},
         "start_line": {"type": "integer", "description": "First diff line, starting at 1"}},
         "required": ["path", "start_line"], "additionalProperties": False}},
    {"type": "function", "name": "search_code", "strict": True,
     "description": "Search tracked text files at the PR head for a literal string. "
                    "Use to find definitions, callers, tests, and conventions.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Literal code or path fragment"}},
         "required": ["query"], "additionalProperties": False}},
    {"type": "function", "name": "search_discussions", "strict": True,
     "description": "Search this repository's other issues and PRs for a specific "
                    "term. The current PR is excluded. Open a relevant result "
                    "with read_discussion.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Specific term or phrase"}},
         "required": ["query"], "additionalProperties": False}},
    {"type": "function", "name": "read_discussion", "strict": True,
     "description": "Read another same-repository issue or PR by number, "
                    "including its title, description, and a small sample of "
                    "ordinary comments. The current PR is excluded.",
     "parameters": {"type": "object", "properties": {
         "number": {"type": "integer", "description": "Issue or PR number"}},
         "required": ["number"], "additionalProperties": False}},
    {"type": "function", "name": "blame_base", "strict": True,
     "description": "Trace up to 20 lines of an existing tracked file at the PR merge "
                    "base to the last commits that changed them.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Repository-relative file path"},
         "start_line": {"type": "integer", "description": "First line, starting at 1"}},
         "required": ["path", "start_line"], "additionalProperties": False}},
    {"type": "function", "name": "read_commit", "strict": True,
     "description": "Read an ancestor commit's message and bounded diff for one "
                    "tracked file at the PR merge base.",
     "parameters": {"type": "object", "properties": {
         "commit": {"type": "string", "description": "Full commit SHA from blame_base"},
         "path": {"type": "string", "description": "Repository-relative file path"}},
         "required": ["commit", "path"], "additionalProperties": False}},
]
FOCUSED_TOOLS = TOOLS[:5]

def request_response(api_key, data, stage_name, on_response=None):
    payload = json.dumps(data).encode()
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses", data=payload,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"}, method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    elapsed = round(time.monotonic() - started, 2)
    if on_response is not None:
        on_response(stage_name, payload, result)
    return result, payload, elapsed


def response_record(result, payload, elapsed):
    usage = result.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    return {"request_bytes": len(payload),
            "request_sha256": hashlib.sha256(payload).hexdigest(),
            "response_output_sha256": hashlib.sha256(
                json.dumps(result.get("output", [])).encode()).hexdigest(),
            "elapsed_seconds": elapsed,
            "input_tokens": usage.get("input_tokens"),
            "cached_tokens": details.get("cached_tokens", 0),
            "cache_write_tokens": details.get("cache_write_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": (usage.get("output_tokens_details") or {}).get(
                "reasoning_tokens")}


def output_text(output):
    return "\n".join(part["text"] for item in output
                     if item.get("type") == "message"
                     for part in item.get("content", [])
                     if part.get("type") == "output_text")


def run_audit(api_key, name, prompt, review, prompt_config, notes="", on_response=None):
    model = prompt_config.models[name]
    output_tokens = (MAX_COLLATOR_OUTPUT_TOKENS if name == "collator"
                     else MAX_AUDIT_OUTPUT_TOKENS)
    output_bytes = (MAX_COLLATOR_OUTPUT_BYTES if name == "collator"
                    else MAX_AUDIT_OUTPUT_BYTES)
    input_text = review + ("\n\nMerge-base doc/developer-notes.md:\n" + notes
                           if name == "developer_notes" else "")
    result, payload, elapsed = request_response(api_key, {
        "model": model, "store": False, "reasoning": {"effort": "low"},
        "instructions": prompt, "input": [{"role": "user", "content": input_text}],
        "max_output_tokens": output_tokens}, name, on_response)
    answer = output_text(result.get("output", []))
    status = result.get("status")
    record = {"name": name, "model": model, "status": status,
              "incomplete_reason": (result.get("incomplete_details") or {}).get("reason"),
              **response_record(result, payload, elapsed),
              "output_truncated": len(answer.encode()) > output_bytes}
    if status != "completed" or not answer.strip():
        if status == "completed":
            record["status"] = "empty"
        return "Audit unavailable.", record
    clipped = answer.encode()[:output_bytes].decode(errors="replace")
    if len(answer.encode()) > output_bytes:
        clipped += "\n[Audit output truncated]"
    return clipped, record

def openai_review(api_key, review, snapshot, bot_config, prompt_config, debug=None,
                  current_pr=None, prompt=None, model=None, tools=None,
                  max_tool_calls=MAX_TOOL_CALLS, first_tool_required=True,
                  max_output_tokens=MAX_OUTPUT_TOKENS, stage_name="independent",
                  on_response=None):
    prompt = prompt_config.instructions if prompt is None else prompt
    model = prompt_config.models["independent"] if model is None else model
    tools = TOOLS if tools is None else tools
    checkout = snapshot.checkout
    files = snapshot.head_files
    inputs = [{"role": "user", "content": review}]
    if debug is not None:
        review_bytes = review.encode()
        debug.update({"instructions": prompt,
                      "review_input_bytes": len(review_bytes),
                      "review_input_sha256": hashlib.sha256(review_bytes).hexdigest(),
                      "turns": [], "tools": []})
    calls_used = 0
    context_calls = 0
    history_calls = 0
    for turn in range(MAX_MODEL_TURNS):
        input_data = json.dumps(inputs).encode()
        tool_choice = ("none" if calls_used >= max_tool_calls
                       or turn == MAX_MODEL_TURNS - 1 else
                       "required" if turn == 0 and first_tool_required else "auto")
        result, payload, elapsed = request_response(api_key, {
            "model": model, "store": False, "instructions": prompt,
            "input": inputs, "tools": tools, "tool_choice": tool_choice,
            "max_output_tokens": max_output_tokens}, stage_name, on_response)
        if debug is not None:
            debug["turns"].append({
                **response_record(result, payload, elapsed),
                "input_bytes": len(input_data),
                "input_sha256": hashlib.sha256(input_data).hexdigest(),
                "tool_choice": tool_choice,
                "response_id": str(result.get("id", ""))[:100],
                "response_model": str(result.get("model", ""))[:100],
                "status": str(result.get("status", ""))[:100],
            })
        if result.get("status") != "completed":
            raise ValueError("OpenAI response did not complete")
        output = result.get("output", [])
        calls = [item for item in output if item.get("type") == "function_call"]
        if calls:
            inputs.extend(output)
            for call in calls:
                if calls_used >= max_tool_calls:
                    answer = "Inspection limit reached. Finish with evidence already available."
                else:
                    calls_used += 1
                    try:
                        args = json.loads(call["arguments"])
                        if call["name"] == "find_paths":
                            answer = find_paths(files, args.get("query"))
                        elif call["name"] == "read_file":
                            answer = read_file(checkout, files, args.get("path"),
                                               args.get("start_line"))
                        elif call["name"] == "read_base_file":
                            answer = read_file(checkout, snapshot.base_files, args.get("path"),
                                               args.get("start_line"))
                        elif call["name"] == "read_diff":
                            answer = read_diff(checkout, snapshot.changed_paths,
                                               snapshot.merge_base, snapshot.head_sha,
                                               args.get("path"), args.get("start_line"))
                        elif call["name"] == "search_code":
                            answer = search_code(checkout, snapshot.head_sha, args.get("query"))
                        elif call["name"] in {"search_discussions", "read_discussion"}:
                            if current_pr is None:
                                answer = "Discussion lookup unavailable without the current PR number."
                            elif context_calls >= MAX_CONTEXT_CALLS:
                                answer = "Discussion lookup limit reached."
                            else:
                                context_calls += 1
                                if call["name"] == "search_discussions":
                                    answer = forgejo.search_discussions(bot_config, args.get("query"), current_pr)
                                else:
                                    answer = forgejo.read_discussion(bot_config, args.get("number"), current_pr)
                        elif call["name"] in {"blame_base", "read_commit"}:
                            if history_calls >= MAX_HISTORY_CALLS:
                                answer = "History lookup limit reached."
                            else:
                                history_calls += 1
                                if call["name"] == "blame_base":
                                    answer = blame_base(checkout, snapshot.base_files, snapshot.merge_base,
                                                        args.get("path"), args.get("start_line"))
                                else:
                                    answer = read_commit(checkout, snapshot.base_files, snapshot.merge_base,
                                                         args.get("commit"), args.get("path"))
                        else:
                            answer = "Unknown tool."
                    except (KeyError, TypeError, ValueError):
                        answer = "Invalid tool arguments."
                    except urllib.error.HTTPError as exc:
                        answer = f"Discussion lookup returned HTTP {exc.code}."
                    except (urllib.error.URLError, TimeoutError, subprocess.CalledProcessError):
                        answer = "Context lookup failed; continue with available evidence."
                inputs.append({"type": "function_call_output",
                               "call_id": call["call_id"], "output": answer})
                if debug is not None:
                    arguments = str(call.get("arguments", ""))
                    debug["tools"].append({
                        "name": str(call.get("name", ""))[:100],
                        "arguments": arguments[:160],
                        "arguments_sha256": hashlib.sha256(arguments.encode()).hexdigest(),
                        "output_bytes": len(answer.encode()),
                        "output_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                    })
            continue
        text = output_text(output)
        if not text.strip():
            raise ValueError("OpenAI response contained no review text")
        return text
    raise ValueError("OpenAI review exceeded model turn limit")
