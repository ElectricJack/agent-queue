# Task 9b2 fix 1b report

## Identity and scope

- Assigned base: `bb16874e420853d8e1c020cb1321031647c6adc7`.
- While implementation was in progress, the shared branch advanced through controller-owned,
  documentation-only commits `9f697477`, `21e6346f`, and `6e307fd8`. They were preserved. The
  nearest preceding documentation head and runtime review base is therefore
  `6e307fd89237ad28cd88dd692285a97ee794783a`.
- Runtime/tests/migration commit:
  `a0115bd1005f9b51a0570939048e9e37794278d4` (`feat(integration): finalize immutable root receipts`).
- Scope is only Task 9b2 fix 1b: immutable root reservation/receipt schema, root finalization,
  registered terminal handoff events, and focused tests.
- No Task 10 consumer/cleanup implementation, post-main CI, Git/provider/network activity,
  operator database mutation, queue mutation, workspace creation, push, or PR work was performed.

## Delivered schema and migration

New Alembic head `e9b2f1b7c3d5` follows `d4a81f0c9e72` and adds:

- non-partial unique referenced identities for the exact root intent, sealed batch member,
  candidate member result, and review-evidence subject;
- `RESTRICT` composite foreign keys from every root-member reservation to all four exact
  identities;
- the partial unique root receipt tuple `(batch_id, candidate_revision, member_ordinal)` when
  `batch_id IS NOT NULL`;
- a pre-DDL compatibility guard rejecting the first cross-intent reservation, sealed-member
  mismatch, non-applied/mismatched result, review-subject mismatch, JSON evidence drift, duplicate
  root receipt, or receipt without its exact reservation;
- a downgrade precondition that refuses any retained root intent, reservation, root-main claim, or
  root receipt, then restores the exact d4 foreign-key shape only after the history is drained;
- explicit recreation of both root-reservation and delivery-receipt append-only UPDATE/DELETE
  guards across SQLite batch alteration and PostgreSQL migration cycles.

The PostgreSQL cycle exposed two existing d4 functions that used `IS DISTINCT FROM` directly on
`json` values, which PostgreSQL cannot compare. This head recreates the terminal-root function with
the same fail-closed terminal policy and recreates prepared-identity comparisons with text-stable
JSON comparisons. This is a dialect repair required for the new migration and finalizer to execute;
it does not relax either immutability contract.

## Delivered root finalization

`RootPromotionService._finalize_root` now performs one `db.immediate()` transaction with the
prescribed lock sequence: hierarchy project, project, batch, candidate revision, lease,
operation/stage, integration owner, intent, root-main claim, ordered sealed members, ordered
candidate results, ordered reservations, review evidence, then matching/existing root receipts.
There is no provider or Git await inside this transaction.

Before writing, it requires the current promoting batch, green exact tested candidate and aggregate
CI evidence, current awaiting-completion batch operation/stage, frozen lease and collector fences,
applied authenticated root-main claim, authenticated final-main observation, non-empty equal member
sets, contiguous exact ordinals, complete candidate cursor, ordinal-zero receipt identity, and exact
task/repository/head/tree/review/result/squash/evidence identity for every member. Existing receipts
must be absent or byte-identical, and no extra batch/revision receipt may exist.

Receipts use reservation time and complete explicit nullable fields, so replay is byte-stable. The
candidate, batch, stage, operation, and intent terminal transitions are each one-row CAS operations;
any loss rolls the transaction back. Success records candidate `promoted`, batch
`lifecycle='promoted'`, authenticated `final_main_sha`, independent `cleanup_state='pending'`, stage
`passed`, operation `completed`, and intent `committed`.

The finalizer emits through `enqueue_integration_event` with stable existing IDs and the actual
root operation ID:

- one `integration.root_delivered` per ordered member;
- one `integration.batch_promoted`;
- one `integration.cleanup_requested`.

All three event types have registered exact payload schemas. No cleanup action or post-main CI is
performed.

