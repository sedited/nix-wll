Inspect changed shared objects, ownership, callbacks, locks, queues,
cancellation, and teardown. Identify the owning thread and lock for each
changed shared value. Trace lock acquisition order, callbacks while locked,
references and views that outlive mutations, iterator invalidation, shutdown,
and cross-thread handoff.

For a race, deadlock, or lifetime claim, supply the reachable event ordering
and explain why existing synchronization or ownership does not prevent it.
Report only concrete risks introduced by this PR.
