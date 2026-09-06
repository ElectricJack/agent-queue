# Task 10a implementation report

## Status and provenance

- Logical implementation base requested by the controller: `df2bfc0d`.
- The shared branch advanced through documentation-only commits while this phase began:
  `c686eda3`, `5a22bced`, and `cbec0753`. They were preserved without reset, rebase, or
  revert.
- Runtime/tests/migration commit: `ed12434d` (`feat(integration): add bounded reconciliation service`).
- Queue discovery: `aq prime` returned `task_id is required`; no queue mutation was attempted.
- Functionality remains disabled/dormant where Task 10b/10c handlers do not yet exist.

## Delivered scope

### Schedule catch-up and frozen policy

- Added the nullable all-or-none `catchup_trigger`, `catchup_requested_at`, and
  `catchup_after_sequence` tuple to `project_integration_schedules` with named constraint
  `ck_project_integration_schedules_catchup`.
- An outstanding request with a nonterminal nonempty train batch retains its request ID, first
  trigger, first timestamp, and sequence. The first later manual or due-periodic trigger stores
  catch-up provenance; later triggers replay it byte-stably and no second sweep event is emitted.
- Existing periodic elapsed-boundary arithmetic remains unchanged. Terminal empty sealing clears
  the request and catch-up tuple together.
- Added frozen `IntegrationCleanupPolicy` defaults and validation, plus
  `HierarchicalIntegrationPolicy.on_main_moved = "rebuild"`. Existing stored policies parse with
  these defaults, and newly sealed policy snapshots include the resolved values.

### Bounded durable selectors

- Added `IntegrationReconciliationQueriesMixin` with all four required keyset pages:
  due schedules; due current repair stages; current pending candidate-CI subjects; unresolved
  typed promotion intents.
- Each validates `limit > 0`, returns plain dictionaries, orders by the documented cursor tuple,
  and excludes disabled/stale/terminal rows.
- Composed the mixin into both SQLite and PostgreSQL adapters and exposed the methods on the
  database protocol.
- Replaced the unbounded implementation of `RepairService.due_stages` with a page-shaped wrapper
  over the selector (`after`, `limit=100`). Existing timeout behavior is unchanged.

### Integration service and outbox lifecycle

- Added one `IntegrationService` loop with a nonblocking local overlap guard and one bounded page
  per source in this order: schedules, repair deadlines, candidate CI, typed intents, dormant
  cleanup slot, then integration outbox.
- Schedule and deadline rows call the existing `IntegrationScheduler.mark_due` and
  `RepairService.expire` authorities. Optional later-phase handlers leave rows untouched and log
  bounded retryable blockers when absent.
- Per-item exceptions are isolated; `asyncio.CancelledError` propagates.
- `start()` owns one named task and `stop()` signals and awaits it deterministically.
- Orchestrator construction occurs after command/V2 playbook setup, and shutdown stops the
  integration service before database close. The service is not also driven by the orchestrator
  cycle.
- The outbox callback calls `V2PlaybookRuntime.accept_integration_event` directly. An absent V2
  runtime returns false, retaining the outbox event for retry. Existing frozen operation-route and
  crash-boundary behavior remains in the reviewed outbox/runtime implementation.

## TDD evidence

### RED

1. Catch-up tuple:

   ```text
   pytest -q tests/test_integration_schedule.py::test_missed_windows_and_manual_calls_coalesce_until_release
   FAILED: sqlalchemy.exc.NoSuchColumnError: Could not locate column ... 'catchup_trigger'
   1 failed
   ```

2. Compatibility policy defaults (the test was subsequently moved into the new phase-owned test
   file without changing its assertion):

   ```text
   pytest -q tests/test_integration_parent_completion.py::test_hierarchical_policy_uses_compatible_rebuild_and_cleanup_defaults
   FAILED: AttributeError: 'HierarchicalIntegrationPolicy' object has no attribute 'on_main_moved'
   1 failed
   ```

