# Task 9b2 fix1a round4 — preserve the authority deadline across commit

Read the round3 review in this directory for the sole open finding verbatim.
Prior implementer completed three rounds; you own this narrow correction now.

Return an absolute monotonic transport deadline from the successful hierarchy-
locked prewrite validation, sampled before the wall-clock authority sample so
conversion is conservative. Carry that deadline across commit, crash-hook and
dispatch into the actual Git transport. Commit latency and event-loop delay must
consume, never reset, the approved transport budget. Keep cleanup margin reserved.

Use a typed internal deadline value or an optional tightening-only deadline on
the daemon Python transport interface. An input deadline can only shorten the
existing maximum: validate finite values and cap at entry plus the exported
maximum. Do not add timeout parameters to callable command contracts. Existing
candidate/repair callers retain their bounded default. Do not add another
transport implementation or change prewrite ambiguity/reconciliation semantics.

Cover the production root-to-Git boundary with deterministic delayed commit or
post-marker dispatch, using real local Git preparation and an instrumented remote
child (no live network). Cover exhausted budget (no remote start) and partially
consumed budget (remaining time only); ensure cleanup fits original authority.
Test normal promotion and cancellation/replay retain their behavior. Reuse existing
deadline/process fixtures where possible; no full-suite or repeated unaffected
area runs. Focused files: tests/test_git_app_auth.py and
tests/test_integration_main_promotion.py; final area includes
tests/test_git_manager_async.py and tests/test_integration_promotion.py.

Scope runtime to src/git/manager.py and src/integration/main_promotion.py plus
covering tests. No schema/fix1b, network under locks, forge/operator writes,
subagents, new workspace, or deployment changes. Commit code/tests, then write
task-9b2-fix-1a-round4-report.md with exact tests/output, commit IDs, and concerns.
