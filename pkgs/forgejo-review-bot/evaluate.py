#!/usr/bin/env python3
"""Replay Forgejo pull request reviews into private local JSON artifacts."""

import argparse
import json
import os
from pathlib import Path

import bot


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    path.chmod(0o700)
    return path


def write_private_json(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True, ensure_ascii=True)
        file.write("\n")
    path.chmod(0o600)


def read_secret(path, name):
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{name} file must not be empty")
    return value


def configure_models_json(path):
    models = json.loads(path.read_text(encoding="utf-8"))
    if (set(models) != set(bot.MODEL_NAMES)
            or any(not isinstance(model, str) or not model.startswith("gpt-")
                   for model in models.values())):
        raise ValueError("model override must name each review stage")
    bot.MODELS = models


def pull_request_details(token, number):
    pull = bot.forgejo_request(token, f"/pulls/{number}")
    base_ref = ((pull.get("base") or {}).get("ref") or "")
    api_head = ((pull.get("head") or {}).get("sha") or "")
    title = pull.get("title") or ""
    description = pull.get("body") or ""
    if (not isinstance(base_ref, str) or not bot.BRANCH.fullmatch(base_ref)
            or base_ref.startswith("-") or ".." in base_ref or "//" in base_ref
            or base_ref.endswith("/") or base_ref.endswith(".lock")):
        raise ValueError(f"PR {number} has an invalid base branch")
    if api_head and (not isinstance(api_head, str) or not bot.SHA.fullmatch(api_head)):
        raise ValueError(f"PR {number} has an invalid API head SHA")
    return {"base_ref": base_ref, "api_head": api_head,
            "title": title, "description": description}


def replay_pull_request(api_key, forgejo_token, checkout, output_dir, number):
    output_dir = private_dir(output_dir)
    details = pull_request_details(forgejo_token, number)
    expected_head = bot.current_head(number)
    if not bot.SHA.fullmatch(expected_head):
        raise ValueError(f"PR {number} has no mirrored pull head")
    base_sha, head_sha, review, skip = bot.collect_review(
        checkout, number, details["base_ref"], expected_head,
        details["title"], details["description"])
    debug = {"skip": skip} if skip else {}
    if skip:
        content = f"Skipped: {skip}"
    else:
        content = bot.review_with_independent_passes(
            api_key, review, checkout, number, debug)
    stage_outputs = debug.get("stage_outputs", {})
    final_comment = bot.review_body(base_sha, head_sha, content, debug)
    artifact = {
        "pr": number,
        "base_ref": details["base_ref"],
        "api_head": details["api_head"],
        "expected_head": expected_head,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "skipped": skip,
        "stage_outputs": stage_outputs,
        "verifier": stage_outputs.get("verifier"),
        "final_comment": final_comment,
        "debug": bot.review_trace(debug),
    }
    path = output_dir / f"pr-{number}-{head_sha[:12]}.json"
    write_private_json(path, artifact)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True,
                        help="Directory used for the mirror checkout")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Directory for private JSON replay artifacts")
    parser.add_argument("--origin", required=True,
                        help="Git remote URL used to fetch base branches and PR heads")
    parser.add_argument("--repository", required=True,
                        help="Forgejo repository full name, such as owner/repo")
    parser.add_argument("--forgejo-api", required=True,
                        help="Forgejo repository API URL, ending in /api/v1/repos/owner/repo")
    parser.add_argument("--repository-url",
                        help="Expected repository HTML URL")
    parser.add_argument("--comment-marker",
                        help="Hidden marker rendered in the local final comment")
    parser.add_argument("--openai-key-file", type=Path, required=True)
    parser.add_argument("--forgejo-token-file", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, default=bot.DEFAULT_PROMPT_FILE,
                        help="Markdown file containing the review prompt")
    parser.add_argument("--audit-prompt-dir", type=Path, default=bot.DEFAULT_AUDIT_DIR,
                        help="Directory containing focused audit prompts and models.json")
    parser.add_argument("--models-json", type=Path,
                        help="Optional replacement for audit-prompt-dir/models.json")
    parser.add_argument("prs", type=int, nargs="+",
                        help="Pull request numbers to replay")
    args = parser.parse_args(argv)

    bot.configure(args.origin, args.repository, args.forgejo_api,
                  args.repository_url, args.comment_marker)
    bot.configure_prompt(args.prompt_file)
    bot.configure_audit_prompts(args.audit_prompt_dir)
    if args.models_json is not None:
        configure_models_json(args.models_json)
    api_key = read_secret(args.openai_key_file, "OpenAI key")
    forgejo_token = read_secret(args.forgejo_token_file, "Forgejo token")
    output_dir = private_dir(args.output_dir)
    checkout = args.state_dir / "checkout"
    for number in args.prs:
        print(replay_pull_request(api_key, forgejo_token, checkout, output_dir, number))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
