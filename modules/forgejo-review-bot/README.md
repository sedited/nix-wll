# Forgejo review bot

This module runs `forgejo-review-bot`, a webhook receiver that posts a
first-pass review on Forgejo pull requests.

The bot fetches the base branch and PR head into its own state directory,
reviews the PR title, description, commit messages, and diff, and gives the
model read-only tools for path discovery, numbered head and merge-base file
reads, per-file diff reads, and literal code search. Large patches are replaced
by a changed-file list so the model can read relevant diffs on demand. It never
builds, runs, or tests pull request code.

Three worker threads review different PRs concurrently by default. Set
`services.forgejoReviewBot.workers` (or `--workers`) to change this limit.
Reviews of the same PR run serially; newer heads wait for the active attempt to
finish. Fetches and snapshot creation share a lock in one Git object store.
Each active review pins its own base and head refs until it finishes, and tools
read immutable blobs without a checked-out worktree. Model calls run outside
the Git lock. Spending reservations remain atomic across all workers, including
the shared monthly allowance.

The model can also search and read other public issues and pull requests in the
same repository, including a small sample of ordinary comments. The current PR
is excluded from these tools, so its comments cannot influence the review.
These reads use no Forgejo credentials and are limited to eight calls per
tool-enabled stage.
It can inspect up to 20 lines of merge-base blame and read a related ancestor
commit's message and file diff, with eight history calls per tool-enabled stage.
Tool output is capped.

A Luna router selects relevant specialists even for sensitive paths. Code rules
set a minimum risk tier and require the applicable domain checks; they do not
select every audit just because a change is sensitive. Incomplete input or
failed routing requests the full set. Routine changes use Luna.
Header changes follow their domain's sensitivity rules; the `.h` extension
alone does not force a full review. The router and reviewers can escalate
based on changed behavior and inspect relevant headers with their tools.
Sensitive changes receive parallel independent Sol 6.1 and GLM-5.3
adversarial reviews. GLM uses PPQ at `https://api.ppq.ai/v1/responses`.
Selected consensus, wallet, and P2P profiles add the same domain instructions
to both reviews. Concurrency covers C++ lifetime, locks and shared state; state
covers persistence and recovery. Public-contract and build audits cover exposed
behavior and portability respectively. These correctness checks run before
tests and design. Reviewers can escalate when inspection reveals sensitive
behavior. Independent reviewers never receive each other's candidates.

The tests audit starts with changed production behavior and existing coverage,
even when no tests changed. It then judges assertions, fixtures and runtime.
Design owns architecture, simplicity and applicable merge-base developer notes;
there is no separate developer-notes stage. Pure test changes can take the
routine route when the router establishes that deeper checks are unnecessary.

The design audit also weighs practical benefit against recurring contributor
work, maintenance, confusion and reviewer attention, even when the code is
correct. It compares the claimed outcome with what the mechanism guarantees.
Grounded design questions can be published when their factual premise is
verified and the answer would settle a material tradeoff.

The Luna design pass uses extra-high reasoning. Both adversarial reviews and
Luna verification of sensitive reviews use high reasoning. These stages each
have a 25,000-token allowance per response for reasoning and visible output
combined. This is initial headroom, not a measured optimum or a request for
longer findings. Concurrency uses medium reasoning with 8,000 tokens. Other
stages use low reasoning; ordinary verification retains 8,000 tokens.
Debug output records the settings and actual usage. OpenAI stages share their per-review spending ceiling. GLM has its own
USD 0.50 per-review ceiling and ledger.

Accepted design concerns appear under "Design and approach", after critical
bugs and before the remaining findings. Empty sections are omitted. The Luna
writing pass edits wording but cannot remove accepted findings or change their
classification. It retains its 6,000-token output limit; an incomplete or
invalid edit falls back to the verifier's wording without truncating findings.

Routine discovery stages get up to 12 tool inspections, standard stages 24,
and sensitive stages 48. The adversarial passes and verifier each get
48. With an allowance of N inspections, the model can make up to N tool calls
across at most N + 1 responses, leaving a final response without tools. The
router and writing pass have no tools. The final response and all inspections
remain subject to the spending ceiling. At the inspection limit, further reads
are refused. The model returns supported findings and names material unanswered
evidence. Debug output records the limit and which requests were skipped.

Discovery stages return structured candidates. The verifier accounts for each
candidate as published, dropped or unresolved, and the writing pass receives
only accepted findings. Python checks finding IDs and preserves verified
locations and severity. A bad verifier finding is withheld without discarding
other valid findings, and debug output identifies the validation error. Partial
coverage alone does not discard findings. Broken candidate accounting or a
failed verifier still prevents publication of unverified findings. If only
editing fails, the verified wording is used.

The default OpenAI per-review spending ceiling is USD 1.00. It is an allowance for
complex reviews, not a target spend. Lightweight routing still skips both adversarial passes and
irrelevant specialist stages. Before each API request the bot reserves a
conservative input/output cost estimate, holding some allowance for verification
based on its actual model, input, response limit and bounded evidence headroom.
The pipeline refreshes that protection as candidates accumulate or risk changes.
Tool-enabled requests also preserve room for a final response. When another
inspection cannot fit, the model is asked to finish from available evidence if
that final request fits. This is bounded headroom, not a guarantee of complete
verification: evidence growth and shared monthly spending can still exhaust it.
Editing uses remaining allowance and can fall back to the verifier's wording.
Reported usage settles the reservation;
missing usage or an ambiguous transport failure retains conservative estimated
charges. These estimates depend on configured model prices and API token
accounting, so they are not an invoice guarantee. Unknown model prices prevent
requests.
The optional monthly allowance is unset by default. The ledger retains monthly
totals and includes failed requests whose charges are uncertain.

