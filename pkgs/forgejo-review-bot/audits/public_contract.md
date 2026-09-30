Inspect user-visible RPC, CLI, configuration, and other public behavior changed
by this PR. Check defaults, results, errors, help text, documentation, and
release notes where relevant. Suggest a release note only when users need to
act, change configuration, or understand a significant behavior change, and
follow the applicable documented project policy. If the patch omits one,
identify the exact observable change and why it matters to users; do not flag
internal-only changes or let release-note advice crowd out substantive review.
For RPC changes, check argument conversion, omitted versus null values, amount
parsing and formatting, result types, error behavior, and applicable
compatibility policy. Trace the actual exposed behavior and affected callers.
Keep release-note requests tied to a concrete user consequence.

Return the discovery object in the supplied schema, including coverage and
limitations. Distinguish defects from suggestions and tie every claim to
checkout evidence.