3. Selector/service seam:

   ```text
   pytest -q tests/test_integration_service.py::test_due_schedule_keyset_pages_every_row_once_past_two_hundred
   ERROR: ModuleNotFoundError: No module named 'src.integration.service'
   ```

4. Orchestrator lifecycle:

   ```text
   pytest -q tests/test_orchestrator.py::test_orchestrator_owns_single_integration_service_loop
   FAILED: AttributeError: 'Orchestrator' object has no attribute 'integration_service'
   1 failed
   ```

### GREEN milestones

```text
pytest -q tests/test_integration_schedule.py::test_missed_windows_and_manual_calls_coalesce_until_release
1 passed

pytest -q tests/test_integration_parent_completion.py::test_hierarchical_policy_uses_compatible_rebuild_and_cleanup_defaults
1 passed

pytest -q tests/test_integration_service.py::test_due_schedule_keyset_pages_every_row_once_past_two_hundred \
  tests/test_integration_service.py::test_tick_is_bounded_nonoverlapping_and_isolates_sources \
  tests/test_integration_service.py::test_tick_keeps_work_without_later_phase_handlers_retryable
3 passed

pytest -q tests/test_integration_service.py::test_reconciliation_pages_select_only_current_work_and_keep_intent_kind
1 passed

pytest -q tests/test_integration_service.py::test_all_reconciliation_keysets_page_past_two_hundred_rows
1 passed

pytest -q tests/test_orchestrator.py::test_orchestrator_owns_single_integration_service_loop
1 passed

pytest -q tests/test_integration_service.py
9 passed

pytest -q tests/test_integration_schedule.py
7 passed

pytest -q tests/test_integration_repair.py
34 passed
```

The keyset tests use page sizes 7 and 9 and prove more than 200 unique rows are returned exactly
once for schedules, repair stages, candidate-CI subjects, and typed intents.

## Migration evidence

- `python3 -m alembic heads` before generation: `e9b2f1b7c3d5 (head)`.
- Generated, rather than hand-selected:

  ```text
  python3 -m alembic revision -m 'integration schedule catchup policy'
  Generating .../ed46f4aec7be_integration_schedule_catchup_policy.py ... done
  ```

- New revision: `ed46f4aec7be`, down revision `e9b2f1b7c3d5`.
- The first bare `alembic` launcher was unusable on this host and `python` was absent; generation
  used the installed `python3 -m alembic` module without touching a database.
- The first fresh-empty SQLite test exposed an older hierarchy-migration bootstrap assumption;
  the test was corrected to use the repository's established migration-test pattern: initialize
  current head, downgrade to the prior revision, seed, and exercise the new revision.
- The first PostgreSQL seed used integer `1` for a boolean and failed with
  `DatatypeMismatchError`; the dialect-neutral seed was corrected to `TRUE`.
- Fresh final dual-dialect evidence:

  ```text
  POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/postgres' \
    pytest -q -m migration tests/test_migration_integration_service.py
  2 passed, 2 warnings in 2.90s
  ```

  This covers SQLite and a uniquely named disposable PostgreSQL database through current-head
  initialization, downgrade to `e9b2f1b7c3d5`, legacy-row upgrade with NULL catch-up defaults,
  seeded live-catch-up downgrade refusal naming `project-z`, drain, downgrade preserving the
  outstanding request identity, and re-upgrade. The PostgreSQL scratch database was dropped in
  test cleanup.

- Final head check: `ed46f4aec7be (head)`.

## Required affected-area gate and checks

