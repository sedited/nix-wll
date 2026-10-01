# Frozen review evaluation

The frozen runner is for local comparison of review configurations. Capture
stores the PR text, Git object IDs, prompt/model configuration, bounded current
discussion, optional prior discussion calls, and hashes. Replay reads those
frozen inputs and writes private run artifacts. It does not fall back to live
discussion or web research. Missing research evidence becomes an unavailable
tool result, so the review continues from code and the frozen input. The
`summarize` command reads saved artifacts offline; it does not call Forgejo, Git
remotes, or a model.

```sh
ralph-evaluate summarize ./results
```

The summary groups runs by `effective_config_sha256`. For each group it counts
runs, failures, partial coverage, budget exhaustion, labels present, verifier
decisions, accepted findings, archaeology completion, published conceptual
concerns, candidate and verified review recommendations, known estimated model
spend, unknown usage indicators, elapsed time, and per-stage source attribution.

The cost fields intentionally separate attempt totals from per-run averages.
`cost_usd.total` is the sum of priced token usage saved in the run artifacts.
Unknown usage is excluded from that subtotal and counted separately. Accepted
finding counts mean verifier-published findings. Conceptual concern counts mean
the verifier published a concept assessment. Neither count is a human accuracy
or recall measurement.

## Case selection

Use frozen cases that represent the decisions the bot has to make in real
review. Include both known bugs and clean changes. A useful set covers:

- consensus and script behavior
- P2P, relay, and denial-of-service risk
- wallet funds, signing, descriptors, fees, and privacy
- C++ lifetime, lock ordering, callbacks, and shutdown
- persistence, migration, restart, reorg, rescan, and recovery
- RPC, CLI, configuration, error, and result contracts
- build, portability, dependency, and toolchain changes
- test-only changes and clean refactors

Freeze the reviewed head before a later fix. Use `--research-cutoff` when later
discussion should not be visible, and use `--research-requests-json` for exact
prior discussion calls that the replay may need. The requests file is a JSON
list of tool calls using `search_discussions`, `read_discussion`,
`read_current_pr_discussion` or `read_github_discussion`. Keep expected findings
and labels out of reviewer inputs.

## Human scoring

Treat labels as local notes for human comparison. Current labels are arbitrary;
the runner does not know whether a published finding matches a label, and the
summary must not be read as recall.

Score final comments with blinded judgment. The scorer should not know which
configuration produced each comment. For each case, record unique valid
findings, severity, actionability, false positives, missed known labels, useful
design criticism, false proposed stops, and useful code findings that appeared
after a candidate or verified `would_stop` recommendation. Review a sample of
dropped and unresolved verifier decisions, especially consequential claims, so
confident false drops are visible.

For known-bug cases, count a finding as valid when it identifies the changed
behavior, reachable precondition, broken invariant, and consequence well enough
for a maintainer to act. For clean cases, count unsupported defect claims as
false positives. A proposed stop is false when the PR still deserved code review
or later stages found useful code findings. Keep design feedback and concept
assessment quality separate from correctness recall. Public concept feedback
should contain only supported, actionable objections and at most one alternative
shown to be better under the PR's requirements and costs. Check for duplicated
design findings and unnecessary prose. Score favorable assessments and rejected
or inconclusive alternatives from the detailed trace, where they are retained.

## Comparisons

Run a baseline first, then change one factor at a time. Compare baseline runs
against selective routing, prompt profiles, model profile changes, reasoning
effort changes, and the `--no-blind-alternatives` ablation by their
`effective_config_sha256` groups. Blind alternatives are on by default after a
valid concept candidate. The blind run uses the existing archaeologist model,
base-only code tools, and neutral problem, goal and baseline inputs; its result
goes to the final verifier, not to discovery. Use the summary for cost, latency,
coverage, failure, review recommendation and verifier disposition counts. Use
the human scoring sheet for recall, false positives, missed labels, false drops
and false proposed stops.

Useful comparison fields are unique valid findings, known-defect recall by
consequence, false positives per PR, verifier false drops, unresolved claims,
dollars per PR, dollars per unique useful finding, p95 cost, p95 wall time,
budget exhaustion, failed attempts, candidates lost when a stage cannot finish,
and useful downstream findings after proposed stops.

Repeat promising configurations on a smaller set to check variation before
running held-out cases. Add a specialist only when it finds useful issues that
the existing stages miss often enough to justify the extra spend.
