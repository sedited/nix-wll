# ralph

This module runs `ralph`, a webhook receiver that posts a
first-pass review on Forgejo pull requests.

ralph fetches the base branch and PR head into its own state directory,
reviews the PR title, description, commit messages, and diff, and gives the
model read-only tools for path discovery, numbered head and merge-base file
reads, per-file diff reads, and literal code search. Large patches are replaced
by a changed-file list so the model can read relevant diffs on demand. It never
builds, runs, or tests pull request code.

Three worker threads review different PRs concurrently by default. Set
`services.ralph.workers` (or `--workers`) to change this limit.
Reviews of the same PR run serially; newer heads wait for the active attempt to
finish. Fetches and snapshot creation share a lock in one Git object store.
Each active review pins its own base and head refs until it finishes, and tools
read immutable blobs without a checked-out worktree. Model calls run outside
the Git lock. Spending reservations remain atomic across all workers, including
the shared monthly allowance.

The model can also search and read other public issues and pull requests in the
same repository, including a small sample of ordinary comments. The current PR
is excluded from code discovery. After routing and before independent code
discovery, a separate Luna archaeologist receives the full review input, base
and head code tools, current discussion, original upstream reviews, previous
attempts and alternatives. Its concept candidate goes to the verifier with the
pooled code candidates.
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
behavior. Independent reviewers never receive each other's candidates. They also
never receive the archaeology output or current PR discussion.

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

The archaeologist returns a source-backed concept candidate about the problem,
baseline, delivered benefit, history and credible alternatives. It can inspect
base and head code to check what the PR delivers, but it does not publish code
findings or assign severity. Its `assessment` is one of `worth_pursuing`,
`needs_motivation`, `rework_approach` or `not_worth_pursuing`; its advisory
`proposed_review` is `continue`, `would_stop` or `undetermined`, with a
`review_reason`. Negative, failed or incomplete archaeology never skips any
selected code stage. The verifier checks decisive claims and technical
assumptions before publishing supported, actionable objections to the PR's
premise or approach, including when no code findings survive. It also records
its own final review recommendation. The public "Concept and approach" section
is a short paragraph and may include at most one alternative demonstrated to be
better under the PR's actual requirements, accounting for its costs. It omits
concerns already covered by design findings. Favorable assessments and rejected
or inconclusive alternatives stay in the detailed trace; comments have no
separate alternatives list. Unsupported assessments are withheld; editor
failure retains the verified wording.

After a valid concept candidate, blind alternatives are enabled by default. This
uses the existing archaeologist model with base-only code tools and neutral
problem, goal and baseline inputs. Its result goes to the verifier, not to
discovery, and code review still runs every selected stage.

The Luna design pass uses extra-high reasoning. Both adversarial reviews and
Luna verification of sensitive reviews use high reasoning. These stages each
have a 25,000-token allowance per response for reasoning and visible output
combined. This is initial headroom, not a measured optimum or a request for
longer findings. Concurrency uses medium reasoning with 8,000 tokens. Other
stages use low reasoning; ordinary verification retains 8,000 tokens.
Debug output records the settings and actual usage. OpenAI stages share their
per-review spending ceiling. GLM has its own USD 0.50 per-review ceiling and
ledger.

Accepted design concerns appear under "Design and approach", after critical
bugs and before the remaining findings. Empty sections are omitted. The Luna
writing pass edits wording but cannot remove accepted findings or change their
classification. It retains its 6,000-token output limit; an incomplete or
invalid edit falls back to the verifier's wording without truncating findings.

Routine discovery stages get up to 12 tool inspections, standard stages 24,
and sensitive stages 48. The adversarial passes and verifier each get
48. Archaeology gets twelve total inspections, including hosted web actions,
with at most two hosted actions per response. Search charges are included in
the OpenAI spending allowance. The concept stage spends from the same capped
review allowance as the OpenAI code stages, does not estimate savings and never
stops later code stages. With an allowance of N inspections, the model can make
up to N tool calls across at most N + 1 responses, leaving a final response
without tools. The router and writing pass have no tools. The final response and
all inspections remain subject to the spending ceiling. At the inspection limit,
further reads are refused. The model returns supported findings and names
material unanswered evidence. Debug output records the limit and which requests
were skipped.

Discovery stages return structured candidates. The verifier accounts for each
candidate as published, dropped or unresolved, and the writing pass receives
accepted findings and a separately verified conceptual assessment. Python checks
finding IDs and preserves verified
locations and severity. A bad verifier finding is withheld without discarding
other valid findings, and debug output identifies the validation error. Partial
coverage alone does not discard findings. Broken candidate accounting or a
failed verifier still prevents publication of unverified findings. If only
editing fails, the verified wording is used.

The default OpenAI per-review spending ceiling is USD 1.00. It is an allowance for
complex reviews, not a target spend. Lightweight routing still skips both adversarial passes and
irrelevant specialist stages. Before each API request ralph reserves a
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
the service restarts. ralph retains one editable comment per PR.

