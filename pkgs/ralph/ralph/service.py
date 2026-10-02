"""Webhook server and review worker."""
import argparse
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import badge, config, forgejo, model, pipeline, report, repository, spend, trace
from .jobs import JobStore
from .repository import BRANCH, SHA

MAX_BODY = 1024 * 1024
CHECKOUT_LOCK = threading.Lock()

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
    force = payload.get("ralph_force", False)
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
    if force and "sha" in base:
        historical_base = base["sha"]
        if not isinstance(historical_base, str) or not SHA.fullmatch(historical_base):
            raise ValueError("invalid historical base")
        base_ref = f"sha:{historical_base}"
    return number, base_ref, head_sha, payload["action"], force

def requeue_latest_head(jobs, bot_config, number, base_ref, expected_head, action, force):
    latest_head = repository.current_head(bot_config, number)
    if latest_head == expected_head:
        return
    logging.info("review stale head pr=%d expected=%s latest=%s; requeueing",
                 number, short_sha(expected_head), short_sha(latest_head))
    jobs.enqueue(number, base_ref, latest_head, action, force)

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

def retryable_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    return isinstance(exc, (urllib.error.URLError, TimeoutError,
                            subprocess.TimeoutExpired))


def process_job(job, jobs, state_dir, api_key, forgejo_token, bot_login,
                bot_config, prompt_config, ledger, routing_mode="enabled",
                allow_discussions=True, ppq_api_key=None, ppq_ledger=None):
    number = job["number"]
    base_ref = job["base_ref"]
    expected_head = job["head"]
    action = job["action"]
    force = job["force"]
    checkout = state_dir / "checkout"
    ref_prefix = f'refs/ralph/jobs/{job["id"]}-{job["generation"]}'
    started = time.monotonic()
    debug = {}
    content = ""
    stage = "precheck"
    outcome = "failed"
    failure = None
    budget = spend.RequestBudget(
        ledger, review_id=f'pr:{number}:{job["id"]}:{job["generation"]}')

    ppq_budget = (spend.RequestBudget(ppq_ledger, budget.review_id)
                  if ppq_ledger is not None else None)

    def is_current():
        return (jobs.is_current(job)
                and repository.current_head(bot_config, number) == expected_head)

    try:
        try:
            logging.info("review start pr=%d action=%s head=%s force=%s", number, action,
                         short_sha(expected_head), force)
            saved = job["review_result"]
            if saved is None:
                if not is_current():
                    raise model.StaleReview("PR head changed")
                comment = (None if force else forgejo.find_comment(
                    bot_config, forgejo_token, number, bot_login))
                stage = "context"
                title, description = forgejo.pull_request_context(
                    bot_config, forgejo_token, number)
                stage = "collect"
                with CHECKOUT_LOCK:
                    base_sha, head_sha, review, skip = repository.collect_review(
                        checkout, number, base_ref, expected_head, title, description,
                        bot_config, ref_prefix=ref_prefix)
                    if head_sha != expected_head:
                        if jobs.is_current(job):
                            jobs.enqueue(number, base_ref, head_sha, action, force)
                        raise model.StaleReview("PR head changed during fetch")
                    if (forgejo.comment_matches_head(comment, head_sha)
                            and f"Base: `{base_sha}`" in [line.strip() for line in comment["body"].splitlines()[:5]]):
                        jobs.complete(job)
                        outcome = "already-reviewed"
                        return outcome
                    snapshot = (None if skip else repository.snapshot_repository(
                        checkout, base_sha, head_sha))
                debug = {"skip": skip} if skip else {}
                stage = "model"
                content = (f"Skipped: {skip}" if skip else
                           pipeline.review_with_independent_passes(
                               api_key, review, snapshot, bot_config, prompt_config,
                               number, debug, budget=budget, is_current=is_current,
                               routing_mode=routing_mode,
                               allow_discussions=allow_discussions, ppq_api_key=ppq_api_key,
                               ppq_budget=ppq_budget))
                debug["budget"] = budget.summary()
                saved = {"base_sha": base_sha, "head_sha": head_sha,
                         "content": content, "debug": debug}
                if not jobs.save_result(job, saved):
                    outcome = "superseded"
                    return outcome
            else:
                base_sha = saved["base_sha"]
                head_sha = saved["head_sha"]
                content = saved["content"]
                debug = saved["debug"]
            if not is_current():
                raise model.StaleReview("PR head changed before publication")
            if bot_config.report_dir is not None:
                stage = "report"
                report_id = f'{number}-{head_sha}-{job["id"]}-{job["generation"]}'
                try:
                    filename = report.save_report(
                        bot_config.report_dir, number, head_sha, content, debug,
                        prompt_config, report_id)
                    debug["report_url"] = f"{bot_config.report_base_url}/{filename}"
                except OSError as exc:
                    debug.pop("report_url", None)
                    logging.warning("Could not save public review report for PR %d: %s",
                                    number, type(exc).__name__)
            stage = "publish"
            result = forgejo.publish_review(bot_config, prompt_config, forgejo_token,
                                            number, bot_login, base_sha, head_sha,
                                            content, debug)
            if result == "stale":
                if jobs.is_current(job):
                    requeue_latest_head(jobs, bot_config, number, base_ref,
                                        head_sha, action, force)
                jobs.supersede(job)
            else:
                jobs.complete(job)
            outcome = result
            return outcome
        except model.StaleReview:
            stage = "stale"
            if jobs.is_current(job):
                requeue_latest_head(jobs, bot_config, number, base_ref,
                                    expected_head, action, force)
            jobs.supersede(job)
            outcome = "stale"
            return outcome
    except Exception as exc:
        failure = exc
        outcome = (jobs.retry(job, exc) if retryable_error(exc)
                   else "failed" if jobs.fail(job, exc) else "lost-claim")
        if outcome is None:
            outcome = "lost-claim"
        return outcome
    finally:
        if (checkout / ".git").exists():
            try:
                with CHECKOUT_LOCK:
                    repository.release_review_refs(checkout, ref_prefix)
            except (OSError, subprocess.SubprocessError) as exc:
                logging.warning("Could not release review refs for PR %d: %s",
                                number, type(exc).__name__)
        if debug and "budget" not in debug:
            try:
                debug["budget"] = budget.summary()
            except sqlite3.Error as exc:
                logging.error("Could not read review spend summary: %s",
                              type(exc).__name__)
        if debug and job["review_result"] is None:
            try:
                trace.save_review_trace(state_dir, number, expected_head,
                                        content, debug, prompt_config)
            except (OSError, TypeError, ValueError) as exc:
                logging.warning("Could not save private review trace for PR %d: %s",
                                number, type(exc).__name__)
        http_status = failure.code if isinstance(failure, urllib.error.HTTPError) else None
        http_host = (urllib.parse.urlsplit(failure.url or "").hostname
                     if isinstance(failure, urllib.error.HTTPError) else None)
        log_review_outcome(number, action, expected_head, outcome, started,
                           debug, prompt_config,
                           logging.ERROR if failure else logging.INFO,
                           debug.get("pipeline_stage", stage) if failure else None,
                           type(failure).__name__ if failure else None,
                           http_status, http_host)
        try:
            summary = ledger.summary()
            logging.info("monthly spend month=%s estimated_usd=%.6f reserved_usd=%.6f "
                         "unknown_requests=%d usage_complete=%s",
                         summary["month"], summary["estimated_total_usd"],
                         summary["reserved_total_usd"],
                         summary["unknown_request_count"], summary["usage_complete"])
        except sqlite3.Error as exc:
            logging.error("Could not read monthly spend summary: %s",
                          type(exc).__name__)


