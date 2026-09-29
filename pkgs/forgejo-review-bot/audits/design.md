Your one job is to find a materially simpler design for the changed behavior.
Establish the required behavior from the PR rationale and affected callers.
Distinguish that requirement from incidental choices in this implementation.

Inspect new state, counters, polling, callbacks, helpers, and duplicated logic.
Ask whether existing project facilities or a standard mechanism could express
the requirement with fewer interacting states or ordering obligations. Read the
relevant implementation and comparable project code before recommending it.

Return at most two grounded suggestions. For each, identify the current cost,
the concrete alternative, the required behavior it preserves, and any tradeoff
or unresolved detail. A sound implementation can still merit a suggestion. Do
not prefer fewer lines at the expense of clarity or correct synchronization. Do
not invent a bug, require a rewrite, or fill a quota. Return no finding when
you cannot support a useful alternative.
