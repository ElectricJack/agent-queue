# Task 8b — Atomic full-frontier sealing report

## Status

Complete and still disabled. The implementation adds the caller-transaction-owned
eligibility/review projections, atomic `TrainService.seal`, typed `integration_seal`
authority boundary, real Task 7a batch-operation reservation, and cross-dialect
sealing-versus-descendant-reopen coverage. No Git/ref/forge operation, candidate
construction, operator database mutation, activation, or Task 9 behavior was added.

Base was verified clean at `44018637` before edits. Runtime and tests are committed as
`96771800` (`feat: seal integration train frontier atomically`).

## Implementation

- `src/database/queries/integration_train_queries.py`
  - Adds a conn-owned `(task_id, source_head)` keyset page over the locked project
    frontier and a conn-owned bulk newest-exact-review lookup.
  - Uses the exact leaf/current-parent identities and all Task 6 completion joins from
    the Task 8 preflight.
  - Applies structural-root, completed status, designated repository, live origin,
    checkpoint, PR URL, hold, gate, active membership, and root-delivery exclusions.
- `src/database/base.py`, `src/database/adapters/sqlite.py`, and
  `src/database/adapters/postgresql.py`
  - Expose/compose the two query adapters on the shared `DatabaseBackend` protocol and
    both concrete adapters.
- `src/integration/scheduler.py`
  - Adds `TrainService.seal(project_id, request_id, now)` under one `db.immediate()` and
    project hierarchy lock.
  - Implements durable request replay, live-batch busy behavior without a frontier
    read, expired same-batch resume, full unbounded paging, resolver-based effective
    mode, newest exact approved review selection, terminal resource-free empty batches,
    immutable full member evidence, deterministic batch/branch/digest, real
    `reserve_batch_operation_on`, actual operation ID outbox emission, request
    consumption, and `sealing -> sealed` in that same transaction.
- `src/commands/contracts/integration.py` and `src/commands/integration_commands.py`
  - Register typed `integration_seal` arguments/results/outcomes and enforce the
    existing project-scoped integration authority policy. Session principals remain
    denied.
- `src/database/queries/task_queries.py`
  - Uses the existing `immediate()` transaction boundary for canonical status
    transitions. This is the smallest shared fix for a RED concurrency test: SQLite
    now takes its intended writer lock before the pre-state read, while PostgreSQL
    remains an ordinary transaction and uses the existing advisory project lock.
    Post-commit blocked/settled/ready callbacks are unchanged.
- `tests/test_integration_sealing.py` and `tests/test_integration_contracts.py`
  - Cover all phase invariants, the typed command inventory, and ordinary transition
    compatibility.

## RED evidence

1. Missing eligibility adapter:

   ```text
   $ pytest -q tests/test_integration_sealing.py -x
   FAILED ...test_keyset_pages_full_frontier_and_bulk_review_selects_latest_exact
   E AttributeError: 'SQLiteDatabaseAdapter' object has no attribute 'eligible_root_page_on'
   1 failed, 2 warnings
   ```

2. Missing service:

   ```text
   $ pytest -q tests/test_integration_sealing.py -k 'zero_root_seal or one_root_seal or live_batch' -x
   FAILED ...test_zero_root_seal_is_terminal_resource_free_and_request_replay
   E ImportError: cannot import name 'TrainService' from 'src.integration.scheduler'
   1 failed, 2 deselected
   ```

3. A common-predicate test exposed whitespace-only PR URLs passing `pr_url != ''`:

   ```text
   $ pytest -q tests/test_integration_sealing.py::test_root_projection_excludes_each_common_near_miss
   FAILED ... expected ['good']; got ['blank-pr', 'good']
   1 failed
   ```

   GREEN changed the SQL predicate to `trim(pr_url) != ''`.

4. Missing typed command:

   ```text
   $ pytest -q tests/test_integration_sealing.py::test_integration_seal_command_is_typed_and_project_scoped
   WARNING Unknown command requested: integration_seal ...
   FAILED ... KeyError: 'outcome'
   1 failed, 3 warnings in 0.93s
   ```

5. SQLite transaction ordering exposed the canonical transition seam:

   ```text
   $ pytest -q tests/test_integration_sealing.py -k 'seal_lock_excludes or reopen_before_seal' -x
   FAILED ...test_seal_lock_excludes_descendant_reopen_until_frozen[sqlite]
   E Failed: DID NOT RAISE HierarchyError
   1 failed, 10 deselected, 3 warnings in 1.04s
   ```

   Root/controller approved the narrow shared correction: `transition_task()` enters
   `immediate()` so SQLite locks before its pre-state read; PostgreSQL still relies on
   the existing scoped advisory hierarchy lock.

6. The final affected-area gate caught the stale implemented-command inventory:

   ```text
   $ aq test tests/test_integration_contracts.py tests/test_task_ready_event.py tests/test_hierarchy_queries.py tests/test_integration_schedule.py tests/test_integration_sealing.py -x
   FAILED ...test_unimplemented_integration_operations_are_not_registered
   E Extra items in the left set: 'integration_seal'
   1 failed, 80 passed, 7 skipped, 11 warnings in 21.11s
   ```

   The focused GREEN was `1 passed, 3 warnings in 0.74s` after adding only the newly
   implemented command to that inventory.

Two test-fixture defects surfaced while progressing from RED to GREEN and were corrected
without production workarounds: heterogeneous SQLAlchemy executemany mappings omitted
keys in later rows, and the full immutable review expectation initially omitted the
nullable `reviewer_session_attempt_id` column.

## Focused GREEN evidence

