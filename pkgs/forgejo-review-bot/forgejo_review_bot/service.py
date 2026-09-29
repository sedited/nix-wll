"""Webhook server and review worker."""
import argparse
import hashlib
import hmac
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, forgejo, pipeline, repository, trace
from .repository import BRANCH, SHA

MAX_BODY = 1024 * 1024

def valid_signature(body, header, secret):
    if not header:
        return False
    supplied = header.removeprefix("sha256=")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied.lower())

def parse_event(bot_config, event, payload):
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
    if (repo.get("full_name") != bot_config.repository
            or (bot_config.repository_url is not None and html_url != bot_config.repository_url.rstrip("/"))
            or not isinstance(number, int) or isinstance(number, bool) or number < 1
            or not isinstance(base_ref, str) or not BRANCH.fullmatch(base_ref)
            or base_ref.startswith("-") or ".." in base_ref or "//" in base_ref
            or base_ref.endswith("/") or base_ref.endswith(".lock")
            or not isinstance(head_sha, str) or not SHA.fullmatch(head_sha)):
        raise ValueError("invalid or unexpected pull request payload")
    if not isinstance(force, bool):
        raise ValueError("invalid review override")
    return number, base_ref, head_sha, payload["action"], force

def requeue_latest_head(jobs, bot_config, number, base_ref, expected_head, action, force):
    latest_head = repository.current_head(bot_config, number)
    if latest_head == expected_head:
        return
    logging.info("review stale head pr=%d expected=%s latest=%s; requeueing",
                 number, short_sha(expected_head), short_sha(latest_head))
    jobs.put((number, base_ref, latest_head, action, force))

def short_sha(sha):
    return sha[:12]

def metric_value(value):
    return "unknown" if value is None else value

def log_review_outcome(number, action, head_sha, outcome, started, debug, prompt_config,
                       level=logging.INFO, stage=None, error_type=None,
                       http_status=None, http_host=None):
    metrics = trace.review_metrics(debug, prompt_config)
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

def worker(jobs, state_dir, api_key, forgejo_token, bot_login, bot_config, prompt_config):
    checkout = state_dir / "checkout"
    while True:
        number, base_ref, expected_head, action, force = jobs.get()
        started = time.monotonic()
        debug = {}
        stage = "precheck"
        try:
            logging.info("review start pr=%d action=%s head=%s force=%s", number, action,
                         short_sha(expected_head), force)
            if not force and forgejo.comment_matches_head(
                    forgejo.find_comment(bot_config, forgejo_token, number, bot_login), expected_head):
                log_review_outcome(number, action, expected_head, "already-reviewed",
                                   started, debug, prompt_config)
                continue
            stage = "context"
            title, description = forgejo.pull_request_context(bot_config, forgejo_token, number)
            stage = "collect"
            base_sha, head_sha, review, skip = repository.collect_review(
                checkout, number, base_ref, expected_head, title, description,
                bot_config)
            if head_sha != expected_head:
                requeue_latest_head(jobs, bot_config, number, base_ref, expected_head, action, force)
                log_review_outcome(number, action, expected_head, "stale", started, debug, prompt_config)
                continue
            debug = {"skip": skip} if skip else {}
            stage = "model"
            snapshot = (None if skip else repository.snapshot_repository(
                checkout, base_sha, head_sha))
            content = f"Skipped: {skip}" if skip else pipeline.review_with_independent_passes(
                api_key, review, snapshot, bot_config, prompt_config, number, debug)
            if not skip:
                try:
                    trace.save_review_trace(state_dir, number, head_sha, content, debug, prompt_config)
                except OSError as exc:
                    logging.warning("Could not save private review trace for PR %d: %s",
                                    number, type(exc).__name__)
            stage = "publish"
            result = forgejo.publish_review(bot_config, prompt_config, forgejo_token,
                                            number, bot_login, base_sha, head_sha,
                                            content, debug)
            if result == "stale":
                requeue_latest_head(jobs, bot_config, number, base_ref, head_sha, action, force)
            log_review_outcome(number, action, expected_head, result, started, debug, prompt_config)
        except urllib.error.HTTPError as exc:
            host = urllib.parse.urlsplit(exc.url or "").hostname
            log_review_outcome(number, action, expected_head, "failed", started, debug, prompt_config,
                               logging.ERROR, debug.get("pipeline_stage", stage),
                               type(exc).__name__, exc.code, host)
        except Exception as exc:
            log_review_outcome(number, action, expected_head, "failed", started, debug, prompt_config,
                               logging.ERROR, debug.get("pipeline_stage", stage),
                               type(exc).__name__)
        finally:
            jobs.task_done()

def make_handler(secret, jobs, bot_config):
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
                job = parse_event(bot_config, self.headers.get("X-Forgejo-Event"), json.loads(body))
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
    parser.add_argument("--prompt-file", type=Path, default=config.DEFAULT_PROMPT_FILE,
                        help="Markdown file containing the review prompt")
    parser.add_argument("--audit-prompt-dir", type=Path, default=config.DEFAULT_AUDIT_DIR,
                        help="Directory containing the focused Luna audit prompts")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    bot_config = config.BotConfig(args.origin, args.repository, args.forgejo_api,
                                  args.repository_url, args.comment_marker)
    prompt_config = config.PromptConfig.load(args.prompt_file, args.audit_prompt_dir)
    api_key = args.openai_key_file.read_text().strip()
    secret = args.webhook_secret_file.read_bytes().strip()
    forgejo_token = args.forgejo_token_file.read_text().strip()
    if not api_key or not secret or not forgejo_token or not args.bot_login:
        parser.error("secret files must not be empty")
    jobs = queue.Queue()
    threading.Thread(target=worker, args=(jobs, args.state_dir, api_key,
                                          forgejo_token, args.bot_login,
                                          bot_config, prompt_config), daemon=True).start()
    server = ThreadingHTTPServer((args.listen, args.port), make_handler(secret, jobs, bot_config))
    server.serve_forever()