The public PR comment contains the marker, base and head commits, and verified
review text. When full reports are configured, it also links to the report. It
does not include a collapsed debug section, finding-attribution table, or
clipped preliminary stage replies.

Finding attribution is still recorded with the final title, discovering agents
and their models, verifier and editor. Merged candidates retain all their
source agents; a finding first discovered during verification is credited to
the verifier. Editing never earns discovery credit. Per-stage summaries record
candidate dispositions, accepted findings found solely or jointly, concept
recommendations, cost estimates and incomplete usage. Joint credit is not
evidence that each agent was necessary, and verifier acceptance is not a human
quality label.
Adversarial records include selected profiles without attributing findings to
an individual profile within the shared call. Private traces preserve full
responses and per-request usage, including failures.

For full browser reports, set `services.ralph.reportDir` to a
separate public directory and `services.ralph.reportBaseUrl` to
the HTTP URL serving it. The module creates the directory with mode `0755`;
ralph writes readable static HTML and JSON files before publishing a link
in the review comment. Configure your web server to serve this directory
without directory browsing. Do not serve the private `stateDir` or its
`review-traces` directory.

Reports include full preliminary agent replies, coverage limitations, and
review details. Preliminary candidates remain unverified; the main review
contains verified findings. Public reports exclude private request payloads
and configuration. Each review run has a distinct URL, and publication retries
reuse it. If a report cannot be written, the review is published without a
report link. Existing comments and private traces are not backfilled.

When reports are configured, a timer also refreshes `reportDir/stats/index.html`
and its public JSON download every five minutes. The dashboard reads durable
review jobs and both spend ledgers without model calls or credentials. It ranks
agents and models by verifier-accepted findings, separates sole and shared
contributions, and shows execution opportunities, candidate dispositions,
coverage, routing, token usage, costs, and review details. Shared findings earn
credit for each contributing agent, so leaderboard counts overlap. Concept
assessments have separate saved/published counts and do not inflate finding
counts. Candidate and verifier `would_stop` counts are shown with downstream
findings and costs so a human can judge false proposed stops and useful findings
that appeared after a proposed stop. Archaeology costs and execution remain
visible outside the finding leaderboard.

Distinct review jobs and paid request attempts are counted separately. Retries
increase request counts and ledger spend; they do not duplicate the final
findings. Known costs and unsettled reservations are shown separately, including
failed requests. Verifier acceptance is not human validation or a recall
measurement, and summed model time is not elapsed review time.

The page is reachable under the report URL's `stats/` subdirectory. Hosts may
also serve `reportDir/stats` at a shorter URL such as `/stats/`. To refresh it
manually without waiting for the timer:

```sh
systemctl start ralph-stats.service
```

To force a fresh review of the same head, send a signed synthetic pull request
webhook with `"ralph_force": true`. Normal Forgejo webhooks omit this field.
For a historical review, also supply `pull_request.base.sha` with the recorded
base commit. Merged PRs use the merge commit's first parent; closed, unmerged
PRs use their recorded base SHA. Only forced requests honor this field. Open
PRs omit it so the worker fetches the current target branch. Every review uses
`merge-base(base, head)..head` for its patch and file tools. Historical reviews
remain retrospective: discussion and web research are not time-filtered.
A forced review receives a new allowance. Changing prompts does not
automatically rerun previously reviewed heads.

## Review status images

The HTTP listener serves `GET /assets/<owner>/<repo>/<pr>.png` for the
configured repository. It returns a 16 by 16 PNG dot, green when the latest
queued review has completed publication with verified findings or a published
conceptual concern. Otherwise it returns grey, including unknown PRs, pending
or failed reviews, and reviews with nothing to say. A new queued generation
resets the dot to grey until publication completes. Existing saved results
work immediately; no model calls or backfill are needed.

Forward `/assets/` to Ralph's listener alongside `/webhooks/forgejo`, preserving
the full path. This endpoint works independently of `reportDir`; the proxy
does not need access to private state. Responses use `Cache-Control: no-store`,
though external image proxies may apply their own caching.

For example, with `repository = "bitcoin/bitcoin"` and a host proxying the
listener at `ralph.example.org`:

```html
<img src="https://ralph.example.org/assets/bitcoin/bitcoin/123456.png"
     width="16" height="16" alt="Ralph review status">
```

## Minimal configuration

```nix
{
  imports = [
    inputs.will-nix.nixosModules.ralph
  ];

  services.ralph = {
    enable = true;
    origin = "https://git.example.org/owner/repo.git";
    repository = "owner/repo";
    forgejoApi = "https://git.example.org/api/v1/repos/owner/repo";
    botLogin = "ralph";

    openaiKeyFile = "/run/secrets/ralph/openai-key";
    ppqKeyFile = "/run/secrets/ralph/ppq-key";
    webhookSecretFile = "/run/secrets/ralph/webhook-secret";
    forgejoTokenFile = "/run/secrets/ralph/forgejo-token";
  };
}
```

Set the Forgejo webhook to `POST` JSON to
`https://YOUR_HOST/webhooks/forgejo`. Configure a long random secret in Forgejo
and store the same value in `webhookSecretFile`. Select custom pull request
events.