def worker(jobs, state_dir, api_key, forgejo_token, bot_login, bot_config,
           prompt_config, ledger, routing_mode="enabled", allow_discussions=True,
           stop_event=None, ppq_api_key=None, ppq_ledger=None):
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            job = jobs.claim()
        except Exception:
            logging.exception("Could not claim review job")
            stop_event.wait(1)
            continue
        if job is None:
            stop_event.wait(1)
            continue
        try:
            process_job(job, jobs, state_dir, api_key, forgejo_token, bot_login,
                        bot_config, prompt_config, ledger, routing_mode,
                        allow_discussions,
                        **({"ppq_api_key": ppq_api_key, "ppq_ledger": ppq_ledger}
                           if ppq_api_key is not None else {}))
        except Exception as exc:
            logging.exception("Review worker failed unexpectedly for PR %d", job["number"])
            try:
                if retryable_error(exc):
                    jobs.retry(job, exc)
                else:
                    jobs.fail(job, exc)
            except Exception:
                logging.exception("Could not record failed review for PR %d", job["number"])
            stop_event.wait(1)

def make_handler(secret, jobs, bot_config):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request_path = urllib.parse.urlsplit(self.path).path
            match = re.fullmatch(
                rf"/assets/{re.escape(bot_config.repository)}/([1-9][0-9]{{0,18}})\.png",
                request_path)
            if match is None or int(match[1]) > 2 ** 63 - 1:
                self.send_error(404)
                return
            try:
                result = jobs.latest_completed_result(int(match[1]))
            except sqlite3.Error:
                logging.exception("Could not read review badge status")
                self.send_error(503)
                return
            debug = result.get("debug", {}) if result else {}
            if result is None or debug.get("skip") or "decisions" not in debug:
                image = badge.GREY
            elif (any(decision.get("disposition") == "publish"
                      for decision in debug.get("decisions", []))
                  or trace.published_concept_concern(debug)):
                image = badge.ORANGE
            else:
                image = badge.GREEN
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(image)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(image)

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
                number, base_ref, head_sha, action, force = job
                try:
                    queued = jobs.enqueue(number, base_ref, head_sha, action, force)
                except Exception:
                    logging.exception("Could not persist webhook job for PR %d", number)
                    self.send_error(503)
                    return
                logging.info("review enqueue pr=%d action=%s head=%s force=%s queued=%s",
                             number, action, short_sha(head_sha), force, queued)
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
    parser.add_argument("--workers", type=int, default=3,
                        help="Maximum concurrent pull request reviews")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path,
                        help="Public directory for static HTML and JSON review reports")
    parser.add_argument("--report-base-url",
                        help="Public HTTP URL serving the report directory")
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
    parser.add_argument("--ppq-key-file", type=Path, required=True)
    parser.add_argument("--webhook-secret-file", type=Path, required=True)
    parser.add_argument("--forgejo-token-file", type=Path, required=True)
    parser.add_argument("--bot-login", required=True)
    parser.add_argument("--prompt-file", type=Path, default=config.DEFAULT_PROMPT_FILE,
                        help="Markdown file containing the review prompt")
    parser.add_argument("--audit-prompt-dir", type=Path, default=config.DEFAULT_AUDIT_DIR,
                        help="Directory containing the focused Luna audit prompts")
    parser.add_argument("--models-json", type=Path,
                        help="JSON model routing for each review stage")
    parser.add_argument("--review-budget-usd", type=float, default=1.00)
    parser.add_argument("--ppq-review-budget-usd", type=float, default=0.50)
    parser.add_argument("--monthly-budget-usd", type=float)
    parser.add_argument("--routing-mode", choices=("enabled", "shadow", "full"),
                        default="enabled")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("workers must be positive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    bot_config = config.BotConfig(args.origin, args.repository, args.forgejo_api,
                                  args.repository_url, args.comment_marker,
                                  args.report_dir, args.report_base_url)
    prompt_config = config.PromptConfig.load(args.prompt_file, args.audit_prompt_dir,
                                             args.models_json)
    api_key = args.openai_key_file.read_text().strip()
    ppq_api_key = args.ppq_key_file.read_text().strip()
    secret = args.webhook_secret_file.read_bytes().strip()
    forgejo_token = args.forgejo_token_file.read_text().strip()
    if not api_key or not ppq_api_key or not secret or not forgejo_token or not args.bot_login:
        parser.error("secret files must not be empty")
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.state_dir.chmod(0o700)
    jobs = JobStore(args.state_dir / "jobs.sqlite3")
    recovered = jobs.recover()
    ledger = spend.Ledger(args.state_dir / "spend.sqlite3",
                          review_limit_usd=args.review_budget_usd,
                          monthly_limit_usd=args.monthly_budget_usd)
    ppq_ledger = spend.Ledger(args.state_dir / "ppq-spend.sqlite3",
                             review_limit_usd=args.ppq_review_budget_usd)
    logging.info("review queue recovered_claims=%d workers=%d", recovered, args.workers)
    for index in range(args.workers):
        threading.Thread(target=worker, name=f"review-{index + 1}",
                         args=(jobs, args.state_dir, api_key, forgejo_token,
                               args.bot_login, bot_config, prompt_config, ledger,
                               args.routing_mode),
                         kwargs={"ppq_api_key": ppq_api_key, "ppq_ledger": ppq_ledger},
                         daemon=True).start()
    server = ThreadingHTTPServer((args.listen, args.port), make_handler(secret, jobs, bot_config))
    server.serve_forever()
