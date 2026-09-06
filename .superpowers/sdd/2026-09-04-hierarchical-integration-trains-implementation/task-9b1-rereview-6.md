# Task 9b1 fix round 6 re-review

## Finding verdict

1. **Public `build()` must reach safe expired-claim CAS takeover instead of
   stalling in observation — ADDRESSED.** `build()` now consumes a structured
   `_MutationObservation` and continues only when there are no blockers;
   `rebuild()` separately treats either blockers or recoverable rows as
   unresolved, preserving its invalidation fence
   (`src/integration/candidates.py:188-205`,
   `src/integration/candidates.py:252-271`). Observation performs each
   authenticated ref read outside SQL, reconciles exact desired remote without
   pushing, classifies read failures/live expected/expired unexpected as
   blockers, and classifies expired expected-old as recoverable only after a
   hierarchy-locked phase/authority check
   (`src/integration/candidates.py:2211-2257`). It neither deletes the row nor
   changes its nonce.

   `_recoverable_build_mutation()` rereads the canonical row under the project
   hierarchy lock and binds batch/revision, candidate-only purpose,
   repository/branch/target, exact OIDs, old nonce/expiry, operation
   episode/stage, project lease owner/fence and remaining 135-second window,
   collector owner/fence, and current revision state
   (`src/integration/candidates.py:2259-2324`). A final claim additionally
   requires the exact reserved publication identity on a built revision; a
   partial claim requires the current cursor ordinal and persisted conflict
   partial head on a constructing revision
   (`src/integration/candidates.py:2325-2363`). Thus an earlier/later phase,
   stale revision, repair/handoff purpose, insufficient authority, or changed
   identity remains a blocker. Multiple rows are fully accumulated before the
   disposition is returned, so any blocker wins independent of query order
   (`src/integration/candidates.py:2223-2257`).

   Recovery then follows the previously approved `_mutate_ref()` path: it
   rereads remote, performs the exact old-nonce/state/expiry CAS, and only that
   winner receives a new execution nonce and can pass pre-push authority. A
   race after observation is therefore re-fenced rather than authorized by the
   observation result (`src/integration/candidates.py:1925-1965`,
   `src/integration/candidates.py:2060-2150`). Desired remote remains
   push-free; unexpected remote retains its immutable reserved blocker.

   The new public-only regression crashes after reservation before push for
   both `candidate_partial` and `candidate_final`, expires the claim while the
   project lease remains sufficient, overlaps two fresh services, and asserts
   exactly one takeover push plus canonical convergence
   (`tests/test_integration_candidates.py:1288-1396`). The fail-closed matrix
   covers live expected-old, expired unexpected, insufficient lease, failed
   authenticated read, and out-of-order progress with zero pushes and unchanged
   nonce (`tests/test_integration_candidates.py:1399-1490`). Exact desired
   remote is reconciled to applied and completes through public `build()` with
   no service push (`tests/test_integration_candidates.py:1493-1552`).

## New breakage in the fix diff

None. The observer introduces no executor capability, no remote write inside a
database transaction, and no ambiguity deletion. Its only database mutation is
the existing exact desired-remote reconciliation. The prior inserter-only/live-
reader nonce protocol, repair-handoff atomic reservation, expiry-independent
stage/rebuild/transfer blockers, post-push authority fence, and canonical
losing-acceptance result remain intact.

## Out-of-scope observations

None. The unrelated `80034ced` origin/main merge is outside the exact runtime
diff and does not overlap candidate runtime or tests.

## Test and report checks

- The fix report records eight passing focused public observer tests, 21
  passing public/executor/invalidation tests, 62 passing candidate-file tests,
  and a final affected-area result of 111 passed, plus Ruff, py_compile,
  Alembic-head, and diff checks. Those reported suites were not rerun, per the
  scoped instruction.
- No focused test was run. The submitted barriers exercise the two fresh public
  callers at authenticated observation and the code's later nonce CAS is the
  sole push-authority transition.
- Report claims match the committed runtime and test semantics. No schema or
  migration changed in this range.

## Quality verdict

The structured disposition cleanly separates observation, phase eligibility,
and executor authority. The validation is intentionally strict, blocker
aggregation is safe for multiple rows, and the public tests exercise real Git
effects rather than a private-method shortcut. The fix preserves the previously
reviewed transaction and ownership boundaries.

## Verdict

**PASS — all findings addressed with no new Critical or Important breakage.**
Open counts: **Critical 0, Important 0, Minor 0**.