## Options

Required deployment options:

- `services.ralph.origin`: Git remote URL used for `git fetch` and
  stale-head checks.
- `services.ralph.repository`: Forgejo repository full name, for
  example `owner/repo`.
- `services.ralph.forgejoApi`: Forgejo repository API URL ending in
  `/api/v1/repos/owner/repo`.
- `services.ralph.openaiKeyFile`: file containing the OpenAI API
  key.
- `services.ralph.ppqKeyFile`: file containing the PPQ API key.
- `services.ralph.webhookSecretFile`: file containing the Forgejo
  webhook secret.
- `services.ralph.forgejoTokenFile`: file containing the Forgejo API
  token.
- `services.ralph.botLogin`: Forgejo login that owns the review
  comment.

Useful defaults:

- `services.ralph.listenAddress = "127.0.0.1"`
- `services.ralph.port = 8765`
- `services.ralph.stateDir = "/var/lib/ralph"`
- `services.ralph.commentMarker = null`, which uses
  `<!-- ralph:${repository} -->`
- `services.ralph.promptFile =
  "${services.ralph.package}/share/ralph/prompt.md"`
- `services.ralph.auditPromptDir =
  "${services.ralph.package}/share/ralph/audits"`

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
  public_contract, tests, design, build, archaeologist, verifier and collator.
  Prices must also be supported by ralph's ledger.

Custom prompt directories must include `consensus.md`, `wallet.md`, `p2p.md`,
`concurrency.md`, `build.md` and `archaeologist.md`. Custom model maps must
include `archaeologist`; old complete maps are rejected.
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
ralph-evaluate spend --state-dir /var/lib/ralph
```

Use the service account or another account permitted to read its private state.
The summary includes outstanding reservations in estimated_total_usd;
reserved_total_usd is the portion whose charge has not been settled.
usage_complete is false when any reported count or response is missing.

## Frozen prompt and routing experiments

The package installs `ralph-evaluate` for capture, replay, spend
inspection and offline summaries.
Capture performs Forgejo/Git reads but makes no model calls:

```sh
ralph-evaluate capture \
  --state-dir ./evaluation-state --output-dir ./cases \
  --origin https://git.example.org/owner/repo.git \
  --repository owner/repo \
  --forgejo-api https://git.example.org/api/v1/repos/owner/repo \
  --forgejo-token-file /run/secrets/forgejo-token \
  123
```

Capture retains complete Git objects in a dedicated evaluation checkout. The
initial download can be large. Keep that checkout with the manifests.
Capture stores bounded current PR discussion by default. Use
`--research-requests-json` to add exact prior discussion calls to the manifest,
and `--research-cutoff` to cap visible discussion at an ISO timestamp. The
requests file is a JSON list of objects with `name` and `arguments`; supported
names are `search_discussions`, `read_discussion`,
`read_current_pr_discussion` and `read_github_discussion`.
Replay reads frozen title/body, commit IDs, Git objects, prompt/model
configuration and captured research evidence. It disables live discussion, live
web fallback and lazy Git downloads. If archaeology or verification asks for a
research call that was not captured, the tool reports unavailable frozen
evidence and the model continues from code and the frozen input:

```sh
ralph-evaluate run \
  --state-dir ./evaluation-state --output-dir ./results \
  --openai-key-file /run/secrets/openai-key \
  --ppq-key-file /run/secrets/ppq-key \
  --review-budget-usd 1.00 \
  ./cases/case-123-*.json
```

Run supports `--prompt-file`, `--audit-prompt-dir`, `--models-json`,
`--routing-mode` and `--no-blind-alternatives` for ablation. Blind alternatives
are on by default after a valid concept candidate. The blind run keeps the
existing archaeologist model, gives it only base-side code tools and neutral
problem, goal and baseline inputs, and sends the result to the final verifier
rather than discovery. Each run gets a distinct private JSON artifact with
effective configuration identity, raw stage results, research inventory, usage,
final comment and any failure. Optional `--labels-json` maps case IDs to
expected findings; these labels are saved for comparison and never sent to
reviewers.

Compare useful findings and missed known findings alongside cost, incomplete
coverage and wall time. An empty review is not proof of a good route. Shadow
routing is useful for a bounded comparison before changing sensitive path
rules or removing a specialist.

See [the evaluation guide](evaluation.md) for offline summaries and a human
scorecard for comparing coverage and cost. Summaries use recorded attribution;
they do not automatically decide whether a finding is correct or useful.

The service and evaluation run command require `--ppq-key-file`. Compare
`adversarial` and `adversarial_glm` in debug stage metrics for sole/shared
accepted findings and published, dropped or unresolved candidates. OpenAI has a
USD 1.00 ceiling and GLM has a separate USD 0.50 ceiling; incomplete passes are
not evidence of model quality.
PPQ GLM rates use the model catalog input/output prices without cache discounts.
See [PPQ integration guide](https://ppq.ai/llms.txt).
