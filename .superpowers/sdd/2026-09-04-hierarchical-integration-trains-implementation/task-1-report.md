# Task 1 report — durable integration state and validated policy

## Result

Implemented the Task 1 durable persistence package on
`feat/hierarchical-integration-trains`.

- Added all fourteen integration-state tables, named check constraints,
  SQLite/PostgreSQL partial unique indexes, and audit-safe soft task
  references.
- Added frozen Pydantic values: `BranchKey`, `Fence`, `PromotionInput`,
  `PromotionValue`, `RequiredCheckSet`, and `RepairPolicy`.  An empty required
  check set is rejected.
- Added read-only projections `get_integration_checkpoint`,
  `get_integration_batch`, and `get_integration_operation` to both database
  adapters and the backend protocol.
- Added generated Alembic revision `3f30b34c7e7c` (`hierarchical integration
  state`).  Its schema guards make sealed batch membership and materialized
  branch origins immutable, and prevent repair attempt counters decreasing.
- Delivery receipts deliberately have no task foreign keys, so active task
  deletion cannot erase delivery proof.

## TDD evidence

1. Wrote `tests/test_integration_state.py` before the implementation and ran:
   `aq test tests/test_integration_state.py -x`.
   It failed at collection because `integration_batches` did not yet exist in
   `src.database.tables`.
2. Added the schema, values, query mixin, adapters, and generated migration.
3. Added a specific empty-`RequiredCheckSet` test and ran the focused suite;
   it failed because the value type allowed `names=()`.
4. Added `Field(min_length=1)` and reran the suite successfully.

## Verification

Executed successfully:

```text
aq test tests/test_integration_state.py -x
9 passed, 7 skipped

ruff check src/integration src/database/tables.py src/database/base.py \
  src/database/adapters/sqlite.py src/database/adapters/postgresql.py \
  src/database/queries/integration_state_queries.py \
  tests/test_integration_state.py \
  migrations/versions/3f30b34c7e7c_hierarchical_integration_state.py
All checks passed

git diff --check
```

The focused suite covers SQLite constraints, sealed-member/origin immutability,
all durable row round trips, receipt survival after actual task deletion, read
projections, and an uncached disposable SQLite upgrade that creates every new
table.

## Migration generation and review

Autogeneration used an explicit disposable SQLite connection through
`alembic.config.Config.attributes['connection']`; no worker or operator
database environment variable was changed and no operator database was
migrated.  The first scratch attempt incorrectly used `engine.begin()` and
failed in the historical hierarchy migration because that migration opens a
second connection. Re-running with `engine.connect()` (the repository's
documented transaction ownership) resolved the chain and generated revision
`3f30b34c7e7c`.

Alembic also detected unrelated legacy-to-V2 playbook drift already present in
the branch's migration graph. Those generated drop/recreate operations were
reviewed and removed; this revision contains only Task 1 tables/indexes plus
the required SQLite/PostgreSQL schema guards. This avoids folding unrelated
playbook migration work into the integration-state revision.

## Self-review and limitations

- All `CheckConstraint`s are explicitly named; SQLite and PostgreSQL partial
  predicates are identical.
- Policy/artifact/check evidence uses JSON; mutable ordering/progress uses
  scalar columns.
- The task's mutation APIs are intentionally not implemented: subsequent tasks
  own fenced writes and conditional-version updates. This task only exposes
  the required reads and accepts explicit caller-owned connections for those
  future mutation functions.
- The initial pass had no PostgreSQL DSN; Fix round 1 subsequently executed
  the same focused fixture against the supplied disposable PostgreSQL DSN.

## Fix round 1

Review found a PostgreSQL DDL failure and incomplete durable guards. The
scratch PostgreSQL run reproduced the exact root cause: Alembic rendered
`BOOLEAN DEFAULT 0` for `project_integration_schedules.enabled` (and the two
branch-origin boolean defaults), which PostgreSQL rejects. The migration now
uses `sa.false()` for all three defaults.

This round also:

- validates both the old and new batch on member updates, preventing a member
  moving from a sealed batch into a still-sealing one;
- adds narrow SQLite/PostgreSQL guards against generation/version, revision,
  fence, schedule-sequence, and outbox-attempt regression; freezes prepared
  promotion identity while allowing ordinary state/evidence updates; makes
  receipts append-only; and prevents deletion of materialized origins;
- adds `integration_candidate_member_results`, keyed by
  `(batch_id, revision, member_ordinal)`, retaining exact ordered input SHAs,
  generated squash SHA, result, and conflict evidence;
- makes active parent repair uniqueness project the parent alone rather than
  `(parent, episode)`.

Focused verification with the supplied disposable PostgreSQL DSN:

```text
POSTGRES_TEST_DSN=postgresql+asyncpg://… aq test tests/test_integration_state.py -x
28 passed

aq test tests/test_migration_boolean_defaults.py -x
1 passed
```

## Fix round 2

The unpublished Task 1 work was consolidated into its single final migration,
`3f30b34c7e7c`; no intermediate development schema is a deployment contract.
It freezes prepared intent fence owner/token and recovery ref, prevents
candidate progress and repair-operation stage regression, and adds composite
foreign keys from candidate-member results to both the candidate revision and
sealed member identity. Regression tests seed the real parent
batch/member/revision and prove orphan and ordinal-mismatch results fail on
SQLite and PostgreSQL.

Verification remained green against the supplied scratch PostgreSQL DSN:
`aq test tests/test_integration_state.py -x` (28 passed) and
`aq test tests/test_migration_boolean_defaults.py -x` (1 passed), plus Ruff
and `git diff --check`.

## Changed files

- `src/integration/__init__.py`
- `src/integration/models.py`
- `src/database/tables.py`
- `src/database/base.py`
- `src/database/queries/integration_state_queries.py`
- `src/database/adapters/sqlite.py`
- `src/database/adapters/postgresql.py`
- `migrations/versions/3f30b34c7e7c_hierarchical_integration_state.py`
- `tests/test_integration_state.py`
