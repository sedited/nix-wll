# Forgejo review bot

This module runs `forgejo-review-bot`, a webhook receiver that posts a
first-pass review on Forgejo pull requests.

The bot fetches the base branch and PR head into its own state directory,
then runs each review stage through `codex exec` with the model selected in
`auditPromptDir/models.json`. Codex can inspect the checkout with its normal
tools under the service's systemd restrictions. The review prompts prohibit
using discussion on the current PR; this is an instruction to Codex rather
than a restriction imposed by a custom discussion tool.

Five focused reviews check state, public contracts, test evidence, developer
notes, and design. They run alongside independent and adversarial reviews. A
verifier checks their candidate findings, and a collator formats the accepted
findings for the single bot comment. Text-only stages run outside the checkout.
The bot gives Codex its API key as `CODEX_API_KEY` and excludes it from shell
commands. Shell tools run as the bot service user and can read its secret
files. The collapsed comment debug section includes stage status, usage,
and estimated cost without full candidate text.

`auditPromptDir` contains the stage prompts and `models.json`. Each stage's
model can be changed separately in that file. `codexPackage` must point to a
Codex CLI package; the host can use the latest pinned package from
`numtide/llm-agents.nix`. Pass `inputs` through the host's
`specialArgs` when using the example below.

The bot writes at most one comment per pull request. New reviews edit the
existing bot-owned comment when the content changes, and leave it untouched
when it is already current.

To rerun a review after changing the prompt or tools without a new PR commit,
send a signed synthetic pull request webhook with `"review_bot_force": true`.
Normal Forgejo webhooks leave this field unset.

## Minimal configuration

```nix
{ inputs, pkgs, ... }:
{
  imports = [
    inputs.will-nix.nixosModules.forgejo-review-bot
  ];

  services.forgejoReviewBot = {
    enable = true;
    codexPackage = inputs.llm-agents.packages.${pkgs.stdenv.hostPlatform.system}.codex;
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

- `services.forgejoReviewBot.codexPackage`: Codex CLI package in the service
  PATH, for example from a pinned `numtide/llm-agents.nix` input.
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
- `services.forgejoReviewBot.auditPromptDir =
  "${services.forgejoReviewBot.package}/share/forgejo-review-bot/audits"`

Set `repositoryUrl` only when the HTML URL in Forgejo webhook payloads cannot
be derived from `forgejoApi`.
