#!/usr/bin/env python3
"""Publish first-pass reviews for Forgejo pull request webhooks."""

import argparse
import hashlib
import hmac
import html
import json
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ORIGIN = None
REPOSITORY = None
FORGEJO_API = None
REPOSITORY_URL = None
COMMENT_MARKER = None
MAX_BODY = 1024 * 1024
MAX_REVIEW_BYTES = 200_000
MAX_FOCUSED_DIFF_BYTES = 80_000
MAX_AUDIT_OUTPUT_BYTES = 4_000
MAX_COLLATOR_OUTPUT_BYTES = 10_000
MAX_PUBLIC_STAGE_OUTPUT_BYTES = 4_000
MAX_AUDIT_DOC_BYTES = 100_000
SHA = re.compile(r"^[0-9a-f]{40}$")
BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
DEFAULT_PROMPT_FILE = Path(__file__).with_name("prompt.md")
DEFAULT_AUDIT_DIR = Path(__file__).with_name("audits")
AUDIT_NAMES = ("state", "public_contract", "tests", "developer_notes", "design")
TOOLED_AUDITS = ("tests", "design")
MODEL_NAMES = ("independent", "adversarial", *AUDIT_NAMES, "verifier", "collator")
MODEL_RATES = {"gpt-6-luna": (0.1, 0.01, 0.125, 0.5),
               "gpt-6.1-sol": (2, 0.1, 2.5, 10)}
INSTRUCTIONS = None
AUDIT_PROMPTS = None
MODELS = None
SECRET_PATHS = ("/run/secrets",)


def configure_secret_paths(*paths):
    global SECRET_PATHS
    SECRET_PATHS = tuple(sorted({"/run/secrets",
                                 *(str(path.absolute()) for path in paths),
                                 *(str(path.resolve()) for path in paths)}))


def load_prompt_file(path):
    return path.read_text(encoding="utf-8").removesuffix("\n")


def configure_prompt(prompt_file):
    global INSTRUCTIONS
    INSTRUCTIONS = load_prompt_file(prompt_file)


def instructions():
    if INSTRUCTIONS is None:
        configure_prompt(DEFAULT_PROMPT_FILE)
    return INSTRUCTIONS


def configure_audit_prompts(directory):
    global AUDIT_PROMPTS, MODELS
    prompts = {name: load_prompt_file(directory / f"{name}.md")
               for name in ("common", "adversarial", *AUDIT_NAMES,
                            "verifier", "collator")}
    if any(not prompt.strip() for prompt in prompts.values()):
        raise ValueError("audit prompt files must not be empty")
    models = json.loads((directory / "models.json").read_text(encoding="utf-8"))
    if (set(models) != set(MODEL_NAMES)
            or any(not isinstance(model, str) or not model.startswith("gpt-")
                   for model in models.values())):
        raise ValueError("model config must name each review stage")
    AUDIT_PROMPTS = prompts
    MODELS = models


def audit_prompts():
    if AUDIT_PROMPTS is None:
        configure_audit_prompts(DEFAULT_AUDIT_DIR)
    return AUDIT_PROMPTS


def stage_models():
    if MODELS is None:
        configure_audit_prompts(DEFAULT_AUDIT_DIR)
    return MODELS


def default_repository_url(forgejo_api):
    api_marker = "/api/v1/repos/"
    forgejo_api = forgejo_api.rstrip("/")
    if api_marker not in forgejo_api:
        return None
    base_url, repository = forgejo_api.split(api_marker, 1)
    return f"{base_url.rstrip('/')}/{repository}".rstrip("/")


def default_comment_marker(repository):
    return f"<!-- forgejo-review-bot:{repository} -->"


def configure(origin, repository, forgejo_api, repository_url=None, comment_marker=None):
    global ORIGIN, REPOSITORY, FORGEJO_API, REPOSITORY_URL, COMMENT_MARKER
    if not origin or not repository or not forgejo_api:
        raise ValueError("origin, repository, and forgejo_api are required")
    ORIGIN = origin
    REPOSITORY = repository
    FORGEJO_API = forgejo_api.rstrip("/")
    REPOSITORY_URL = (repository_url or default_repository_url(FORGEJO_API))
    if not REPOSITORY_URL:
        raise ValueError("repository_url is required when forgejo_api is not a repository API URL")
    COMMENT_MARKER = comment_marker or default_comment_marker(repository)


