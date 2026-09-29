"""Capture Forgejo PR cases and replay them from frozen local inputs."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import config, forgejo, pipeline, repository, spend


SCHEMA_VERSION = 1


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    path.chmod(0o700)
    return path


def write_private_json(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True, ensure_ascii=True)
        file.write("\n")


def read_secret(path, name):
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{name} file must not be empty")
    return value


def digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode()
    return hashlib.sha256(data).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def source_code_sha256():
    sources = sorted(Path(__file__).resolve().parent.glob("*.py"))
    checksum = hashlib.sha256()
    for path in sources:
        checksum.update(path.name.encode())
        checksum.update(b"\x00")
        checksum.update(path.read_bytes())
        checksum.update(b"\x00")
    return checksum.hexdigest()


@contextmanager
def offline_git():
    previous = os.environ.get("GIT_NO_LAZY_FETCH")
    os.environ["GIT_NO_LAZY_FETCH"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GIT_NO_LAZY_FETCH", None)
        else:
            os.environ["GIT_NO_LAZY_FETCH"] = previous


def verify_local_objects(checkout, base_sha, head_sha):
    """Reject partial clones that would fetch Git objects during replay."""
    if not checkout.is_absolute():
        raise ValueError("Frozen checkout path must be absolute")
    if not repository.SHA.fullmatch(base_sha) or not repository.SHA.fullmatch(head_sha):
        raise ValueError("Frozen Git object IDs are invalid")
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-list", "--objects", "--missing=print",
         base_sha, head_sha], capture_output=True, text=True, timeout=300,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
    )
    if result.returncode or any(line.startswith("?") for line in result.stdout.splitlines()):
        raise ValueError("Frozen Git objects are unavailable in the local checkout")


def verify_case_pins(checkout, manifest):
    case_id = manifest["case_id"]
    for role in ("base", "head"):
        pin = manifest[f"{role}_pin"]
        if pin != f"refs/review-cases/{case_id}/{role}":
            raise ValueError("Frozen case ref is invalid")
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--verify", pin],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
        )
        if result.returncode or result.stdout.strip() != manifest[f"{role}_sha"]:
            raise ValueError("Frozen case ref no longer pins its Git object")


def pull_request_details(bot_config, token, number):
    pull = forgejo.forgejo_request(bot_config, token, f"/pulls/{number}")
    base_ref = ((pull.get("base") or {}).get("ref") or "")
    api_head = ((pull.get("head") or {}).get("sha") or "")
    title = pull.get("title") or ""
    description = pull.get("body") or ""
    if (not isinstance(base_ref, str) or not repository.BRANCH.fullmatch(base_ref)
            or base_ref.startswith("-") or ".." in base_ref or "//" in base_ref
            or base_ref.endswith("/") or base_ref.endswith(".lock")):
        raise ValueError(f"PR {number} has an invalid base branch")
    if api_head and (not isinstance(api_head, str) or not repository.SHA.fullmatch(api_head)):
        raise ValueError(f"PR {number} has an invalid API head SHA")
    if not isinstance(title, str) or not isinstance(description, str):
        raise ValueError(f"PR {number} has invalid title or description")
    return {"base_ref": base_ref, "api_head": api_head,
            "title": title, "description": description}


def capture_case(forgejo_token, checkout, output_dir, number, bot_config, prompt_config):
    """Make one private manifest with complete local Git objects."""
    output_dir = private_dir(output_dir)
    checkout = checkout.resolve()
    details = pull_request_details(bot_config, forgejo_token, number)
    expected_head = repository.current_head(bot_config, number)
    if not repository.SHA.fullmatch(expected_head):
        raise ValueError(f"PR {number} has no mirrored pull head")
    base_sha, head_sha, review, skip = repository.collect_review(
        checkout, number, details["base_ref"], expected_head,
        details["title"], details["description"], bot_config)
    if skip:
        raise ValueError(f"PR {number} cannot be frozen: {skip}")
    case_id = uuid.uuid4().hex
    base_pin = f"refs/review-cases/{case_id}/base"
    head_pin = f"refs/review-cases/{case_id}/head"
    try:
        repository.git(checkout, "update-ref", base_pin, base_sha)
        repository.git(checkout, "update-ref", head_pin, head_sha)
        # collect_review uses a partial fetch. Request the captured object IDs,
        # so a force push cannot quietly replace the case with a newer head.
        try:
            repository.git(checkout, "fetch", "--refetch", "--no-tags",
                           "--no-filter", "origin", base_sha, head_sha)
        except subprocess.CalledProcessError as exc:
            raise ValueError("Captured Git objects could not be fetched after PR refs moved") from exc
        with offline_git():
            verify_local_objects(checkout, base_sha, head_sha)
            snapshot = repository.snapshot_repository(checkout, base_sha, head_sha)
        frozen_config = {"bot": asdict(bot_config),
                         "prompts": asdict(prompt_config)}
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "case_id": case_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "pr": number,
            "checkout": str(checkout),
            "base_ref": details["base_ref"],
            "api_head": details["api_head"],
            "expected_head": expected_head,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "base_pin": base_pin,
            "head_pin": head_pin,
            "merge_base": snapshot.merge_base,
            "title": details["title"],
            "description": details["description"],
            "review_input": review,
            "review_input_sha256": hashlib.sha256(review.encode()).hexdigest(),
            "config": frozen_config,
            "config_sha256": digest(frozen_config),
            "source_code_sha256": source_code_sha256(),
        }
        manifest["manifest_sha256"] = digest(manifest)
        path = output_dir / f"case-{number}-{head_sha[:12]}-{timestamp()}-{case_id}.json"
        write_private_json(path, manifest)
        return path
    except Exception:
        for pin in (base_pin, head_pin):
            subprocess.run(["git", "-C", str(checkout), "update-ref", "-d", pin],
                           capture_output=True, timeout=30)
        raise


def prompt_override(frozen, prompt_file=None, audit_dir=None, models_json=None):
    instructions = (config.load_prompt_file(prompt_file) if prompt_file
                    else frozen.instructions)
    prompts = dict(frozen.audit_prompts)
    if audit_dir:
        prompts = {name: config.load_prompt_file(audit_dir / f"{name}.md")
                   for name in ("common", "router", "adversarial", *config.AUDIT_NAMES,
                                "verifier", "collator")}
        if any(not prompt.strip() for prompt in prompts.values()):
            raise ValueError("audit prompt files must not be empty")
    models = (config.validate_models(json.loads(models_json.read_text(encoding="utf-8")))
              if models_json else dict(frozen.models))
    return config.PromptConfig(instructions, prompts, models)


def run_case(manifest_path, api_key, output_dir, ledger, routing_mode="enabled",
             prompt_file=None, audit_dir=None, models_json=None, labels=None):
    """Replay a captured case without Forgejo or remote Git reads."""
    output_dir = private_dir(output_dir)
    debug = {}
    artifact = {"manifest": str(manifest_path.resolve()), "status": "failed",
                "started_at": datetime.now(timezone.utc).isoformat()}
    config_hash = "unknown"
    case_id = "unknown"
    budget = None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported frozen case schema")
        if digest({key: value for key, value in manifest.items()
                   if key != "manifest_sha256"}) != manifest.get("manifest_sha256"):
            raise ValueError("Frozen case hash mismatch")
        if not re.fullmatch(r"[0-9a-f]{32}", manifest["case_id"]):
            raise ValueError("Frozen case ID is invalid")
        case_id = manifest["case_id"]
        frozen_config = manifest["config"]
        if digest(frozen_config) != manifest["config_sha256"]:
            raise ValueError("Frozen configuration hash mismatch")
        bot_config = config.BotConfig(**frozen_config["bot"])
        config.validate_models(frozen_config["prompts"]["models"])
        frozen_prompts = config.PromptConfig(**frozen_config["prompts"])
        prompt_config = prompt_override(frozen_prompts, prompt_file, audit_dir, models_json)
        code_hash = source_code_sha256()
        review_limit = getattr(ledger, "review_limit_micros", None)
        monthly_limit = getattr(ledger, "monthly_limit_micros", None)
        effective_config = {
            "bot": asdict(bot_config), "prompts": asdict(prompt_config),
            "routing_mode": routing_mode,
            "review_budget_usd": None if review_limit is None else review_limit / 1_000_000,
            "monthly_budget_usd": None if monthly_limit is None else monthly_limit / 1_000_000,
            "source_code_sha256": code_hash,
        }
        config_hash = digest(effective_config)
        artifact["effective_config"] = effective_config
        artifact["source_code_sha256"] = code_hash
        artifact["capture_code_sha256"] = manifest["source_code_sha256"]
        artifact["code_changed_since_capture"] = code_hash != manifest["source_code_sha256"]
        checkout = Path(manifest["checkout"])
        base_sha, head_sha = manifest["base_sha"], manifest["head_sha"]
        review = manifest["review_input"]
        if hashlib.sha256(review.encode()).hexdigest() != manifest["review_input_sha256"]:
            raise ValueError("Frozen review input hash mismatch")
        with offline_git():
            verify_local_objects(checkout, base_sha, head_sha)
            verify_case_pins(checkout, manifest)
            snapshot = repository.snapshot_repository(checkout, base_sha, head_sha)
            if snapshot.merge_base != manifest["merge_base"]:
                raise ValueError("Frozen merge base mismatch")
            review_id = f"eval:{case_id}:{uuid.uuid4().hex}"
            budget = spend.RequestBudget(ledger, review_id)
            artifact["review_id"] = review_id
            content = pipeline.review_with_independent_passes(
                api_key, review, snapshot, bot_config, prompt_config,
                manifest["pr"], debug, budget=budget,
                routing_mode=routing_mode, allow_discussions=False)
        artifact.update({
            "status": "completed", "case_id": case_id, "pr": manifest["pr"],
            "base_sha": base_sha, "head_sha": head_sha,
            "merge_base": snapshot.merge_base,
            "review_input_sha256": manifest["review_input_sha256"],
            "final_comment": forgejo.review_body(bot_config, prompt_config,
                                                   base_sha, head_sha, content, debug),
            "stage_outputs": debug.get("stage_outputs", {}),
        })
    except Exception as exc:
        artifact["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        artifact["finished_at"] = datetime.now(timezone.utc).isoformat()
        artifact["effective_config_sha256"] = config_hash
        artifact["raw_debug"] = debug
        if budget is not None:
            try:
                artifact["budget"] = budget.summary()
            except Exception as exc:
                artifact["budget"] = debug.get("budget")
                artifact["budget_error"] = type(exc).__name__
        if isinstance(labels, dict) and case_id in labels:
            artifact["expected_findings"] = labels[case_id]
        path = output_dir / f"run-{case_id}-{timestamp()}-{uuid.uuid4().hex}-{config_hash[:12]}.json"
        write_private_json(path, artifact)
    return path


def add_config_args(parser):
    parser.add_argument("--origin", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--forgejo-api", required=True)
    parser.add_argument("--repository-url")
    parser.add_argument("--comment-marker")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture", help="freeze PR inputs and Git objects")
    capture.add_argument("--state-dir", type=Path, required=True)
    capture.add_argument("--output-dir", type=Path, required=True)
    add_config_args(capture)
    capture.add_argument("--forgejo-token-file", type=Path, required=True)
    capture.add_argument("--prompt-file", type=Path, default=config.DEFAULT_PROMPT_FILE)
    capture.add_argument("--audit-prompt-dir", type=Path, default=config.DEFAULT_AUDIT_DIR)
    capture.add_argument("--models-json", type=Path)
    capture.add_argument("prs", type=int, nargs="+")

    run = commands.add_parser("run", help="replay frozen cases without Forgejo")
    run.add_argument("--state-dir", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--openai-key-file", type=Path, required=True)
    run.add_argument("--prompt-file", type=Path)
    run.add_argument("--audit-prompt-dir", type=Path)
    run.add_argument("--models-json", type=Path)
    run.add_argument("--routing-mode", choices=("enabled", "shadow", "full"),
                     default="enabled")
    run.add_argument("--review-budget-usd", type=float, default=1.00)
    run.add_argument("--monthly-budget-usd", type=float)
    run.add_argument("--labels-json", type=Path,
                     help="Optional local labels keyed by case ID; never sent to models")
    run.add_argument("manifests", type=Path, nargs="+")

    summary = commands.add_parser("spend", help="show current month spend")
    summary.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "capture":
        bot_config = config.BotConfig(args.origin, args.repository, args.forgejo_api,
                                      args.repository_url, args.comment_marker)
        prompt_config = config.PromptConfig.load(args.prompt_file, args.audit_prompt_dir,
                                                 args.models_json)
        token = read_secret(args.forgejo_token_file, "Forgejo token")
        checkout = args.state_dir / "evaluation-checkout"
        for number in args.prs:
            print(capture_case(token, checkout, args.output_dir, number,
                               bot_config, prompt_config))
        return 0
    if args.command == "spend":
        path = args.state_dir / "spend.sqlite3"
        if not path.exists():
            parser.error(f"No spend ledger at {path}")
        print(json.dumps(spend.Ledger(path).summary(), sort_keys=True))
        return 0

    api_key = read_secret(args.openai_key_file, "OpenAI key")
    ledger = spend.Ledger(args.state_dir / "spend.sqlite3",
                          review_limit_usd=args.review_budget_usd,
                          monthly_limit_usd=args.monthly_budget_usd)
    labels = (json.loads(args.labels_json.read_text(encoding="utf-8"))
              if args.labels_json else {})
    result = 0
    for manifest in args.manifests:
        path = run_case(manifest, api_key, args.output_dir, ledger,
                        args.routing_mode, args.prompt_file, args.audit_prompt_dir,
                        args.models_json, labels)
        print(path)
        if json.loads(path.read_text(encoding="utf-8"))["status"] != "completed":
            result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