Jobs are persisted before the webhook receives a 202 response. Pending PR
updates are coalesced, stale work stops between model requests, and completed
review payloads are saved before publication. Publication retries reuse that
payload. Transient failures have bounded retries; pending claims recover when
the service restarts. The bot retains one editable comment per PR.

The collapsed public debug section includes a finding-attribution table with
the final title, discovering agents and their models, verifier and editor. Merged candidates
retain all their source agents; a finding first discovered during verification
is credited to the verifier. Editing never earns discovery credit. Per-stage
summaries show candidate dispositions, accepted findings found solely or jointly,
cost estimates and incomplete usage. Joint credit is not evidence that each
agent was necessary, and verifier acceptance is not a human quality label.
Adversarial debug records its selected profiles without attributing findings
to an individual profile within the shared call.

The collapsed public debug section intentionally includes clipped preliminary
responses during development, including findings the verifier rejected. The
main comment contains verified findings. Private traces preserve full responses
and per-request usage, including failures.

To force a fresh review of the same head, send a signed synthetic pull request
webhook with `"review_bot_force": true`. Normal Forgejo webhooks omit this field.
A forced review receives a new allowance. Changing prompts does not
automatically rerun previously reviewed heads.

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
    ppqKeyFile = "/run/secrets/forgejo-review-bot/ppq-key";
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
- `services.forgejoReviewBot.ppqKeyFile`: file containing the PPQ API key.
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

## Cost and routing options

- `reviewBudgetUsd = 1.00`: per-review spending ceiling. It spans retries and
  is not a target spend.
- `ppqReviewBudgetUsd = 0.50`: separate ceiling for the GLM pass, using
  `ppq-spend.sqlite3`. GLM preserves room for its own final response.
- `monthlyBudgetUsd = null`: optional OpenAI ceiling on the month's recorded charges
  and outstanding reservations.
- `routingMode = "enabled"`: apply conservative routing.
- `routingMode = "shadow"`: record the proposed route while requesting every
  audit. This costs more and still respects the same allowance.
- `routingMode = "full"`: request every audit without calling the router.
- `modelsJson = null`: optional per-stage replacement for the model map.
  The map must include router, independent, adversarial, adversarial_glm, concurrency, state,
  public_contract, tests, design, build, verifier and collator.
  Prices must also be supported by the bot's ledger.

Custom prompt directories must include `consensus.md`, `wallet.md`, `p2p.md`,
`concurrency.md` and `build.md`; remove `developer_notes` from custom model maps.
Update custom prompt/model configurations together. Unsupported model names
are rejected when configuration loads. The existing verifier model override
can be used for controlled Sol comparisons while the default stays on Luna.

Enabled routing is the default to control spend. Routing rules and contracts
are covered by local tests; model quality and recall still need evaluation on
representative frozen PRs. No production-quality claim follows from those
unit tests.

The service writes a monthly spend summary to its journal after each job.
Read the ledger directly without an API key:

```sh
forgejo-review-bot-evaluate spend --state-dir /var/lib/forgejo-review-bot
```

Use the service account or another account permitted to read its private state.
The summary includes outstanding reservations in estimated_total_usd;
reserved_total_usd is the portion whose charge has not been settled.
usage_complete is false when any reported count or response is missing.

## Frozen prompt and routing experiments

The package installs `forgejo-review-bot-evaluate` for capture, replay, spend
inspection and offline summaries.
Capture performs Forgejo/Git reads but makes no model calls:

```sh
forgejo-review-bot-evaluate capture \
  --state-dir ./evaluation-state --output-dir ./cases \
  --origin https://git.example.org/owner/repo.git \
  --repository owner/repo \
  --forgejo-api https://git.example.org/api/v1/repos/owner/repo \
  --forgejo-token-file /run/secrets/forgejo-token \
  123
```

Capture retains complete Git objects in a dedicated evaluation checkout. The
initial download can be large. Keep that checkout with the manifests.
Replay reads frozen title/body, commit IDs, Git objects and prompt/model
configuration; discussion tools and lazy Git downloads are disabled:

```sh
forgejo-review-bot-evaluate run \
  --state-dir ./evaluation-state --output-dir ./results \
  --openai-key-file /run/secrets/openai-key \
  --review-budget-usd 1.00 \
  ./cases/case-123-*.json
```

Run supports `--prompt-file`, `--audit-prompt-dir`, `--models-json` and
`--routing-mode` overrides. Each run gets a distinct private JSON artifact
with effective configuration identity, raw stage results, usage, final comment
and any failure. Optional `--labels-json` maps case IDs to expected findings;
these labels are saved for comparison and never sent to reviewers.

Compare useful findings and missed known findings alongside cost, incomplete
coverage and wall time. An empty review is not proof of a good route. Shadow
routing is useful for a bounded comparison before changing sensitive path
rules or removing a specialist.

See [the evaluation guide](evaluation.md) for offline summaries and a human
scorecard for comparing coverage and cost. Summaries use recorded attribution;
they do not automatically decide whether a finding is correct or useful.

The service and evaluation run command require `--ppq-key-file`. Compare
`adversarial` and `adversarial_glm` in debug stage metrics for sole/shared
accepted findings and published, dropped or unresolved candidates. OpenAI has a USD 1.00 ceiling and GLM has a separate USD 0.50 ceiling; incomplete passes
are not evidence of model quality.
PPQ GLM rates use the model catalog input/output prices without cache discounts.
See [PPQ integration guide](https://ppq.ai/llms.txt).
