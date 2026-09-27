#!/usr/bin/env python3
"""Publish first-pass reviews for Forgejo pull request webhooks."""

import argparse
import hashlib
import hmac
import html
import json
import logging
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ORIGIN = None
REPOSITORY = None
FORGEJO_API = None
REPOSITORY_URL = None
COMMENT_MARKER = None
MAX_BODY = 1024 * 1024
MAX_REVIEW_BYTES = 200_000
MAX_FILE_BYTES = 1_000_000
MAX_TOOL_BYTES = 12_000
MAX_TOOL_CALLS = 24
MAX_MODEL_TURNS = 10
MAX_OUTPUT_TOKENS = 6_000
SHA = re.compile(r"^[0-9a-f]{40}$")
BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
DEFAULT_PROMPT_FILE = Path(__file__).with_name("prompt.md")
INSTRUCTIONS = None
TOOLS = [
    {"type": "function", "name": "read_file", "strict": True,
     "description": "Read numbered lines from a tracked text file at the PR head. "
                    "Use another call for later lines.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Repository-relative file path"},
         "start_line": {"type": "integer", "description": "First line, starting at 1"}},
         "required": ["path", "start_line"], "additionalProperties": False}},
    {"type": "function", "name": "search_code", "strict": True,
     "description": "Search tracked text files at the PR head for a literal string. "
                    "Use to find definitions, callers, tests, and conventions.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Literal code or path fragment"}},
         "required": ["query"], "additionalProperties": False}},
]


def load_prompt_file(path):
    return path.read_text(encoding="utf-8").removesuffix("\n")


def configure_prompt(prompt_file):
    global INSTRUCTIONS
    INSTRUCTIONS = load_prompt_file(prompt_file)


def instructions():
    if INSTRUCTIONS is None:
        configure_prompt(DEFAULT_PROMPT_FILE)
    return INSTRUCTIONS


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
    html_url = repo.get("html_url", "").rstrip("/")
    if (repo.get("full_name") != REPOSITORY
            or (REPOSITORY_URL is not None and html_url != REPOSITORY_URL.rstrip("/"))
            or not isinstance(number, int) or isinstance(number, bool) or number < 1
            or not isinstance(base_ref, str) or not BRANCH.fullmatch(base_ref)
            or base_ref.startswith("-") or ".." in base_ref or "//" in base_ref
            or base_ref.endswith("/") or base_ref.endswith(".lock")
            or not isinstance(head_sha, str) or not SHA.fullmatch(head_sha)):
        raise ValueError("invalid or unexpected pull request payload")
    return number, base_ref, head_sha, payload["action"]


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
    review = (f"PR title: {title}\nPR description:\n{description}\n"
              f"Commits:\n{commits}\nPatch:\n{patch}")
    if len(review.encode()) > MAX_REVIEW_BYTES:
        return base_sha, actual_head, None, f"Review input exceeds {MAX_REVIEW_BYTES} bytes"
    return base_sha, actual_head, review, None


def tracked_files(checkout):
    """Map tracked regular paths to their checked-out Git blob IDs."""
    entries = git(checkout, "ls-files", "--stage", "-z").split("\x00")
    files = {}
    for entry in entries:
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, blob, stage = metadata.split()
        if stage == "0" and mode in {"100644", "100755"}:
            files[path] = blob
    return files


def read_file(checkout, files, path, start_line):
    if (not isinstance(path, str) or path not in files
            or not isinstance(start_line, int) or isinstance(start_line, bool)
            or start_line < 1):
        return "Invalid path or line. Only tracked regular files can be read."
    size = int(git(checkout, "cat-file", "-s", files[path]).strip())
    if size > MAX_FILE_BYTES:
        return f"File exceeds {MAX_FILE_BYTES} bytes."
    content = subprocess.run(
        ["git", "-C", str(checkout), "cat-file", "blob", files[path]],
        check=True, capture_output=True, timeout=30,
    ).stdout.decode(errors="replace")
    if "\x00" in content:
        return "Binary file cannot be read as text."
    lines = content.splitlines()
    if start_line > len(lines):
        return f"{path} has {len(lines)} lines."
    result = f"{path} ({len(lines)} lines):\n"
    for number in range(start_line, min(start_line + 150, len(lines) + 1)):
        line = f"{number}: {lines[number - 1]}\n"
        if len((result + line).encode()) > MAX_TOOL_BYTES:
            break
        result += line
    return result