## TDD evidence

### Schema and migration RED/GREEN

The first schema RED was intentionally narrow:

```text
pytest -q -m migration tests/test_migration_root_main_promotion.py::test_sqlite_root_promotion_schema_and_guarded_round_trip
FAILED ... missing uq_integration_promotion_intents_root_identity,
uq_integration_batch_members_root_identity, uq_integration_candidate_results_root_identity,
and uq_integration_review_evidence_root_identity
1 failed, 2 warnings in 0.36s
```

An initial invocation omitted `-m migration`; repository marker defaults deselected the only node
and returned pytest exit 5. It was corrected rather than treated as evidence.

SQLite GREEN after the migration, structural constraints, compatibility guards, downgrade guard,
and append-only trigger preservation:

```text
pytest -q -m migration tests/test_migration_root_main_promotion.py -k sqlite -x
7 passed, 1 deselected, 2 warnings in 1.33s
```

PostgreSQL iteration found three concrete test/runtime issues rather than weakening the schema:

1. the compatibility test's nested transaction was oriented incorrectly (`1 failed`), fixed by
   making the expected upgrade failure own the savepoint;
2. the inherited terminal-root trigger attempted row-wide JSON equality (`1 failed`), fixed with
   an equivalent strict terminal-state function;
3. the inherited prepared-identity trigger compared PostgreSQL JSON directly (`1 failed`), fixed
   with text-stable JSON comparisons while retaining every immutable column.

The focused PostgreSQL GREEN was:

```text
POSTGRES_TEST_DSN=postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/aq_t9b2_fix1b_sol_20260905a pytest -q -m migration tests/test_migration_root_main_promotion.py -k postgres -x
2 passed, 7 deselected, 2 warnings in 4.92s
```

### Event and public finalizer RED/GREEN

Event-schema RED:

```text
pytest -q tests/test_integration_contracts.py -k 'all_design_events or hierarchy_event_payloads'
FAILED ... integration.root_delivered / integration.batch_promoted /
integration.cleanup_requested were not registered
1 failed, 1 passed, 8 deselected, 3 warnings
```

Event-schema GREEN:

```text
pytest -q tests/test_integration_contracts.py -k 'all_design_events or hierarchy_event_payloads'
2 passed, 8 deselected, 3 warnings in 0.87s
```

Public finalizer RED:

```text
pytest -q tests/test_integration_main_promotion.py -k 'exact_tested_sha_main_push_finalizes_every_member_without_post_ci or public_root_finalization_rejects_incomplete_frozen_member_set' -x
FAILED ... batch lifecycle was cleanup_pending rather than terminal promoted
1 failed, 37 deselected, 3 warnings in 1.38s
```

Public finalizer GREEN after complete-set checks, immutable receipts, terminal CASes, and canonical
events:

```text
pytest -q tests/test_integration_main_promotion.py -k 'exact_tested_sha_main_push_finalizes_every_member_without_post_ci or public_root_finalization_rejects_incomplete_frozen_member_set' -x
3 passed, 37 deselected, 3 warnings in 1.73s

pytest -q tests/test_integration_main_promotion.py::test_public_root_finalization_rolls_back_every_lost_terminal_cas
5 passed, 3 warnings in 2.19s

pytest -q tests/test_integration_main_promotion.py -x
40 passed, 3 warnings in 9.85s
```

The existing real public crash regression
`test_mid_receipt_crash_rolls_back_entire_root_finalization` exercises the crash immediately after
ordinal zero, observes zero receipts/events and no terminal state, then recovers without a second
main push. The expanded success/replay test proves two complete ordered receipts, four exact
operation-bound events, terminal promoted/pending state, and byte-identical replay.

PostgreSQL public finalization/replay GREEN, using the actual database adapter and full service path:

