# Task 9b1 fix round 3 re-review

## Scope and evidence

Reviewed the runtime range `96bf6a1c..d695789e` against the binding Task 9,
Task 9b, Task 9b1, and fix-round-3 briefs. I read the prior round-2 review, the
fix brief and report, the complete runtime diff, and the relevant unchanged
repair, ownership, orchestration, and migration callers. I did not rerun the
reported 105-test or dual-dialect migration gates; the remaining failures are
visible in the committed state transitions and the submitted tests do not
exercise the named interleavings.

## Prior-finding verdicts

1. **Atomic invalidation and bounded lease coverage — NOT ADDRESSED
   (Critical).** Rebuild now correctly queries every reserved mutation in the
   same hierarchy transaction that supersedes the revision
   (`src/integration/candidates.py:315-335`). Claims last 135 seconds, and the
   immediate pre-push transaction requires 125 seconds on both the exact nonce
   and lease (`src/integration/candidates.py:1888-1922`), so the former
   transport-bound defect is addressed. Expired writers also cannot push, while
   observation can mark an exact desired tip applied or delete an expired claim
   proved unapplied at the exact old tip (`src/integration/candidates.py:1813-1846`,
   `src/integration/candidates.py:1935-1977`).

   The required invalidator invariant is still violated for an expired but
   ambiguous claim. If remote is neither `expected_old_sha` nor `desired_sha`,
   reconciliation deliberately leaves the row `reserved`, as required
   (`src/integration/candidates.py:1957-1977`). Repair-stage expiry nevertheless
   ignores that row because it filters blockers to `expires_at > observed_at`
   (`src/integration/repair.py:798-809`) and can then expire/change the active
   stage. Both ordinary ownership transfer and the new confirmed transfer make
   the same mistake (`src/integration/ownership.py:123-135`,
   `src/integration/ownership.py:225-235`) and can fence in a new owner while the
   prior mutation's remote result remains unresolved. This directly contradicts
   the fix brief's rule that unexpected/ambiguous remote remains blocked and
   that stage and transfer invalidators share the same-transaction claim check.
   The reported “stage/transfer equivalents” test covers only a live claim, not
   an expired ambiguous one. Minimal remediation: block invalidators on every
   `reserved` claim in their exact scope, regardless of claim expiry; only a
   completed observation that atomically proves expected-old/absent and removes
   the row may permit invalidation. Add both writer-before-invalidator orderings
   for stage expiry and each ownership-transfer path.

2. **Durable repair-to-collector handoff and final expected-old — NOT ADDRESSED
   (Critical).** The resolution now immutably records the collector owner/fence,
   and replay canonical-rereads it (`src/integration/candidates.py:755-862`).
   Final publication also derives its expected old OID from the current
   revision's applied repair handoff before the partial mutation
   (`src/integration/candidates.py:1315-1363`), addressing that sub-defect.

   The ownership transfer is still committed before the durable handoff ref
   mutation is reserved. `_reserve_repair_handoff()` atomically transfers the
   branch and stores the collector fence on the resolution
   (`src/integration/candidates.py:829-857`), then returns and commits. Both crash
   hooks execute before `_mutate_ref()` creates the `repair_handoff` mutation
   row (`src/integration/candidates.py:683-699`,
   `src/integration/candidates.py:1739-1807`). In that exposed window a
   concurrent rebuild's same-transaction blocker sees no reserved mutation and
   can supersede revision N. The accepted head is still only on its qualified
   repair ref; replay then returns stale because the batch revision changed,
   leaving the pushed resolution unconsumed. The crash matrix serially retries
   acceptance and never interleaves rebuild in this window. Minimal remediation:
   persist a replay-adoptable `repair_handoff` mutation reservation in the same
   hierarchy transaction as collector transfer/fence persistence, or introduce
   an equally exact pending-handoff record that every invalidator locks and
   honors until the mutation becomes applied. Add the transfer-commit / rebuild
   race and fresh-service replay.

3. **Immutable canonical workspace provenance — ADDRESSED.** Reservation stores
   the server-resolved absolute path and subsequent push authorization compares
   and uses that immutable value. The same-workspace-ID rebind regression is
   present. I found no regression in this fix range.

