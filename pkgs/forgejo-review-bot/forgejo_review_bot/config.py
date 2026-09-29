"""Validated bot configuration and review prompts."""

import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompt.md"
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent / "audits"
AUDIT_NAMES = ("state", "public_contract", "tests", "developer_notes", "design")
MODEL_NAMES = ("router", "independent", "adversarial", *AUDIT_NAMES, "verifier", "collator")


def load_prompt_file(path):
    return path.read_text(encoding="utf-8").removesuffix("\n")


def default_repository_url(forgejo_api):
    api_marker = "/api/v1/repos/"
    forgejo_api = forgejo_api.rstrip("/")
    if api_marker not in forgejo_api:
        return None
    base_url, repository = forgejo_api.split(api_marker, 1)
    return f"{base_url.rstrip('/')}/{repository}".rstrip("/")


def default_comment_marker(repository):
    return f"<!-- forgejo-review-bot:{repository} -->"


@dataclass(frozen=True)
class BotConfig:
    origin: str
    repository: str
    forgejo_api: str
    repository_url: str | None = None
    comment_marker: str | None = None

    def __post_init__(self):
        if not self.origin or not self.repository or not self.forgejo_api:
            raise ValueError("origin, repository, and forgejo_api are required")
        object.__setattr__(self, "forgejo_api", self.forgejo_api.rstrip("/"))
        url = self.repository_url or default_repository_url(self.forgejo_api)
        if not url:
            raise ValueError("repository_url is required when forgejo_api is not a repository API URL")
        object.__setattr__(self, "repository_url", url)
        object.__setattr__(self, "comment_marker",
                           self.comment_marker or default_comment_marker(self.repository))


def validate_models(models):
    if (set(models) != set(MODEL_NAMES)
            or any(not isinstance(model, str) or not model.startswith("gpt-")
                   for model in models.values())):
        raise ValueError("model config must name each review stage")
    return models


@dataclass(frozen=True)
class PromptConfig:
    instructions: str
    audit_prompts: dict[str, str]
    models: dict[str, str]

    @classmethod
    def load(cls, prompt_file=DEFAULT_PROMPT_FILE, audit_dir=DEFAULT_AUDIT_DIR,
             models_json=None):
        prompts = {name: load_prompt_file(audit_dir / f"{name}.md")
                   for name in ("common", "router", "adversarial", *AUDIT_NAMES,
                                "verifier", "collator")}
        if any(not prompt.strip() for prompt in prompts.values()):
            raise ValueError("audit prompt files must not be empty")
        path = models_json or audit_dir / "models.json"
        models = validate_models(json.loads(path.read_text(encoding="utf-8")))
        return cls(load_prompt_file(prompt_file), prompts, models)