```text
POSTGRES_TEST_DSN=postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/aq_t9b2_fix1b_sol_20260905b pytest -q tests/test_integration_main_promotion.py::test_postgres_root_finalization_replay_is_atomic_and_operation_bound
1 passed, 3 warnings in 3.37s
```

## Final verification

Final SQLite migration cycle:

```text
pytest -q -m migration tests/test_migration_root_main_promotion.py -x
7 passed, 2 skipped, 2 warnings in 4.90s
```

Final combined SQLite/PostgreSQL upgrade-downgrade-upgrade and compatibility cycle, on unique
database family `aq_t9b2_fix1b_sol_20260905c`:

```text
POSTGRES_TEST_DSN=postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/aq_t9b2_fix1b_sol_20260905c pytest -q -m migration tests/test_migration_root_main_promotion.py -x
9 passed, 2 warnings in 6.35s
```

The scratch family was explicitly dropped after the run; the survivor query returned `[]`. The
shared `postgres`, `integration_test`, worker, and operator databases were not migrated.

The one prescribed affected-area gate was:

```text
aq test tests/test_integration_main_promotion.py tests/test_integration_candidates.py tests/test_integration_ci.py tests/test_integration_promotion.py tests/test_integration_contracts.py tests/test_integration_ownership.py tests/test_integration_repair.py -x
aq test: slot 1 of 2, -n 3
251 passed, 1 skipped, 11 warnings in 57.84s
```

Final static verification:

```text
ruff check src/database/tables.py src/event_schemas.py src/integration/main_promotion.py migrations/versions/e9b2f1b7c3d5_immutable_root_receipts.py tests/test_integration_contracts.py tests/test_integration_main_promotion.py tests/test_migration_root_main_promotion.py
All checks passed!

/usr/bin/python3.12 -m compileall -q src/database/tables.py src/event_schemas.py src/integration/main_promotion.py migrations/versions/e9b2f1b7c3d5_immutable_root_receipts.py tests/test_integration_contracts.py tests/test_integration_main_promotion.py tests/test_migration_root_main_promotion.py
exit 0, no output

/usr/bin/python3.12 -m alembic heads
e9b2f1b7c3d5 (head)

git diff --check -- src/database/tables.py src/event_schemas.py src/integration/main_promotion.py migrations/versions/e9b2f1b7c3d5_immutable_root_receipts.py tests/test_integration_contracts.py tests/test_integration_main_promotion.py tests/test_migration_root_main_promotion.py
exit 0, no output
```

Warnings are the pre-existing `pkg_resources`, namespace-package, and `audioop` deprecations.

## Files and self-review

- `migrations/versions/e9b2f1b7c3d5_immutable_root_receipts.py`: compatibility/downgrade
  preconditions, exact structural bindings, receipt uniqueness, and dialect trigger preservation.
- `src/database/tables.py`: head metadata mirrors the migration's exact keys and foreign keys.
- `src/integration/main_promotion.py`: complete hierarchy-locked root finalizer and canonical event
  emission.
- `src/event_schemas.py`: registered typed terminal handoff events.
- `tests/test_migration_root_main_promotion.py`: SQLite/PG U-D-U, pre-DDL refusal, exact schema, and
  append-only trigger proof.
- `tests/test_integration_main_promotion.py`: public-path success/replay, PostgreSQL atomic replay,
  incomplete/evidence mismatch rejection, and every terminal CAS rollback.
- `tests/test_integration_contracts.py`: exact event registration and payload contracts.

Self-review confirmed that all member validation and existing-receipt comparison precedes any write;
the ordinal-zero crash and every terminal CAS failure roll back receipts, events, and state together;
all database locks are acquired in the prescribed hierarchy-to-receipt order; the external push and
authenticated observation remain outside the transaction; and the finalizer neither runs cleanup
nor requests another CI cycle. Child delivery behavior is unchanged and remains covered by the
251-test affected-area gate.

No unresolved concern remains within Task 9b2 fix 1b scope. The only observed warnings are the
deferred dependency deprecations above.
