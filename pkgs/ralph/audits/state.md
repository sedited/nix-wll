Inspect changed persisted state and recovery behavior. Identify the invariant,
who enforces it, and what happens on partial failure, early return, retry,
restart, reindex, rescan, reorg, rollback, or migration where relevant.
Distinguish successful parsing or loading from successful restoration of usable
state. Report only concrete risks introduced by this PR.

Check what survives a partial write or interrupted operation and what the next
startup or recovery action does with it. Identify the durable commit point,
rollback behavior, and applicable format or migration guarantees. Successful
loading does not prove that restored state is complete or usable.