def search_code(checkout, query):
    if (not isinstance(query, str) or not 3 <= len(query) <= 100
            or "\n" in query or "\r" in query or "\x00" in query):
        return "Search query must be 3 to 100 characters on one line."
    process = subprocess.Popen(
        ["git", "-C", str(checkout), "grep", "-n", "-I", "-F", "-e", query,
         "HEAD", "--"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        output = process.stdout.read(MAX_TOOL_BYTES + 1)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        process.stdout.close()
    if not output:
        return "No matches in tracked text files."
    return output[:MAX_TOOL_BYTES].decode(errors="replace") + (
        "\n[Results truncated]" if len(output) > MAX_TOOL_BYTES else "")


def openai_review(api_key, review, checkout, debug=None):
    prompt = instructions()
    files = tracked_files(checkout)
    inputs = [{"role": "user", "content": review}]
    if debug is not None:
        review_bytes = review.encode()
        debug.update({"instructions": prompt,
                      "review_input_bytes": len(review_bytes),
                      "review_input_sha256": hashlib.sha256(review_bytes).hexdigest(),
                      "turns": [], "tools": []})
    calls_used = 0
    for turn in range(MAX_MODEL_TURNS):
        input_data = json.dumps(inputs).encode()
        tool_choice = ("required" if turn == 0 else
                       "none" if calls_used >= MAX_TOOL_CALLS
                       or turn == MAX_MODEL_TURNS - 1 else "auto")
        payload = json.dumps({"model": "gpt-6-sol", "store": False,
                              "instructions": prompt, "input": inputs,
                              "tools": TOOLS, "tool_choice": tool_choice,
                              "max_output_tokens": MAX_OUTPUT_TOKENS}).encode()
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"}, method="POST",
        )
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
        if debug is not None:
            usage = result.get("usage") or {}
            details = usage.get("input_tokens_details") or {}
            debug["turns"].append({
                "request_bytes": len(payload),
                "request_sha256": hashlib.sha256(payload).hexdigest(),
                "input_bytes": len(input_data),
                "input_sha256": hashlib.sha256(input_data).hexdigest(),
                "tool_choice": tool_choice,
                "response_id": str(result.get("id", ""))[:100],
                "response_model": str(result.get("model", ""))[:100],
                "status": str(result.get("status", ""))[:100],
                "response_output_sha256": hashlib.sha256(
                    json.dumps(result.get("output", [])).encode()).hexdigest(),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "input_tokens": usage.get("input_tokens"),
                "cached_tokens": details.get("cached_tokens", 0),
                "cache_write_tokens": details.get("cache_write_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "reasoning_tokens": (usage.get("output_tokens_details") or {}).get(
                    "reasoning_tokens"),
            })
        if result.get("status") != "completed":
            raise ValueError("OpenAI response did not complete")
        output = result.get("output", [])
        calls = [item for item in output if item.get("type") == "function_call"]
        if calls:
            inputs.extend(output)
            for call in calls:
                if calls_used >= MAX_TOOL_CALLS:
                    answer = "Inspection limit reached. Finish with evidence already available."
                else:
                    calls_used += 1
                    try:
                        args = json.loads(call["arguments"])
                        if call["name"] == "read_file":
                            answer = read_file(checkout, files, args.get("path"),
                                               args.get("start_line"))
                        elif call["name"] == "search_code":
                            answer = search_code(checkout, args.get("query"))
                        else:
                            answer = "Unknown tool."
                    except (KeyError, TypeError, ValueError):
                        answer = "Invalid tool arguments."
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
        text = "\n".join(part["text"] for item in output
                         if item.get("type") == "message"
                         for part in item.get("content", []) if part.get("type") == "output_text")
        if not text.strip():
            raise ValueError("OpenAI response contained no review text")
        return text
    raise ValueError("OpenAI review exceeded model turn limit")


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
    turns = debug.get("turns", [])
    known_usage = all(isinstance(turn.get("input_tokens"), int)
                      and isinstance(turn.get("output_tokens"), int)
                      for turn in turns)
    metrics = {"model_turns": len(turns), "tool_calls": len(debug.get("tools", [])),
               "estimated_cost_usd": None, "total_input_tokens": None,
               "total_output_tokens": None, "total_model_seconds": None}
    if known_usage and turns:
        cost = 0.0
        for turn in turns:
            input_tokens = turn["input_tokens"]
            cached = turn["cached_tokens"] or 0
            written = turn["cache_write_tokens"] or 0
            ordinary = max(0, input_tokens - cached - written)
            multiplier = 2 if input_tokens > 272_000 else 1
            output_multiplier = 1.5 if multiplier == 2 else 1
            cost += (ordinary * 2 + cached * 0.2 + written * 2.5) * multiplier / 1_000_000
            cost += turn["output_tokens"] * 10 * output_multiplier / 1_000_000
        metrics.update({
            "estimated_cost_usd": round(cost, 6),
            "total_input_tokens": sum(turn["input_tokens"] for turn in turns),
            "total_output_tokens": sum(turn["output_tokens"] for turn in turns),
            "total_model_seconds": round(sum(turn["elapsed_seconds"] for turn in turns), 2),
        })
    return metrics


def review_trace(debug):
    prompt = instructions()
    metrics = review_metrics(debug)
    trace = {"model": "gpt-6-sol", "endpoint": "/v1/responses", "store": False,
             "max_output_tokens": MAX_OUTPUT_TOKENS,
             "instructions": debug.get("instructions", prompt),
             "input": "PR text, patch, and commits omitted from public debug output",
             "turns": debug.get("turns", []), "tools": debug.get("tools", [])}
    if "review_input_bytes" in debug:
        trace["review_input_bytes"] = debug["review_input_bytes"]
        trace["review_input_sha256"] = debug["review_input_sha256"]
    if debug.get("skip"):
        trace["skip"] = debug["skip"]
    if metrics["estimated_cost_usd"] is not None:
        trace.update(metrics)
        trace["pricing_note"] = ("Estimated from token usage at gpt-6-sol Standard rates. "
                                 "Missing cache-write counts are treated as zero.")
    else:
        trace["estimated_cost_usd"] = None
    return trace


def debug_section(debug):
    trace = review_trace(debug)
    rendered = html.escape(json.dumps(trace, indent=2, ensure_ascii=True))
    return f"\n<details><summary>Review debug</summary>\n\n<pre>{rendered}</pre>\n</details>\n"


def review_body(base_sha, head_sha, content, debug=None):
    require_config()
    return (f"{COMMENT_MARKER}\n"
            f"First-pass review\n\nBase: `{base_sha}`  \nHead: `{head_sha}`\n\n"
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


def short_sha(sha):
    return sha[:12]


def metric_value(value):
    return "unknown" if value is None else value


def log_review_outcome(number, action, head_sha, outcome, started, debug,
                       level=logging.INFO, stage=None, error_type=None,
                       http_status=None, http_host=None):
    metrics = review_metrics(debug)
    message = ("review outcome pr=%d action=%s head=%s outcome=%s "
               "elapsed_seconds=%.2f model_turns=%d tool_calls=%d "
               "input_tokens=%s output_tokens=%s estimated_usd=%s")
    values = [number, action, short_sha(head_sha), outcome,
              time.monotonic() - started, metrics["model_turns"],
              metrics["tool_calls"], metric_value(metrics["total_input_tokens"]),
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
        number, base_ref, expected_head, action = jobs.get()
        started = time.monotonic()
        debug = {}
        stage = "precheck"
        try:
            logging.info("review start pr=%d action=%s head=%s", number, action,
                         short_sha(expected_head))
            if comment_matches_head(find_comment(forgejo_token, number, bot_login),
                                    expected_head):
                log_review_outcome(number, action, expected_head, "already-reviewed",
                                   started, debug)
                continue
            stage = "context"
            title, description = pull_request_context(forgejo_token, number)
            stage = "collect"
            base_sha, head_sha, review, skip = collect_review(
                checkout, number, base_ref, expected_head, title, description)
            if head_sha != expected_head:
                log_review_outcome(number, action, expected_head, "stale", started, debug)
                continue
            debug = {"skip": skip} if skip else {}
            stage = "model"
            content = f"Skipped: {skip}" if skip else openai_review(api_key, review, checkout, debug)
            stage = "publish"
            result = publish_review(forgejo_token, number, bot_login,
                                    base_sha, head_sha, content, debug)
            log_review_outcome(number, action, expected_head, result, started, debug)
        except urllib.error.HTTPError as exc:
            host = urllib.parse.urlsplit(exc.url or "").hostname
            log_review_outcome(number, action, expected_head, "failed", started, debug,
                               logging.ERROR, stage, type(exc).__name__, exc.code, host)
        except Exception as exc:
            log_review_outcome(number, action, expected_head, "failed", started, debug,
                               logging.ERROR, stage, type(exc).__name__)
        finally:
            jobs.task_done()


def make_handler(secret, jobs):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/webhooks/forgejo":
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
                number, _base_ref, head_sha, action = job
                logging.info("review enqueue pr=%d action=%s head=%s", number, action,
                             short_sha(head_sha))
            self.send_response(202)
            self.end_headers()

        def log_message(self, format, *args):
            log = logging.debug if self.command == "GET" else logging.info
            log("Webhook request: %s", format % args)

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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    configure(args.origin, args.repository, args.forgejo_api,
              args.repository_url, args.comment_marker)
    configure_prompt(args.prompt_file)
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
