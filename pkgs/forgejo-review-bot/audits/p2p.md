P2P and resource-abuse profile: apply this only to changed peer-controlled
inputs, relay behavior, request tracking, admission policy, eviction, resource
limits, and repeated work. It refines the adversarial review; do not treat it
as a separate stage.

Ask what a remote peer can control, including size, order, timing, and
repetition, then trace that control through validation, deduplication, caches,
locks, persistence, timers, disconnect paths, and error handling. Check size,
count, expiry, and work limits under repeated or reordered peer messages.

For a resource-abuse or peer-behavior claim, identify the reachable peer action,
the bound or invariant that changed, and the concrete consequence. Distinguish
a remote-triggerable cost from local maintenance work, and explain why existing
rate limits, caches, or locks do not bound the scenario. Report only concrete
risks introduced by this PR, tied to checkout evidence in the same adversarial
discovery object.