```text
aq test tests/test_integration_schedule.py tests/test_integration_service.py \
  tests/test_integration_outbox.py tests/test_integration_repair.py tests/test_orchestrator.py -x
136 passed, 11 warnings in 48.12s

ruff check src/database/tables.py src/database/base.py \
  src/database/queries/integration_reconciliation_queries.py \
  src/database/adapters/sqlite.py src/database/adapters/postgresql.py \
  src/integration/models.py src/integration/scheduler.py src/integration/repair.py \
  src/integration/service.py src/orchestrator/core.py tests/test_integration_schedule.py \
  tests/test_integration_service.py tests/test_orchestrator.py \
  tests/test_migration_integration_service.py \
  migrations/versions/ed46f4aec7be_integration_schedule_catchup_policy.py
All checks passed!

python3 -m compileall -q src/database/queries/integration_reconciliation_queries.py \
  src/integration/service.py src/integration/scheduler.py src/integration/models.py \
  src/integration/repair.py src/orchestrator/core.py \
  migrations/versions/ed46f4aec7be_integration_schedule_catchup_policy.py
exit 0

git diff --check
exit 0
```

An exploratory full `tests/test_integration_parent_completion.py` run reported 2 failures and 22
passes: both failing historical tests directly mutate `task_delivery_receipts`, which the reviewed
Task 9b2 dependency now correctly rejects as append-only. Task 10a does not modify that test file
or receipt authority, and its moved policy-default test and the prescribed affected-area gate are
green.

## Files

- `migrations/versions/ed46f4aec7be_integration_schedule_catchup_policy.py`
- `src/database/adapters/postgresql.py`
- `src/database/adapters/sqlite.py`
- `src/database/base.py`
- `src/database/queries/integration_reconciliation_queries.py`
- `src/database/tables.py`
- `src/integration/models.py`
- `src/integration/repair.py`
- `src/integration/scheduler.py`
- `src/integration/service.py`
- `src/orchestrator/core.py`
- `tests/test_integration_schedule.py`
- `tests/test_integration_service.py`
- `tests/test_migration_integration_service.py`
- `tests/test_orchestrator.py`

## Deliverable reconciliation and self-review

- Catch-up tuple, coalescing, arithmetic, replay, and empty consumption: delivered once in the
  schedule schema/query/service seam.
- Four bounded keyset selectors and bounded repair compatibility wrapper: delivered once in the
  reconciliation query mixin and `RepairService`.
- One service loop, local overlap guard, source ordering, item isolation, restart-from-rows,
  missing-handler blockers, and deterministic lifecycle: delivered once in `IntegrationService`.
- Direct V2 integration outbox acceptance and absent-runtime retry: wired once in orchestrator;
  no EventBus acknowledgement was added.
- Orchestrator start/stop ordering: delivered once; no second cycle tick exists.
- SQLite/PostgreSQL migration and downgrade guard: delivered by the generated single-head
  revision and dedicated migration test.
- No candidate construction, provider/Git calls, CI trust implementation, promotion, release,
  cleanup action/table, commands, workflow/playbook prose, enablement, or external writes were
  added.
- External side effects in a service tick remain inside pre-existing domain authorities. The new
  selectors are read-only, service state is only an overlap/lifecycle guard, and cross-process
  correctness remains durable CAS/dedup behavior.

## Concerns

- Candidate-CI, unresolved-intent, and cleanup handlers intentionally remain unattached pending
  Task 10b/10c; selected rows are retained and warnings are bounded by page size.
- The two stale receipt-mutation tests noted above are inherited from the reviewed Task 9b2
  append-only change and are not a Task 10a runtime regression.

## Fix round 1 — scheduler catch-up and service liveness

### Provenance and review scope

- Logical fix base: `6c4cc883`.
- While the four scoped files were uncommitted, the shared branch advanced to documentation-only
  commit `f29ea277` (`docs: record service liveness review fixes`). It was preserved without reset,
  rebase, or revert, and the fix was committed on top of it.
- Runtime/tests commit: `33460757` (`fix(integration): preserve reconciliation progress`).
- No schema, migration, Task 10b/10c, provider, Git, or external-write behavior changed.

### TDD evidence

The post-promotion catch-up regression was added first and failed for both trigger orderings:

```text
pytest -q tests/test_integration_schedule.py::test_promoted_pending_release_preserves_first_catchup
2 failed, 3 warnings
```

Both failures showed the persisted catch-up tuple remaining NULL after a manual-first or
periodic-first trigger against the actual terminal representation
`lifecycle='promoted', cleanup_state='pending'`.

