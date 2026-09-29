Inspect whether changed tests provide useful, reliable evidence for the
intended behavior. Read assertion helpers and relevant production code when
their semantics matter.

For an important assertion, identify the regression it would catch and why.
Distinguish a request or entry log from completed behavior. Check that expected
and forbidden events belong to the observation window and that the asserted
sequence matches the implementation's state transitions.

Check whether tests contact real services, routers, or other resources outside
their controlled fixtures. Examine sleeps and repeated checks: what event are
they waiting for, and what regression can occur during that interval? Prefer an
observable completion signal when it establishes the necessary ordering. Do not
remove bounded observation periods without an adequate replacement.

Check whether each added test belongs in this suite, duplicates existing
coverage, or can reuse an existing fixture. Report concrete deficiencies and
useful simplifications, not generic requests for more coverage.