4. **Seeded legacy pushed-row migration compatibility — ADDRESSED for upgrade,
   with separate downgrade breakage below.** The predecessor migration now
   backfills the actual integration branch and deliberately rejects missing
   operation episode, stage deadline, or project authority
   (`migrations/versions/e1eab6dbc186_candidate_durable_mutation_claims.py:191-237`).
   The new migration reconstructs the workspace path, marks rows whose target
   equals the integration branch as `legacy_integration`, and refuses absent or
   non-absolute source data before DDL
   (`migrations/versions/46f910d0dce6_candidate_handoff_workspace_.py:87-138`).
   Runtime selects the persisted integration branch for that kind
   (`src/integration/candidates.py:626-640`). The dual-dialect tests seed a
   pushed row and an irreconstructible deadline case, although they assert the
   reconstructed identity rather than driving runtime acceptance.

## New breakage introduced by the fix

1. **Important — the real handoff confirmer's successful release is rejected on
   the first acceptance attempt.** `confirm_transfer()` returns its stale
   pre-callback row (`src/integration/ownership.py:173-203`). The production
   confirmer atomically changes the owner to `released`, clears `session_id` and
   `workspace_id`, and records `confirmed_workspace_id`
   (`src/orchestrator/workspace.py:1116-1222`,
   `src/orchestrator/workspace_attachments.py:121-168`).
   `transfer_confirmed_on()` then compares the cleared current fields with the
   stale non-null values and raises `BranchBusy`
   (`src/integration/ownership.py:217-224`); `accept_repair()` converts that to
   `wait` (`src/integration/candidates.py:683-686`) even though termination and
   detach already succeeded. A later manual retry can converge, but there is no
   new external event at this point to guarantee that retry. The repair tests
   install `confirm_handoff=lambda _row: True`, so they cannot observe the
   production callback's state mutation (`tests/test_integration_candidates.py:1429-1444`).
   Minimal remediation: after confirmation, re-read/return canonical ownership
   and consume the exact `released` state using `confirmed_workspace_id`, as the
   pre-existing `transfer()` path already does at
   `src/integration/ownership.py:159-171`; cover the real mutating callback
   contract.

2. **Important — downgrade silently destroys the only persisted collector-handoff
   provenance.** Migration `46f910d0dce6` permits the one-time immutable
   `handoff_owner_id`/`handoff_fence_token` transition, but downgrade drops both
   columns without a live-data guard
   (`migrations/versions/46f910d0dce6_candidate_handoff_workspace_.py:141-157`).
   If downgrade occurs after collector transfer/fence persistence and before
   acceptance, the ownership row remains collector-owned while the pushed
   resolution loses the only canonical collector fence. Re-upgrade reconstructs
   those columns as NULL; `accept_repair()` sees that the repair owner is no
   longer current, obtains no confirmation, and `_reserve_repair_handoff()`
   rejects the absent proof (`src/integration/candidates.py:676-686`,
   `src/integration/candidates.py:808-827`). The submitted U-D-U tests seed only
   handoff-null legacy rows (`tests/test_migration_candidate_mutation_claims.py:223-269`,
   `tests/test_migration_candidate_mutation_claims.py:273-329`). Minimal
   remediation: add a deliberate pre-downgrade drain/refusal for any non-null
   handoff pair (and any other state whose replay depends on it), with SQLite and
   PostgreSQL seeded-live U-D-U coverage.

## Outside-scope observation

The pre-existing SQLite-to-PostgreSQL copy-adapter omission remains Task 12
scope; this runtime diff does not newly worsen that unsupported copy path.

## Strengths and quality verdict

The fix substantially improves the protocol: rebuild's authoritative blocker is
now in the correct transaction, the lease/claim timing is explicit and bounded,
expired-lease observation is write-free, resolution identity now includes
canonical workspace and legacy target provenance, and final expected-old is
derived from durable current-revision history. The implementation and tests are
generally clear, but the two remaining authority windows are safety-critical and
the production handoff/migration downgrade paths are not represented by the
reported tests.

## Verdict

**FAIL — findings remain open.** Open counts: **Critical 2, Important 2,
Minor 0**.