def require_config():
    if not all([ORIGIN, REPOSITORY, FORGEJO_API, COMMENT_MARKER]):
        raise RuntimeError("bot configuration is incomplete")


def valid_signature(body, header, secret):
    if not header:
        return False
    supplied = header.removeprefix("sha256=")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied.lower())


def parse_event(event, payload):
    require_config()
    if event != "pull_request" or payload.get("action") not in {
        "opened", "reopened", "synchronize", "synchronized"
    }:
        return None
    repo = payload.get("repository") or {}
    pr = payload.get("pull_request") or {}
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    number = pr.get("number", payload.get("number"))
    base_ref = base.get("ref")
    head_sha = head.get("sha", "")
    force = payload.get("review_bot_force", False)
    html_url = repo.get("html_url", "").rstrip("/")
    if (repo.get("full_name") != REPOSITORY
            or (REPOSITORY_URL is not None and html_url != REPOSITORY_URL.rstrip("/"))
            or not isinstance(number, int) or isinstance(number, bool) or number < 1
            or not isinstance(base_ref, str) or not BRANCH.fullmatch(base_ref)
            or base_ref.startswith("-") or ".." in base_ref or "//" in base_ref
            or base_ref.endswith("/") or base_ref.endswith(".lock")
            or not isinstance(head_sha, str) or not SHA.fullmatch(head_sha)):
        raise ValueError("invalid or unexpected pull request payload")
    if not isinstance(force, bool):
        raise ValueError("invalid review override")
    return number, base_ref, head_sha, payload["action"], force


def git(checkout, *args):
    return subprocess.run(
        ["git", "-C", str(checkout), *args], check=True, capture_output=True,
        text=True, timeout=180,
    ).stdout


def prepare_checkout(checkout):
    require_config()
    if not (checkout / ".git").exists():
        checkout.mkdir(parents=True, exist_ok=True)
        git(checkout, "init", "-q")
        git(checkout, "remote", "add", "origin", ORIGIN)
    if git(checkout, "remote", "get-url", "origin").strip() != ORIGIN:
        raise ValueError("checkout origin does not match configured repository")


def collect_review(checkout, number, base_ref, expected_head, title, description):
    prepare_checkout(checkout)
    git(checkout, "fetch", "--no-tags", "--filter=blob:none", "origin",
        f"+refs/heads/{base_ref}:refs/review-bot/base",
        f"+refs/pull/{number}/head:refs/review-bot/head")
    actual_head = git(checkout, "rev-parse", "refs/review-bot/head").strip()
    base_sha = git(checkout, "rev-parse", "refs/review-bot/base").strip()
    if actual_head != expected_head:
        return base_sha, actual_head, None, "PR head changed before review"
    merge_base = git(checkout, "merge-base", "refs/review-bot/base", actual_head).strip()
    git(checkout, "checkout", "--detach", "--force", "-q", actual_head)
    commits = git(checkout, "log", "--reverse", "--format=%H%n%B%n%x00", f"{merge_base}..{actual_head}")
    patch = git(checkout, "diff", "--no-ext-diff", "--binary", f"{merge_base}..{actual_head}")
    prelude = (f"PR title: {title}\nPR description:\n{description}\n"
               f"Commits:\n{commits}\n")
    review = f"{prelude}Patch:\n{patch}"
    if len(review.encode()) > MAX_REVIEW_BYTES:
        changed = git(checkout, "diff", "--no-ext-diff", "--name-status",
                      f"{merge_base}..{actual_head}")
        review = (f"{prelude}Patch exceeds {MAX_REVIEW_BYTES} input bytes. "
                  "Inspect the checkout diff for changed files.\nChanged files:\n"
                  f"{changed}")
        if len(review.encode()) > MAX_REVIEW_BYTES:
            return base_sha, actual_head, None, f"Review input exceeds {MAX_REVIEW_BYTES} bytes"
    return base_sha, actual_head, review, None


