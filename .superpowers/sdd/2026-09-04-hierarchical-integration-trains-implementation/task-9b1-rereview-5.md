# Task 9b1 fix round 5 re-review

## Finding verdict

1. **Live canonical readers must not share another executor's nonce —
   ADDRESSED.** `_reserve_repair_handoff()` now records a nonce in local
   `owned_mutation_nonces` only for the caller whose transaction actually
   inserted the row (`src/integration/candidates.py:896-904`). A canonical
   reader therefore remains observation-only while a claim is live
   (`src/integration/candidates.py:1893-1933`). For an expired exact-expected
   claim, takeover is an affected-row CAS over the exact ID, `reserved` state,
   old nonce, old expiry value, and expired predicate after current hierarchy,
   revision/stage/owner/fence, and project-lease validation; only the one-row
   winner receives the new nonce and expiry
   (`src/integration/candidates.py:2060-2114`). Immediate pre-push validation
   binds that exclusive nonce and the full remaining transport window
   (`src/integration/candidates.py:2116-2150`). Desired remote is reconciled
   without a push, unexpected remote is left reserved, and a lost push response
   is followed by an exact remote read rather than a retry
   (`src/integration/candidates.py:1903-1982`). Post-push authority loss can
   record the remote fact applied but returns false before lifecycle acceptance
   (`src/integration/candidates.py:1984-2040`). The final member/cursor/resolution
   CAS now converts a concurrent loser to canonical `already_accepted` or
   `wait` (`src/integration/candidates.py:704-766`).

   The focused tests overlap two fresh repair-acceptance services and assert one
   push/no exception, exercise one-winner expired takeover plus live and
   unexpected waits, and exercise lost-response reconciliation
   (`tests/test_integration_candidates.py:1334-1514`,
   `tests/test_integration_candidates.py:2011-2084`). These tests support the
   sole prior finding for the repair-handoff path.

## New breakage in the fix diff

1. **Important — ordinary candidate-build crash recovery can no longer reach
   the expired-claim takeover path.** This fix removes the prior deletion of an
   expired reserved row whose authenticated remote is still exact expected-old;
   `_reconcile_observed_mutation()` now changes only desired-remote rows to
   applied (`src/integration/candidates.py:2163-2194`). That is necessary to
   preserve the row for the new CAS takeover, but `build()` first calls
   `_observe_unresolved_mutations()` and returns `wait` whenever the query found
   any reserved row (`src/integration/candidates.py:173-190`). The observer
   returns `bool(rows)` even when the sole row is expired, remote is exact
   expected-old, and reconciliation made no change
   (`src/integration/candidates.py:2196-2211`). Consequently a crash after
   reserving a normal `candidate_partial` or `candidate_final` mutation but
   before push leaves public `build()` returning before `_mutate_ref()` on every
   retry. It never executes `_takeover_expired_mutation()`, even during the
   interval where the project lease still has the required 135 seconds, and the
   expiry-independent invalidators correctly keep the surviving row blocked.

   The changed expired-writer test now asserts one fresh build returns `wait`
   and that the expired reserved row survives, but never demonstrates later
   public-path takeover (`tests/test_integration_candidates.py:1232-1285`). The
   exclusive-takeover matrix calls the private `_mutate_ref()` directly and
   therefore bypasses the blocking observer
   (`tests/test_integration_candidates.py:1387-1514`). This violates the fix
   brief's fresh crash-recovery requirement and Task 9b1's restart-at-every-
   mutation-boundary contract. Minimal remediation: make observation distinguish
   an expired exact-expected row from live or unexpected blockers. After its
   authenticated exact read, allow the normal build path to continue so
   `_mutate_ref()` rereads remote and competes for the exact CAS takeover; keep
   live, unexpected, and insufficient-authority cases blocked. Add a public
   `build()` crash-before-push test with claim expired but sufficient project
   lease remaining, proving exactly one takeover push and eventual completion
   rather than repeated wait.

## Regression checks on prior approved invariants

- The repair-handoff mutation remains reserved in the same local hierarchy
  transaction as collector transfer and persisted fence; no token, ref read,
  Git, or forge operation moved under that transaction
  (`src/integration/candidates.py:755-905`).
- Expired unexpected rows remain `reserved`, so stage, rebuild, and both
  ownership-transfer invalidators continue to block them. Desired remote is
  monotone-applied and no longer blocks.
- Overlapping final acceptance rolls back a losing partial transaction and
  rereads canonical resolution state; it does not double-advance the member or
  cursor.

## Out-of-scope observations

The pre-existing SQLite-to-PostgreSQL copy-adapter omission remains Task 12
scope and is unchanged.

## Test and report checks

- The fix report records 103 passing affected-area tests, 54 passing candidate
  tests, focused exclusive-executor/lost-response/overlap results, Ruff,
  py_compile, a sole Alembic head, and diff check. Those reported suites were
  not rerun, per the scoped instruction.
- No focused test was run. The public-path stall follows directly from
  `build()`'s unconditional early return, and the submitted private-method test
  does not answer it.
- Report claims match the direct repair-acceptance executor behavior. Its claim
  that fresh crash recovery waits for expiry and then uses CAS takeover does not
  hold for ordinary candidate build mutations.

## Quality verdict

The exclusive executor design is materially safer: insertion ownership is
local, takeover is an exact durable CAS, remote ambiguity stays blocked, and
lost responses reconcile without re-push. The remaining issue is a control-flow
integration gap between the observer and that sound takeover primitive, causing
a durable public-path stall after a normal pre-push crash.

## Verdict

**FAIL — new Important breakage remains.** Open counts: **Critical 0,
Important 1, Minor 0**.
