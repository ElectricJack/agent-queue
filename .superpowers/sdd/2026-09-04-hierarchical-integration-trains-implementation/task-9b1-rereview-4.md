# Task 9b1 fix round 4 re-review

## Finding verdicts

1. **Every unresolved mutation must block invalidation regardless of claim
   expiry — ADDRESSED.** Repair-stage expiry now queries every exact
   operation/stage mutation in `state='reserved'` without an expiry filter
   (`src/integration/repair.py:798-808`). Ordinary and confirmed ownership
   transfer apply the same expiry-independent branch-scope predicate in their
   mutation transaction (`src/integration/ownership.py:117-134`,
   `src/integration/ownership.py:228-265`). Rebuild already checks every
   reserved current-revision mutation in the same hierarchy transaction as
   supersession (`src/integration/candidates.py:315-335`). Conversely, a late
   writer must pass `_validate_authority_on()` before inserting its mutation,
   so a committed stage, owner/fence, or revision change rejects the stale
   snapshot before reservation (`src/integration/candidates.py:1850-1881`). The
   new parameterized tests cover all four writer-first blockers and all four
   invalidator-first fences (`tests/test_integration_candidates.py:968-1131`).

2. **Repair handoff reservation must be atomic with collector transfer and
   persisted fence — ADDRESSED, subject to the separate concurrent-adoption
   regression below.** `_reserve_repair_handoff()` holds the hierarchy-first
   transaction while it revalidates the exact pushed resolution, project,
   batch/revision, operation episode/stage, lease, and branch owner. In that
   same transaction it transfers to the collector, persists the immutable
   collector fence, and inserts or canonical-rereads the deterministic
   `repair_handoff` mutation (`src/integration/candidates.py:755-890`). The
   external token/ref/push path begins only after commit
   (`src/integration/candidates.py:683-703`,
   `src/integration/candidates.py:1889-1922`), so rollback cannot expose a
   collector transfer without the mutation blocker. Applied and expected-old
   observations still converge through the existing exact remote reconciliation
   path, while unexpected remote remains reserved. The crash regression pauses
   at the transfer-commit boundary, proves stage/ordinary-transfer/rebuild are
   blocked, then constructs a fresh service and finishes revision N
   (`tests/test_integration_candidates.py:1823-1864`).

3. **The production mutating confirmer must canonical-reread and succeed on the
   first attempt while retaining negative binding — ADDRESSED.** Confirmation
   occurs outside SQL, after which `confirm_transfer()` locks and rereads the
   exact owner/fence. For a newly released row it requires cleared
   session/workspace and the original workspace in
   `confirmed_workspace_id`; for pending state it requires the original
   attachment fields (`src/integration/ownership.py:172-226`).
   `transfer_confirmed_on()` then compares the full canonical owner, role,
   fence, state, attachment, and confirmed-workspace snapshot before transfer
   (`src/integration/ownership.py:228-265`). Tests use a production-shaped
   callback that mutates the row, cover first-attempt success, reject a
   mismatched newly released workspace, and cover canonical released replay
   (`tests/test_integration_ownership.py:128-240`).

4. **Downgrade must refuse live collector-handoff provenance before DDL —
   ADDRESSED.** `46f910d0dce6.downgrade()` queries for either non-null handoff
   field and raises a deliberate diagnostic before entering the batch DDL
   (`migrations/versions/46f910d0dce6_candidate_handoff_workspace_.py:141-163`).
   The SQLite and PostgreSQL migration cycles retain safe handoff-null U-D-U,
   seed a non-null pair, assert refusal, and reread the intact fields
   (`tests/test_migration_candidate_mutation_claims.py:240-294`,
   `tests/test_migration_candidate_mutation_claims.py:297-377`).

## New breakage in the fix diff

1. **Important — every concurrent repair-acceptance replay adopts the same
   mutation nonce as push authority.** Each call to
   `_reserve_repair_handoff()` unconditionally stores the canonical row's nonce
   in its private `state['adopted_mutations']`, including callers that merely
   reread a reservation inserted by another still-running call
   (`src/integration/candidates.py:883-890`). `_mutate_ref()` then treats a
   matching adopted nonce exactly like insertion ownership
   (`src/integration/candidates.py:1879-1887`), and `_prepush_authorized()` only
   checks that shared nonce plus the common lease/authority
   (`src/integration/candidates.py:1964-1998`). Two duplicate acceptance calls
   can therefore serialize through the handoff transaction, both read the same
   expected remote, both pass pre-push authorization, and both initiate the
   force-with-lease push. One loses the remote lease and escapes as a Git error,
   or, if both observe the desired tip, one later loses the acceptance CAS as an
   uncaught `CandidateStaleAuthority` (`src/integration/candidates.py:704-747`).
   This violates the fix brief's requirement that a uniqueness race return
   canonical state/wait and the Task 9b1 single-accept/replay contract. The
   crash test exercises one fresh caller only and has no overlapping adopter
   (`tests/test_integration_candidates.py:1823-1864`). Minimal remediation:
   make live execution ownership itself an atomic, exclusive durable claim.
   A canonical reader that did not win that claim may observe/reconcile but
   must return wait rather than push; takeover must be a CAS after the prior
   executor is safely stale (or an equivalent one-writer protocol). Also catch
   a losing exact remote CAS by authoritative observation and prove two
   overlapping fresh-service acceptance calls produce one push and one
   canonical wait/replay, never a raw exception.

## Out-of-scope observations

The pre-existing SQLite-to-PostgreSQL copy-adapter omission remains Task 12
scope and is unchanged by this fix.

## Test and report checks

- The fix report names focused RED/GREEN coverage for all four prior findings,
  records a final affected-area result of 97 passed, and records two passing
  migration tests across SQLite and PostgreSQL. Those reported suites were not
  rerun, per the scoped instruction.
- No focused test was run. The concurrent-adoption failure follows directly
  from two callers receiving the same stored nonce and the submitted suite has
  no overlapping acceptance invocation.
- The report's claims about expiry-independent invalidation, atomic handoff
  persistence, production-shaped confirmation, and downgrade refusal match the
  committed code. Its claim that canonical nonce adoption provides a
  single-executor uniqueness race is not supported by a CAS or test.

## Quality verdict

The four targeted lifecycle corrections are implemented with clear local
transactions and materially stronger interleaving coverage. The new conn-owned
reservation helper also centralizes identity comparison well. However, nonce
adoption converts an immutable intent identity into a reusable executor
capability, leaving overlapping replay non-idempotent at the external push and
final acceptance boundaries.

## Verdict

**FAIL — new Important breakage remains.** Open counts: **Critical 0,
Important 1, Minor 0**.