def audit_developer_notes(checkout):
    base = git(checkout, "merge-base", "refs/review-bot/base", "HEAD").strip()
    try:
        notes = git(checkout, "show", f"{base}:doc/developer-notes.md")
    except subprocess.CalledProcessError:
        return "Developer notes are unavailable at the PR merge base."
    content = notes.encode()[:MAX_AUDIT_DOC_BYTES].decode(errors="replace")
    return content + ("\n[Developer notes truncated]"
                      if len(notes.encode()) > MAX_AUDIT_DOC_BYTES else "")


def run_audit(api_key, name, prompt, review, notes=""):
    model = stage_models()[name]
    output_bytes = (MAX_COLLATOR_OUTPUT_BYTES if name == "collator"
                    else MAX_AUDIT_OUTPUT_BYTES)
    input_text = review + ("\n\nMerge-base doc/developer-notes.md:\n" + notes
                           if name == "developer_notes" else "")
    with tempfile.TemporaryDirectory() as directory:
        answer, turn, _tools = codex_review(api_key, model, prompt, input_text,
                                            Path(directory))
    record = {"name": name, "model": model, "status": "completed", **turn,
              "output_truncated": len(answer.encode()) > output_bytes}
    clipped = answer.encode()[:output_bytes].decode(errors="replace")
    if len(answer.encode()) > output_bytes:
        clipped += "\n[Audit output truncated]"
    return clipped, record