```text
$ pytest -q tests/test_integration_sealing.py -k 'keyset_pages or root_projection_requires'
2 passed, 2 warnings in 0.49s

$ pytest -q tests/test_integration_sealing.py::test_root_projection_excludes_each_common_near_miss
1 passed, 2 warnings in 0.54s

$ pytest -q tests/test_integration_sealing.py -k 'zero_root_seal or one_root_seal or live_batch' -x
3 passed, 2 deselected, 2 warnings in 0.79s

$ pytest -q tests/test_integration_sealing.py -k 'exhausts_small_pages or failure_after_first or immutable' -x
3 passed, 5 deselected, 2 warnings in 1.10s

$ pytest -q tests/test_integration_sealing.py::test_integration_seal_command_is_typed_and_project_scoped
1 passed, 3 warnings in 0.90s

$ pytest -q tests/test_integration_sealing.py -k 'seal_lock_excludes or reopen_before_seal' -x
2 passed, 2 skipped, 10 deselected, 3 warnings in 1.11s

$ pytest -q tests/test_integration_sealing.py::test_immediate_transition_preserves_ordinary_status_and_post_commit_callback
1 passed, 3 warnings in 0.84s
```

The >200 service test used 205 scanned roots with page size 7. It rejected the direct-mode
root and the root whose newest exact review was rejected, then persisted 203 members once
in stable ordinal order. A corrupt task-level mode inherited the valid project mode through
the real resolver.

The partial-member failure test injected a database trigger on ordinal 1. The first seal
rolled back batch, members, lease, repair operation, sealed outbox event, and request
consumption; after removing the trigger, replay produced exactly one batch, two members,
one operation, and one sealed event.

## PostgreSQL concurrency and cleanup

Only the confirmed admin DSN was used to create/drop the explicitly named disposable
databases `task8b_sol_atomic` and the test helper's per-worker
`task8b_sol_atomic_master`. Neither `postgres` nor `integration_test` was migrated or
reset.

```text
$ POSTGRES_TEST_DSN=postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/task8b_sol_atomic pytest -q tests/test_integration_sealing.py -k 'seal_lock_excludes or reopen_before_seal' -x
4 passed, 11 deselected, 3 warnings in 6.85s
```

The four arms prove on both SQLite and PostgreSQL that a descendant reopen started after
page one waits for sealing and then receives `HierarchyError(code='sealed')`, while a
reopen committed first invalidates the current parent proof and yields a fresh empty
snapshot.

After test teardown, the admin query was:

```text
SELECT datname FROM pg_database
WHERE datname = ANY(ARRAY['task8b_sol_atomic', 'task8b_sol_atomic_master']);
[]
```

## Required and final gates

Required Task 8b gate:

```text
$ aq test tests/test_integration_schedule.py tests/test_integration_sealing.py -x
aq test: slot 1 of 2, -n 3
20 passed, 2 skipped, 11 warnings in 4.90s
```

Final affected-area gate (query/seal/command plus the shared transition and callback
areas):

```text
$ aq test tests/test_integration_contracts.py tests/test_task_ready_event.py tests/test_hierarchy_queries.py tests/test_integration_schedule.py tests/test_integration_sealing.py -x
aq test: slot 1 of 2, -n 3
93 passed, 7 skipped, 11 warnings in 21.14s
```

Static checks:

```text
$ ruff check src/commands/contracts/integration.py src/commands/integration_commands.py src/database/adapters/postgresql.py src/database/adapters/sqlite.py src/database/base.py src/database/queries/integration_train_queries.py src/database/queries/task_queries.py src/integration/scheduler.py tests/test_integration_contracts.py tests/test_integration_sealing.py
All checks passed!

$ git diff --check
(no output; exit 0)
```

## Migration and compatibility evidence

No schema correction was needed. Task 8a already supplies request identity, empty batch
constraints, immutable member/review identity, lease constraints, batch-operation
uniqueness, and sealing immutability on the current Alembic head.

```text
$ if git diff --quiet 44018637 -- src/database/tables.py migrations; then echo 'schema/migrations unchanged from 44018637'; fi
schema/migrations unchanged from 44018637
```

The ordinary transition compatibility test confirms both persisted status and the
existing post-commit `task.ready` callback after the SQLite transaction-boundary fix.
The final affected-area gate covers the broader transition, settlement, and ready-event
behavior.

## Files and commits

Runtime/query files:

- `src/database/queries/integration_train_queries.py`
- `src/database/base.py`
- `src/database/adapters/sqlite.py`
- `src/database/adapters/postgresql.py`
- `src/database/queries/task_queries.py`
- `src/integration/scheduler.py`
- `src/commands/contracts/integration.py`
- `src/commands/integration_commands.py`

Tests:

- `tests/test_integration_sealing.py`
- `tests/test_integration_contracts.py`

Commits:

- `96771800 feat: seal integration train frontier atomically`
- Report commit: recorded after this file is committed.

## Self-review and concerns

- The cursor is advanced from the last scanned SQL row, never the last accepted Python
  member; all pages are exhausted before final sorting, ordinals, and digest.
- The review lookup returns the newest exact row regardless of verdict; eligibility is
  decided only after that selection, so an older approval cannot override a newer
  rejection.
- Busy detection occurs before route/artifact/frontier inspection and does not consume
  the outstanding request.
- Replay is keyed only by `(project_id, request_id)` and reads the durable operation ID;
  it never discovers a new frontier.
- Empty seals are durable terminal audits with no member, lease, operation, branch, or
  sealed event. Nonempty seals use the real reservation and a distinct operation ID.
- No API model changed, so OpenAPI/client regeneration was not required.
- No known functional concerns remain. PostgreSQL is intentionally exercised only for
  the required lock-order acceptance; the complete invariant matrix runs on SQLite and
  both adapters share the same SQLAlchemy query/service implementation.
