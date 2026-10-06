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

For a new optional RPC argument, check named calls with earlier optional
arguments omitted, and whether functional coverage would catch a wrong mapping
or ignored value. Top-level arguments already support named calls; an
OBJ_NAMED_PARAMS options object is not required for that. Assess argument
placement and naming against applicable project conventions and sibling RPCs.
When an options key intentionally shares a top-level argument's name, check
`.also_positional = true` and that the handler reads both locations; otherwise
check for conflicting names. For startup options, distinguish whether an
argument was set from its value: IsArgSet includes negated settings. Check
whether GetArg or GetBoolArg expresses the intended behavior. Defaults
duplicated across the C++ and Cap'n Proto boundary must agree. For logging,
check whether the logging helper adds a newline, whether unconditional output
causes noise at the expected frequency, and whether sanitising removes
information needed to understand the warning. Explain the concrete consequence
before reporting a finding.