The service fairness and failure-resilience regressions were then added and failed before the
runtime edit:

```text
pytest -q \
  tests/test_integration_service.py::test_tick_advances_persistent_source_cursor_and_wraps_fairly \
  tests/test_integration_service.py::test_selector_failure_isolates_source_and_background_loop_recovers_cleanly
3 failed, 2 warnings
```

The missing and declining handler cases repeatedly selected `['a', 'b']`, starving later rows;
the one-shot selector exception terminated the named service task, prevented recovery, and was
re-raised by `stop()`.

Focused GREEN evidence:

```text
pytest -q tests/test_integration_schedule.py::test_promoted_pending_release_preserves_first_catchup
2 passed, 3 warnings in 1.23s

pytest -q \
  tests/test_integration_service.py::test_tick_advances_persistent_source_cursor_and_wraps_fairly \
  tests/test_integration_service.py::test_selector_failure_isolates_source_and_background_loop_recovers_cleanly
3 passed, 2 warnings in 0.34s

pytest -q tests/test_integration_schedule.py
9 passed, 3 warnings in 1.90s

pytest -q tests/test_integration_service.py
12 passed, 2 warnings in 1.28s
```

### Fix details and acceptance

- `IntegrationScheduler.mark_due` now treats a nonempty promoted batch with pending cleanup as
  active for first-catch-up capture. Manual-first and periodic-first production scheduler tests
  prove the original request ID, trigger, timestamp, and sequence remain unchanged; the first
  catch-up tuple is retained; later triggers cannot replace it; and only one sweep event exists.
- `IntegrationService` maintains an independent keyset cursor per source. It advances each cursor
  from the last selected row before handlers run, so missing, declining, or failing handlers
  cannot starve rows beyond the first page. An exhausted page wraps to the beginning without
  treating the in-memory cursor as a durable correctness queue.
- Selection and processing are isolated per source, so a source exception does not prevent later
  sources or outbox dispatch in the same tick. The outer loop logs and retries unexpected
  non-cancellation failures; `CancelledError` still propagates and deterministic `stop()` remains
  clean. The production loop regression proves a one-shot selector failure, later-source progress,
  a subsequent healthy tick, and clean shutdown.

### Final affected-area gate and checks

```text
aq test tests/test_integration_schedule.py tests/test_integration_service.py \
  tests/test_integration_outbox.py tests/test_integration_repair.py tests/test_orchestrator.py -x
141 passed, 11 warnings in 46.77s

ruff check src/integration/scheduler.py src/integration/service.py \
  tests/test_integration_schedule.py tests/test_integration_service.py
All checks passed!

python3 -m compileall -q src/integration/scheduler.py src/integration/service.py \
  tests/test_integration_schedule.py tests/test_integration_service.py
exit 0

git diff --check
exit 0
```

The 11 gate warnings are inherited dependency warnings, unchanged by this fix:

- `DeprecationWarning` at `src/_compat.py:111`: `pkg_resources is deprecated as an API`
  (reported once per worker/import context).
- `DeprecationWarning` from `pkg_resources/__init__.py:2871`: deprecated
  `pkg_resources.declare_namespace('zope')`; PEP 420 native namespace packages are preferred
  (reported once per worker/import context).
- `DeprecationWarning` from `discord/player.py:29`: `audioop` is deprecated and slated for removal
  in Python 3.13 (reported once per worker/import context).

### Fix-round self-review and concerns

- Cursor movement happens before item handling and remains source-local; selector failure leaves
  that source's prior cursor intact for retry. Cross-process correctness still lives in the
  durable selectors and domain authorities.
- The promoted catch-up condition is deliberately limited to `cleanup_state='pending'`; released
  batches do not capture into the old outstanding request.
- Cancellation is never swallowed by either isolation layer. No additional service loop or
  background driver was introduced.
- No new concern remains within the three accepted review findings. Task 10b/10c handler and
  release/cleanup work remains intentionally out of scope.
