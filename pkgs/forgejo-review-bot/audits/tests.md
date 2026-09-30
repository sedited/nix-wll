Inspect whether changed tests provide useful, reliable evidence for the
intended behavior. Read assertion helpers and relevant production code when
their semantics matter.

For an important assertion, identify the regression it would catch and why.
Name the exact next value, event, or state transition the implementation should
produce. Check whether a realistic regression would make that assertion fail,
or whether the test would still pass after the behavior broke. Distinguish a
request or entry log from completed behavior, especially logging that happens
before a task finishes. Check that expected and forbidden events belong to the
observation window and that the asserted sequence matches the implementation's
state transitions.

Check whether tests contact real services, routers, or other resources outside
their controlled fixtures. Examine sleeps and repeated checks: what event are
they waiting for, what progress can occur during that interval, and what
regression would be exposed by the wait? Prefer an observable completion signal
when it establishes the necessary ordering. Keep a bounded observation period
when the behavior itself is "nothing else happens during this window"; otherwise
do not defend a wait unless it proves a useful ordering or timeout property.

Check whether each added test belongs in this suite, duplicates existing
coverage, or can reuse an existing fixture. Judge the marginal regression
coverage against the fixture setup, test runtime, and maintenance burden. Flag
tests that add cost without useful evidence, or suites whose setup is out of
proportion to the behavior they protect. Suggest deleting or consolidating
tests only when the useful regression coverage remains. Do not impose a test
count target. Report concrete deficiencies and useful simplifications, not
generic requests for more coverage.

When a changed default affects performance or reliability, inspect the evidence
for its value. Does a measurement use representative workloads and compare
meaningful alternatives, or does a test merely assert the chosen constant?
Distinguish regression coverage from evidence that the default suits users.
If important evidence is missing, name the unresolved tradeoff and the smallest
useful measurement; do not request a benchmark suite for a harmless choice.

Return the discovery object in the supplied schema, including coverage and
limitations. For a test-quality suggestion, name the coverage gained or lost,
its current cost, the concrete alternative, and why useful regression
protection remains.
