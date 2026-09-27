# Forgejo review bot

This module runs `forgejo-review-bot`, a webhook receiver that posts a
first-pass review on Forgejo pull requests.

The bot fetches the base branch and PR head into its own state directory,
reviews the PR title, description, commit messages, and diff, and gives the
model two read-only repository tools: numbered file reads and literal code
search. It never builds, runs, or tests pull request code.

The bot writes at most one comment per pull request. New reviews edit the
existing bot-owned comment when the content changes, and leave it untouched
when it is already current.

## Minimal configuration

```nix
{
  imports = [
    inputs.will-nix.nixosModules.forgejo-review-bot
  ];

  services.forgejoReviewBot = {
    enable = true;
    origin = "https://git.example.org/owner/repo.git";
    repository = "owner/repo";
    forgejoApi = "https://git.example.org/api/v1/repos/owner/repo";
    botLogin = "review-bot";

    openaiKeyFile = "/run/secrets/forgejo-review-bot/openai-key";
    webhookSecretFile = "/run/secrets/forgejo-review-bot/webhook-secret";
    forgejoTokenFile = "/run/secrets/forgejo-review-bot/forgejo-token";
  };
}
```

Set the Forgejo webhook to `POST` JSON to
`https://YOUR_HOST/webhooks/forgejo`. Configure a long random secret in Forgejo
and store the same value in `webhookSecretFile`. Select custom pull request
events.

## Options

Required deployment options:

- `services.forgejoReviewBot.origin`: Git remote URL used for `git fetch` and
  stale-head checks.
- `services.forgejoReviewBot.repository`: Forgejo repository full name, for
  example `owner/repo`.
- `services.forgejoReviewBot.forgejoApi`: Forgejo repository API URL ending in
  `/api/v1/repos/owner/repo`.
- `services.forgejoReviewBot.openaiKeyFile`: file containing the OpenAI API
  key.
- `services.forgejoReviewBot.webhookSecretFile`: file containing the Forgejo
  webhook secret.
- `services.forgejoReviewBot.forgejoTokenFile`: file containing the Forgejo API
  token.
- `services.forgejoReviewBot.botLogin`: Forgejo login that owns the review
  comment.

Useful defaults:

- `services.forgejoReviewBot.listenAddress = "127.0.0.1"`
- `services.forgejoReviewBot.port = 8765`
- `services.forgejoReviewBot.stateDir = "/var/lib/forgejo-review-bot"`
- `services.forgejoReviewBot.commentMarker = null`, which uses
  `<!-- forgejo-review-bot:${repository} -->`
- `services.forgejoReviewBot.promptFile =
  "${services.forgejoReviewBot.package}/share/forgejo-review-bot/prompt.md"`

Set `repositoryUrl` only when the HTML URL in Forgejo webhook payloads cannot
be derived from `forgejoApi`.
