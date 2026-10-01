# Frozen review evaluation

The frozen runner is for local comparison of review configurations. Capture
stores the PR text, Git object IDs, prompt/model configuration, and hashes.
Replay reads those frozen inputs and writes private run artifacts. The
`summarize` command reads those artifacts offline; it does not call Forgejo,
Git remotes, or a model.

```sh
ralph-evaluate summarize ./results
```

The summary groups runs by `effective_config_sha256`. For each group it counts
runs, failures, partial coverage, budget exhaustion, labels present, verifier
decisions, accepted findings, known estimated model spend, unknown usage
indicators, elapsed time, and per-stage source attribution.

The cost fields intentionally separate attempt totals from per-run averages.
`cost_usd.total` is the sum of priced token usage saved in the run artifacts.
Unknown usage is excluded from that subtotal and counted separately. Accepted
finding counts mean verifier-published findings. They are not human validity or
recall measurements.

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

Freeze the reviewed head before a later fix. Keep later discussion, expected
findings, and labels out of reviewer inputs.

## Human scoring

Treat labels as local notes for human comparison. Current labels are arbitrary;
the runner does not know whether a published finding matches a label, and the
summary must not be read as recall.

Score final comments with blinded judgment. The scorer should not know which
configuration produced each comment. For each case, record unique valid
findings, severity, actionability, false positives, missed known labels, and
useful design criticism. Review a sample of dropped and unresolved verifier
decisions, especially consequential claims, so confident false drops are
visible.

For known-bug cases, count a finding as valid when it identifies the changed
behavior, reachable precondition, broken invariant, and consequence well enough
for a maintainer to act. For clean cases, count unsupported defect claims as
false positives. Keep design feedback separate from correctness recall.

## Comparisons

Run a baseline first, then change one factor at a time. Compare baseline runs
against selective routing, prompt profiles, model profile changes, and reasoning
effort changes by their `effective_config_sha256` groups. Use the summary for
cost, latency, coverage, failure, and verifier disposition counts. Use the
human scoring sheet for recall, false positives, missed labels, and false
drops.

Useful comparison fields are unique valid findings, known-defect recall by
consequence, false positives per PR, verifier false drops, unresolved claims,
dollars per PR, dollars per unique useful finding, p95 cost, p95 wall time,
budget exhaustion, failed attempts, and candidates lost when a stage cannot
finish.

Repeat promising configurations on a smaller set to check variation before
running held-out cases. Add a specialist only when it finds useful issues that
the existing stages miss often enough to justify the extra spend.