def codex_review(api_key, model, prompt, review, checkout):
    """Run one isolated Codex session and require a completed final answer."""
    denied = ",".join(json.dumps(path) + '="deny"' for path in SECRET_PATHS)
    command = ["codex", "exec", "--json", "--ephemeral",
               "--skip-git-repo-check",
               "-C", str(checkout), "-m", model,
               "-c", 'default_permissions="review"',
               "-c", 'permissions.review.extends=":read-only"',
               "-c", "permissions.review.filesystem={" + denied + "}",
               "-c", "project_doc_max_bytes=0",
               "-c", "shell_environment_policy.ignore_default_excludes=false",
               "-c", 'shell_environment_policy.filters.CODEX_API_KEY="exclude"',
               "-c", "developer_instructions=" + json.dumps(prompt, ensure_ascii=False),
               "-"]
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as codex_home:
        env = dict(os.environ, CODEX_API_KEY=api_key, CODEX_HOME=codex_home)
        env.pop("OPENAI_API_KEY", None)
        result = subprocess.run(command, input=review, capture_output=True,
                                text=True, env=env, timeout=1800)
    if result.returncode:
        raise ValueError(f"Codex exited with status {result.returncode}")
    answer = None
    usage = None
    tool_events = []
    completed = False
    for line in result.stdout.splitlines():
        event = json.loads(line)
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "item.completed":
            if item.get("type") == "agent_message":
                answer = item.get("text")
            elif item.get("type") in {"command_execution", "web_search", "mcp_tool_call"}:
                tool_events.append({"name": item["type"],
                                    "status": item.get("status")})
        elif kind == "turn.completed":
            completed = True
            usage = event.get("usage") or {}
        elif kind in {"turn.failed", "error"}:
            raise ValueError("Codex review failed")
    if not completed or not isinstance(answer, str) or not answer.strip():
        raise ValueError("Codex review did not complete with text")
    details = usage.get("input_tokens_details") or {}
    turn = {"status": "completed", "request_bytes": len(review.encode()),
            "request_sha256": hashlib.sha256(review.encode()).hexdigest(),
            "response_output_sha256": hashlib.sha256(answer.encode()).hexdigest(),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "input_tokens": usage.get("input_tokens"),
            "cached_tokens": usage.get("cached_input_tokens", details.get("cached_tokens", 0)),
            "cache_write_tokens": usage.get("cache_write_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": usage.get("reasoning_output_tokens",
                                           (usage.get("output_tokens_details") or {}).get(
                                               "reasoning_tokens"))}
    return answer, turn, tool_events


def focused_review_input(review, checkout, name):
    if f"Patch exceeds {MAX_REVIEW_BYTES} input bytes. Inspect the checkout diff" not in review:
        return review
    base = git(checkout, "merge-base", "refs/review-bot/base", "HEAD").strip()
    paths = [path for path in git(checkout, "diff", "--name-only", "-z",
                                  f"{base}..HEAD").split("\x00") if path]
    if name == "tests":
        paths = [path for path in paths if path.startswith(("test/", "tests/", "qa/"))]
    elif name == "design":
        paths = [path for path in paths if not path.startswith(("test/", "tests/", "qa/", "doc/"))]
    excerpt = []
    size = 0
    for path in paths:
        patch = git(checkout, "diff", "--no-ext-diff", "--no-color",
                    f"{base}..HEAD", "--", path)
        if size + len(patch.encode()) > MAX_FOCUSED_DIFF_BYTES:
            excerpt.append(patch.encode()[:MAX_FOCUSED_DIFF_BYTES - size]
                           .decode(errors="replace"))
            excerpt.append("\n[Diff excerpt truncated. Inspect the checkout diff for the rest.]\n")
            break
        excerpt.append(patch)
        size += len(patch.encode())
    return review + "\nRelevant diff excerpt:\n" + "".join(excerpt)


def run_focused_review(api_key, name, prompt, review, checkout):
    stage_debug = {}
    answer = codex_stage_review(api_key, review, checkout, stage_debug,
                                prompt=prompt, model=stage_models()[name])
    turns = stage_debug["turns"]
    record = {"name": name, "model": stage_models()[name], "status": "completed",
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


def run_audits(api_key, review, checkout, debug):
    prompts = audit_prompts()
    models = stage_models()
    notes = audit_developer_notes(checkout)
    with ThreadPoolExecutor(max_workers=len(AUDIT_NAMES)) as pool:
        futures = {}
        for name in AUDIT_NAMES:
            prompt = prompts["common"] + "\n\n" + prompts[name]
            input_text = focused_review_input(review, checkout, name)
            if name in TOOLED_AUDITS:
                futures[name] = pool.submit(run_focused_review, api_key, name,
                                            prompt, input_text, checkout)
            else:
                futures[name] = pool.submit(run_audit, api_key, name,
                                            prompt, input_text, notes)
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
    return "Focused Luna reviews:\n" + "\n\n".join(results)


def codex_stage_review(api_key, review, checkout, debug=None, prompt=None, model=None):
    prompt = instructions() if prompt is None else prompt
    model = stage_models()["independent"] if model is None else model
    answer, turn, tool_events = codex_review(api_key, model, prompt, review, checkout)
    if debug is not None:
        review_bytes = review.encode()
        debug.update({"instructions": prompt,
                      "review_input_bytes": len(review_bytes),
                      "review_input_sha256": hashlib.sha256(review_bytes).hexdigest(),
                      "turns": [turn], "tools": tool_events})
    return answer


def review_with_independent_passes(api_key, review, checkout, debug):
    debug["pipeline_stage"] = "independent"
    sol_debug = {}
    adversarial_debug = {}
    debug["adversarial"] = adversarial_debug
    with ThreadPoolExecutor(max_workers=3) as pool:
        audits_future = pool.submit(run_audits, api_key, review, checkout, debug)
        sol_future = pool.submit(codex_stage_review, api_key, review, checkout,
                                 sol_debug)
        adversarial_future = pool.submit(
            codex_stage_review, api_key, review, checkout, adversarial_debug,
            prompt=audit_prompts()["adversarial"], model=stage_models()["adversarial"])
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
               f"Adversarial Sol review:\n{adversarial_review}\n\n{luna_reviews}")

    debug["pipeline_stage"] = "verification"
    verification_debug = {}
    debug["verification"] = verification_debug
    verified = codex_stage_review(
        api_key, review + "\n\nIndependent candidate reviews:\n" + reviews,
        checkout, verification_debug,
        prompt=audit_prompts()["verifier"], model=stage_models()["verifier"])
    stage_outputs["verifier"] = verified
    debug["verification_output_sha256"] = hashlib.sha256(verified.encode()).hexdigest()

    debug["pipeline_stage"] = "collation"
    content, record = run_audit(api_key, "collator", audit_prompts()["collator"],
                                reviews + "\n\nVerification decisions:\n" + verified)
    stage_outputs["collator"] = content
    debug["collator"] = record
    if record["status"] != "completed" or record["output_truncated"]:
        raise ValueError("Luna collator did not return a complete comment")
    debug.pop("pipeline_stage")
    return content


def forgejo_request(token, path, method="GET", data=None):
    require_config()
    headers = {"Authorization": f"token {token}", "Accept": "application/json",
               "User-Agent": "ForgejoReviewBot/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{FORGEJO_API}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers=headers, method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def pull_request_context(token, number):
    issue = forgejo_request(token, f"/issues/{number}")
    if (not isinstance(issue, dict) or issue.get("number") != number
            or not isinstance(issue.get("pull_request"), dict)):
        raise ValueError("Forgejo returned invalid pull request")
    title, description = issue.get("title"), issue.get("body")
    if not isinstance(title, str) or not (description is None or isinstance(description, str)):
        raise ValueError("Forgejo returned invalid pull request text")
    return title, description or ""


def find_comment(token, number, bot_login):
    page = 1
    marker_from_other_user = False
    seen_pages = set()
    while True:
        comments = forgejo_request(token, f"/issues/{number}/comments?limit=50&page={page}")
        if not isinstance(comments, list):
            raise ValueError("Forgejo returned invalid comments")
        ids = tuple(comment.get("id") for comment in comments)
        if ids in seen_pages:
            break
        seen_pages.add(ids)
        for comment in comments:
            if COMMENT_MARKER in comment.get("body", ""):
                if comment.get("user", {}).get("login") == bot_login:
                    return comment
                marker_from_other_user = True
        if len(comments) < 50:
            break
        page += 1
    if marker_from_other_user:
        raise ValueError("Review marker belongs to another user")
    return None


def current_head(number):
    require_config()
    result = subprocess.run(
        ["git", "ls-remote", ORIGIN, f"refs/pull/{number}/head"],
        check=True, capture_output=True, text=True, timeout=180,
    ).stdout.strip()
    fields = result.split()
    if len(fields) != 2 or not SHA.fullmatch(fields[0]) or fields[1] != f"refs/pull/{number}/head":
        raise ValueError("Git returned invalid PR head")
    return fields[0]


def review_metrics(debug):
    models = stage_models()
    independent_turns = debug.get("turns", [])
    adversarial_turns = debug.get("adversarial", {}).get("turns", [])
    verifier_turns = debug.get("verification", {}).get("turns", [])
    turns = independent_turns + adversarial_turns + verifier_turns
    tools = (debug.get("tools", []) + debug.get("adversarial", {}).get("tools", [])
             + debug.get("verification", {}).get("tools", []))
    audits = debug.get("audits", [])
    calls = [(turn, MODEL_RATES.get(models["independent"]))
             for turn in independent_turns]
    calls += [(turn, MODEL_RATES.get(models["adversarial"]))
              for turn in adversarial_turns]
    calls += [(turn, MODEL_RATES.get(models["verifier"]))
              for turn in verifier_turns]
    calls += [(audit, MODEL_RATES.get(audit.get("model", models[audit["name"]])))
              for audit in audits
              if isinstance(audit.get("input_tokens"), int)
              and isinstance(audit.get("output_tokens"), int)]
    collator = debug.get("collator")
    if collator and isinstance(collator.get("input_tokens"), int) \
            and isinstance(collator.get("output_tokens"), int):
        calls.append((collator, MODEL_RATES.get(
            collator.get("model", models["collator"]))))
    known_usage = all(isinstance(call.get("input_tokens"), int)
                      and isinstance(call.get("output_tokens"), int)
                      for call, _rates in calls)
    metrics = {"model_turns": len(turns),
               "tool_calls": len(tools) + sum(audit.get("tool_calls", 0)
                                              for audit in audits),
               "audit_calls": len(audits),
               "estimated_cost_usd": None, "total_input_tokens": None,
               "total_output_tokens": None, "total_model_seconds": None}
    if known_usage and calls:
        metrics.update({
            "total_input_tokens": sum(call["input_tokens"] for call, _ in calls),
            "total_output_tokens": sum(call["output_tokens"] for call, _ in calls),
            "total_model_seconds": round(sum(call["elapsed_seconds"] for call, _ in calls), 2),
        })
        if all(rates is not None for _call, rates in calls):
            cost = 0.0
            for call, rates in calls:
                input_tokens = call["input_tokens"]
                cached = call["cached_tokens"] or 0
                written = call["cache_write_tokens"] or 0
                ordinary = max(0, input_tokens - cached - written)
                multiplier = 2 if input_tokens > 272_000 else 1
                output_multiplier = 1.5 if multiplier == 2 else 1
                cost += (ordinary * rates[0] + cached * rates[1]
                         + written * rates[2]) * multiplier / 1_000_000
                cost += call["output_tokens"] * rates[3] * output_multiplier / 1_000_000
            metrics["estimated_cost_usd"] = round(cost, 6)
    return metrics


def review_trace(debug):
    prompt = instructions()
    metrics = review_metrics(debug)
    trace = {"models": stage_models(), "endpoint": "codex exec",
             "instructions": debug.get("instructions", prompt),
             "input": "PR text, patch, and commits omitted from public debug output",
             "turns": debug.get("turns", []), "tools": debug.get("tools", []),
             "adversarial": debug.get("adversarial", {}),
             "audits": debug.get("audits", []),
             "verification": debug.get("verification", {}),
             "collator": debug.get("collator")}
    if debug.get("stage_outputs"):
        trace["stage_outputs"] = {
            name: {"text": output.encode()[:MAX_PUBLIC_STAGE_OUTPUT_BYTES]
                   .decode(errors="replace"),
                   "truncated": len(output.encode()) > MAX_PUBLIC_STAGE_OUTPUT_BYTES}
            for name, output in debug["stage_outputs"].items()
        }
        trace["stage_outputs_note"] = (
            "Preliminary agent responses are unverified; only the review above "
            "is intended as a public finding. Full responses are retained in "
            "the bot's private state when trace storage succeeds.")
    if "review_input_bytes" in debug:
        trace["review_input_bytes"] = debug["review_input_bytes"]
        trace["review_input_sha256"] = debug["review_input_sha256"]
    if debug.get("skip"):
        trace["skip"] = debug["skip"]
    if debug.get("pipeline_stage"):
        trace["pipeline_stage"] = debug["pipeline_stage"]
    for key in ("independent_review_sha256", "adversarial_review_sha256",
                "verification_output_sha256"):
        if key in debug:
            trace[key] = debug[key]
    if metrics["estimated_cost_usd"] is not None:
        trace.update(metrics)
        trace["pricing_note"] = ("Estimated from token usage at configured "
                                 "gpt-6.1-sol and gpt-6-luna Standard rates. "
                                 "Only calls with reported usage are counted; "
                                 "missing cache-write counts are treated as zero.")
    else:
        trace["estimated_cost_usd"] = None
    return trace


def debug_section(debug):
    trace = review_trace(debug)
    rendered = html.escape(json.dumps(trace, indent=2, ensure_ascii=True))
    return f"\n<details><summary>Review debug</summary>\n\n<pre>{rendered}</pre>\n</details>\n"


def save_review_trace(state_dir, number, head_sha, content, debug):
    trace_dir = state_dir / "review-traces"
    trace_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = trace_dir / f"{number}-{head_sha}-{time.time_ns()}.json"
    record = {"pr": number, "head": head_sha, "review": content,
              "stage_outputs": debug.get("stage_outputs", {}),
              "trace": review_trace(debug)}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(record, file, ensure_ascii=False, indent=2)
        file.write("\n")
    return path


def review_body(base_sha, head_sha, content, debug=None):
    require_config()
    return (f"{COMMENT_MARKER}\n"
            f"Base: `{base_sha}`  \nHead: `{head_sha}`\n\n"
            f"{content.strip()}\n"
            f"{debug_section(debug) if debug is not None else ''}")


def comment_matches_head(comment, head_sha):
    return (comment is not None
            and f"Head: `{head_sha}`" in comment.get("body", "").splitlines()[:5])


def publish_review(token, number, bot_login, base_sha, head_sha, content, debug=None):
    body = review_body(base_sha, head_sha, content, debug)
    comment = find_comment(token, number, bot_login)
    # Check as close as possible to publication, after paginating old comments.
    if current_head(number) != head_sha:
        return "stale"
    if comment is None:
        created = forgejo_request(token, f"/issues/{number}/comments", "POST", {"body": body})
        if created.get("user", {}).get("login") != bot_login:
            raise ValueError("Forgejo token does not belong to bot account")
        return "created"
    if comment.get("body") == body:
        return "unchanged"
    forgejo_request(token, f"/issues/comments/{comment['id']}", "PATCH", {"body": body})
    return "updated"


def requeue_latest_head(jobs, number, base_ref, expected_head, action, force):
    latest_head = current_head(number)
    if latest_head == expected_head:
        return
    logging.info("review stale head pr=%d expected=%s latest=%s; requeueing",
                 number, short_sha(expected_head), short_sha(latest_head))
    jobs.put((number, base_ref, latest_head, action, force))


def short_sha(sha):
    return sha[:12]


def metric_value(value):
    return "unknown" if value is None else value


def log_review_outcome(number, action, head_sha, outcome, started, debug,
                       level=logging.INFO, stage=None, error_type=None,
                       http_status=None, http_host=None):
    metrics = review_metrics(debug)
    message = ("review outcome pr=%d action=%s head=%s outcome=%s "
               "elapsed_seconds=%.2f model_turns=%d audit_calls=%d tool_calls=%d "
               "input_tokens=%s output_tokens=%s estimated_usd=%s")
    values = [number, action, short_sha(head_sha), outcome,
              time.monotonic() - started, metrics["model_turns"],
              metrics["audit_calls"], metrics["tool_calls"],
              metric_value(metrics["total_input_tokens"]),
              metric_value(metrics["total_output_tokens"]),
              metric_value(metrics["estimated_cost_usd"])]
    if stage is not None:
        message += " stage=%s error_type=%s http_status=%s http_host=%s"
        values.extend([stage, error_type, metric_value(http_status),
                       metric_value(http_host)])
    logging.log(level, message, *values)


def worker(jobs, state_dir, api_key, forgejo_token, bot_login):
    checkout = state_dir / "checkout"
    while True:
        number, base_ref, expected_head, action, force = jobs.get()
        started = time.monotonic()
        debug = {}
        stage = "precheck"
        try:
            logging.info("review start pr=%d action=%s head=%s force=%s", number, action,
                         short_sha(expected_head), force)
            if not force and comment_matches_head(
                    find_comment(forgejo_token, number, bot_login), expected_head):
                log_review_outcome(number, action, expected_head, "already-reviewed",
                                   started, debug)
                continue
            stage = "context"
            title, description = pull_request_context(forgejo_token, number)
            stage = "collect"
            base_sha, head_sha, review, skip = collect_review(
                checkout, number, base_ref, expected_head, title, description)
            if head_sha != expected_head:
                requeue_latest_head(jobs, number, base_ref, expected_head, action, force)
                log_review_outcome(number, action, expected_head, "stale", started, debug)
                continue
            debug = {"skip": skip} if skip else {}
            stage = "model"
            content = f"Skipped: {skip}" if skip else review_with_independent_passes(
                api_key, review, checkout, debug)
            if not skip:
                try:
                    save_review_trace(state_dir, number, head_sha, content, debug)
                except OSError as exc:
                    logging.warning("Could not save private review trace for PR %d: %s",
                                    number, type(exc).__name__)
            stage = "publish"
            result = publish_review(forgejo_token, number, bot_login,
                                    base_sha, head_sha, content, debug)
            if result == "stale":
                requeue_latest_head(jobs, number, base_ref, head_sha, action, force)
            log_review_outcome(number, action, expected_head, result, started, debug)
        except urllib.error.HTTPError as exc:
            host = urllib.parse.urlsplit(exc.url or "").hostname
            log_review_outcome(number, action, expected_head, "failed", started, debug,
                               logging.ERROR, debug.get("pipeline_stage", stage),
                               type(exc).__name__, exc.code, host)
        except Exception as exc:
            log_review_outcome(number, action, expected_head, "failed", started, debug,
                               logging.ERROR, debug.get("pipeline_stage", stage),
                               type(exc).__name__)
        finally:
            jobs.task_done()


def make_handler(secret, jobs):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request_path = urllib.parse.urlsplit(self.path).path
            if request_path != "/webhooks/forgejo":
                def safe(value):
                    return "".join(char for char in value if char.isprintable())[:200] or "-"

                logging.warning(
                    "webhook rejected reason=unexpected_path path=%s host=%s event=%s signature_present=%s",
                    safe(request_path), safe(self.headers.get("Host", "")),
                    safe(self.headers.get("X-Forgejo-Event", "")),
                    bool(self.headers.get("X-Forgejo-Signature")),
                )
                self.send_error(404)
                return
            size = self.headers.get("Content-Length", "")
            if not size.isdecimal() or int(size) > MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(int(size))
            if not valid_signature(body, self.headers.get("X-Forgejo-Signature"), secret):
                self.send_error(401)
                return
            try:
                job = parse_event(self.headers.get("X-Forgejo-Event"), json.loads(body))
            except (ValueError, TypeError, AttributeError):
                self.send_error(400)
                return
            if job:
                jobs.put(job)
                number, _base_ref, head_sha, action, force = job
                logging.info("review enqueue pr=%d action=%s head=%s force=%s", number,
                             action, short_sha(head_sha), force)
            self.send_response(202)
            self.end_headers()

        def log_message(self, format, *args):
            log = logging.debug if self.command == "GET" else logging.info
            message = (format % args).replace(self.path, urllib.parse.urlsplit(self.path).path)
            log("Webhook request: %s", message)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--origin", required=True,
                        help="Git remote URL used to fetch the base branch and PR heads")
    parser.add_argument("--repository", required=True,
                        help="Forgejo repository full name, such as owner/repo")
    parser.add_argument("--forgejo-api", required=True,
                        help="Forgejo repository API URL, ending in /api/v1/repos/owner/repo")
    parser.add_argument("--repository-url",
                        help="Expected repository HTML URL from webhook payloads")
    parser.add_argument("--comment-marker",
                        help="Hidden marker used to find the bot's editable comment")
    parser.add_argument("--openai-key-file", type=Path, required=True)
    parser.add_argument("--webhook-secret-file", type=Path, required=True)
    parser.add_argument("--forgejo-token-file", type=Path, required=True)
    parser.add_argument("--bot-login", required=True)
    parser.add_argument("--prompt-file", type=Path, default=DEFAULT_PROMPT_FILE,
                        help="Markdown file containing the review prompt")
    parser.add_argument("--audit-prompt-dir", type=Path, default=DEFAULT_AUDIT_DIR,
                        help="Directory containing the focused Luna audit prompts")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    configure(args.origin, args.repository, args.forgejo_api,
              args.repository_url, args.comment_marker)
    configure_prompt(args.prompt_file)
    configure_audit_prompts(args.audit_prompt_dir)
    secrets = (args.openai_key_file, args.webhook_secret_file,
               args.forgejo_token_file)
    configure_secret_paths(*secrets)
    api_key = args.openai_key_file.read_text().strip()
    secret = args.webhook_secret_file.read_bytes().strip()
    forgejo_token = args.forgejo_token_file.read_text().strip()
    if not api_key or not secret or not forgejo_token or not args.bot_login:
        parser.error("secret files must not be empty")
    jobs = queue.Queue()
    threading.Thread(target=worker, args=(jobs, args.state_dir, api_key,
                                          forgejo_token, args.bot_login), daemon=True).start()
    server = ThreadingHTTPServer((args.listen, args.port), make_handler(secret, jobs))
    server.serve_forever()


if __name__ == "__main__":
    main()
