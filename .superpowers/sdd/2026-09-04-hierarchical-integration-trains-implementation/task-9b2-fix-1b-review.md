# Task 9b2 fix 1b implementation review

## Spec Compliance

- **PASS — Critical 0, Important 0, Minor 1.** The phase closes the original complete-member finalizer and SQLite receipt-trigger findings, adds the required terminal handoff state/events, and introduces no scoped Critical or Important regression.

## Finding Verdicts

1. **The root finalizer must verify the complete exact immutable batch/revision member set before any receipt, event, or terminal state write.** — **ADDRESSED.** The new migration gives each reservation exact composite `RESTRICT` bindings to its root intent, sealed member, candidate result, and review subject, with the necessary referenced unique keys (`migrations/versions/e9b2f1b7c3d5_immutable_root_receipts.py:24-66`, `migrations/versions/e9b2f1b7c3d5_immutable_root_receipts.py:269-341`). The finalizer takes the hierarchy lock and prescribed row locks, then rejects missing/extra/noncontiguous members, results, reservations, or receipts and compares every task/repository/head/tree/review/result/squash/evidence identity before its first insert (`src/integration/main_promotion.py:762-1030`). Existing receipts must be absent or byte-identical, and any extra batch/revision receipt aborts (`src/integration/main_promotion.py:1032-1077`).

2. **SQLite migration must preserve delivery-receipt append-only UPDATE/DELETE guards across upgrade and downgrade, with PostgreSQL parity.** — **ADDRESSED.** The migration explicitly drops and recreates both receipt guards around its schema changes for each dialect (`migrations/versions/e9b2f1b7c3d5_immutable_root_receipts.py:184-218`, `migrations/versions/e9b2f1b7c3d5_immutable_root_receipts.py:344-390`). It likewise preserves the root-reservation guards around SQLite table recreation and repairs the PostgreSQL JSON-bearing root/prepared guard functions without relaxing their terminal/identity policy (`migrations/versions/e9b2f1b7c3d5_immutable_root_receipts.py:221-267`). The downgrade refuses before DDL while any root intent, reservation, root-main claim, or root receipt remains (`migrations/versions/e9b2f1b7c3d5_immutable_root_receipts.py:365-378`).

3. **Root success must atomically persist all receipts, authenticated final-main proof, promoted/pending-cleanup terminal state, completed operation/stage, committed intent, and stable operation-bound events.** — **ADDRESSED.** Finalization requires the current pushed root intent, promoting batch/current green candidate, exact aggregate evidence, current operation/stage, frozen lease/collector fences, applied root-main claim, and matching authenticated remote proof (`src/integration/main_promotion.py:929-995`). Receipt insertion, one `integration.root_delivered` event per ordinal, candidate/batch/stage/operation CASes, `integration.batch_promoted`, `integration.cleanup_requested`, and the intent CAS all occur in the same immediate transaction; every terminal CAS must affect exactly one row or the transaction rolls back (`src/integration/main_promotion.py:1032-1185`). Batch state is terminal `promoted` with `final_main_sha=remote` and independent `cleanup_state='pending'` (`src/integration/main_promotion.py:1107-1125`). The three exact payload shapes are registered and require the actual operation ID through the shared integration schema (`src/event_schemas.py:1099-1131`, `src/event_schemas.py:1287-1339`). No provider, Git, cleanup, or post-main CI operation occurs inside the finalizer transaction.

## New Breakage in the Fix Diff

- None.

## Minor Notes

1. **The migration tests do not fully realize the brief's dual-dialect negative matrix.** All six legacy incompatibility cases run on SQLite, but PostgreSQL covers only cross-intent identity (`tests/test_migration_root_main_promotion.py:268-306`, `tests/test_migration_root_main_promotion.py:396-439`). The U-D-U tests assert receipt immutability before downgrade and after re-upgrade, but not at the intermediate downgraded `d4a81f0c9e72` state (`tests/test_migration_root_main_promotion.py:309-340`, `tests/test_migration_root_main_promotion.py:344-383`). The migration code itself installs the correct constraints/guards, so this is nonblocking test-evidence debt. Extend the parameterized member/result/review/evidence cases to PostgreSQL and assert UPDATE/DELETE failure immediately after downgrade on both dialects.

## Checks

- Read the fix1b brief/report and supplied `review-6e307fd8..34f21559.diff` package. Inspected the current migration, table metadata, finalizer, event registry/outbox helper, and focused tests only at the named schema/finalization seams.
- Did not rerun reported tests. The report records 251 affected-area tests, SQLite and PostgreSQL migration cycles, PostgreSQL public finalization/replay, Ruff, compile, Alembic-head, and diff checks; the claims match the committed code and tests. Existing dependency warnings and one documented skip are carried process evidence, not new fix1b breakage.

## Quality Verdict

- **Good.** The schema and runtime checks reinforce each other, finalization is atomic and replay-stable, and dialect-specific trigger handling is explicit. The remaining Minor is limited to strengthening required migration-negative coverage.
