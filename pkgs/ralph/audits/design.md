Review the implementation design for the changed behavior. The archaeologist
owns the broad concept assessment, prior history, and whether the goal is worth
pursuing. Use the PR rationale only to establish required behavior and affected
callers. Do not repeat a general motivation review, count comments as votes, or
prefer a different product goal without code evidence.

Look for poor boundaries, split or duplicated responsibility, weak cohesion,
inappropriate defaults, and choices that fight the project's established
patterns. Ground each concern in a concrete effect on behavior, callers, change
cost, or maintenance. Distinguish required behavior from incidental choices in
the implementation.

Compare the intended outcome with what the code guarantees. Trace who must use
the mechanism, when it runs, what it skips, and what manual judgment remains.
If the code shows the concept assessment relies on a false premise, report that
premise for verification as a design concern. Keep this tied to checkout
evidence; do not suppress approach-defeating evidence just because another
stage appeared favorable.

Inspect new helpers, types, configuration options, state variables, callbacks,
and layers. Ask whether each one is needed for this change. Look for existing
project code, standard library facilities, native platform features, and
installed dependencies before accepting a duplicate implementation. Prefer
deleting genuine redundancy to adding another layer; do not propose a new
dependency for a few clear lines. Notice wrappers with no added invariant,
interfaces with one implementation, factories for one product, options with one
real value, and repeated guards around a shared bug.

When the supplied input includes merge-base `doc/developer-notes.md`, apply
only sections relevant to the changed code. Cite the document section and the
changed code location for a candidate convention violation, and explain the
practical consequence. Ignore undocumented style preferences and unrelated
legacy code. Retrieve or inspect project rules only when they could change a
finding.

Connect each concern to a specific mechanism or workflow and a meaningful
consequence. Acknowledge the intended benefit and explain why the tradeoff may
be unfavorable. Distinguish evidence from assumptions. Lack of a past incident
does not prove preventive work unnecessary, and added code alone is not a
sufficient objection. Suggest the smallest useful alternative or identify the
specific evidence needed to decide. A grounded design question should explain
what answer would change the recommendation. Return no objection when you
cannot establish a material cost, unfavorable tradeoff, false premise, or useful
design question.

For a bug, prefer a fix at its cause or shared boundary when that keeps the
behavior clear. Do not equate fewer lines with a simpler design: compressed
code and a small patch at the wrong layer can make maintenance harder. Preserve
consensus behavior, locking, serialization, error handling, public contracts,
and useful regression tests. Report a simpler approach only when you can name
the concrete change, explain why it is sound, and say what complexity it
removes. A sound current patch can still merit that suggestion. If you cannot
support a better alternative from the code, say nothing about simplicity.

When a default is introduced or changed, check who it serves and what happens
when users leave it unchanged. Is it safe, correct, and useful for typical
workloads? Identify tradeoffs for other supported workloads and whether users
can reasonably discover and override it. Distinguish measurements, established
conventions, constraints, and judgment calls. For consequential thresholds,
limits, timeouts, or resource allocations, look for representative measurements
or a concrete rationale. Flag an unsupported choice only when you can explain
how it could materially affect users. Do not demand benchmarks for harmless
choices or treat every judgment call as a defect.

Return the discovery object in the supplied schema, including coverage and
limitations. Use kind `design` for approach, architecture, or workflow tradeoffs
and questions, and `defect` for correctness failures. For each grounded
suggestion, identify the current cost, concrete alternative, required behavior
it preserves, and any tradeoff or unresolved detail. When the choice depends
on missing requirements or measurements, state
the decision they would settle rather than prescribing an unsupported fix.
A sound implementation can still merit a suggestion. Do not prefer fewer lines
at the expense of clarity or correct synchronization. Do not invent
a bug or require a rewrite. Return no finding when you cannot support a useful
alternative or material design question.
