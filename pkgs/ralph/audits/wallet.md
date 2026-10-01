Wallet funds and privacy profile: apply this only to changed wallet funds,
privacy, signing, descriptors, key handling, coin selection, fee and change
logic, backup and restore, rescans, and wallet persistence. It refines the
adversarial review; do not treat it as a separate stage.

Check what survives interruption, restart, rescan, reorg, migration, and
partial writes where those paths are affected. Inspect whether restored or
loaded wallet state is complete and usable, not merely parseable.

For a funds or privacy claim, identify the user action or prior state, the
wallet invariant that changes, and the concrete consequence. Establish how
funds could be lost, become unspendable, be sent incorrectly, or how private
wallet data could be revealed. Report only concrete risks introduced by this
PR, tied to checkout evidence in the same adversarial discovery object.
