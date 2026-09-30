Consensus and validation profile: apply this only to changed consensus,
validation, script, serialization, chainstate, coins, kernel, or related helper
paths. It refines the adversarial review; do not treat it as a separate stage.

Classify changed checks as consensus, relay or mining policy, or local
behavior. Compare acceptance, rejection, and state transitions at base and
head. For relevant changes, examine activation boundaries, script flags,
serialization, integer behavior, cache keys, ConnectBlock, DisconnectBlock, and
reorg paths.

For a claimed refactor, look for an input or prior state that distinguishes
the versions. Establish the exact call path and preconditions before claiming
consensus divergence. A stricter policy rule alone is not a consensus change.

Report only claims grounded in a reachable call path and the checked-out
base/head behavior. Tie every profile finding to checkout evidence in the same
adversarial discovery object.
