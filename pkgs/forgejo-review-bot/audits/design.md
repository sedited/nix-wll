Review architecture and design for the changed behavior. Look for poor
boundaries, split or duplicated responsibility, weak cohesion, and choices that
fight the project's established patterns. Ground each concern in a concrete
effect on behavior, callers, change cost, or maintenance. Do not report
subjective style preferences. Establish the required behavior from the PR
rationale and affected callers, and distinguish it from incidental choices in
the implementation.
Before optimizing the mechanism, ask whether the behavior itself is necessary.
If the patch works hard to handle rapid toggles, retries, polling, or other
ordering pressure, verify that this responsiveness is promised or useful, and
state the user-visible or caller-visible value. If the value is not supported,
make that the design concern instead of proposing a more polished mechanism for
the same incidental behavior.

Inspect new state, counters, polling, callbacks, helpers, and duplicated logic.
Ask whether existing project facilities or a standard mechanism could express
the requirement with fewer interacting states or ordering obligations. Read the
relevant implementation and comparable project code before recommending it.

Return the discovery object in the supplied schema, including coverage and
limitations. For each grounded suggestion, identify the current cost, concrete
alternative, required behavior it preserves, and any tradeoff or unresolved
detail. A sound implementation can still merit a suggestion. Do not prefer
fewer lines at the expense of clarity or correct synchronization. Do not invent
a bug or require a rewrite. Return no finding when you cannot support a useful
alternative.
