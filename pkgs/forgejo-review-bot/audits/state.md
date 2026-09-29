Inspect changed persisted or shared state. Identify the invariant, who enforces
it, and what happens on partial failure, early return, retry, invalidation,
teardown, and restart where relevant. Distinguish successful parsing or loading
from successful restoration of usable state. Report only concrete risks
introduced by this PR.
For caches and request trackers, identify the key used to suppress repeat work
and when it is cleared or expires. Check both repeated requests and valid
retries after the underlying state changes.

Return the discovery object in the supplied schema, including coverage and
limitations. Distinguish defects from suggestions and tie every claim to
checkout evidence.
