# Task 8a implementation report

## Status and scope

Implemented the durable sweep schedule and sealing-schema foundation from
`task-8a-brief.md` on base `39040c75c5df9024b47a06e64273e0d40dea2fb1`.
The implementation commit is `77dc4ea7` (`feat: add durable integration sweep
scheduling`). No Task 8b frontier discovery, `TrainService.seal`, candidate
construction, Git/ref/forge mutation, daemon start, project enablement, or operator
database write was added.

The checkout was clean at the required base before edits:

```text
$ git status --short
$ git rev-parse HEAD
39040c75c5df9024b47a06e64273e0d40dea2fb1
```

## Deliverable reconciliation

- Added `IntegrationScheduler.mark_due(project_id, now, trigger)` with all arithmetic
  and writes in one `db.immediate()` transaction. The scheduler uses conn-owned query
  helpers and never opens an independent connection.
- New schedules are disabled and use the shipped 300-second interval. The narrow
  `IntegrationScheduler.configure` service method durably changes enablement and
  interval; an interval change sets `next_due_at = now + interval` without touching an
  outstanding request.
- A periodic tick advances directly to the first boundary strictly after `now`.
  Twelve windows ending at 3600 advance a 300-second schedule to 3900 without creating
  twelve requests.
- At most one outstanding request is retained. Periodic/manual replays preserve the
  first request ID, trigger, requested timestamp, and sequence. The transactionally
  deduplicated `integration.sweep_due` outbox fact uses the request ID as both event and
  dedup identity and carries `operation_id=request_id` for the existing event schema.
- Disabled periodic calls allocate nothing and do not advance the clock; manual calls
  remain permitted. A later call can allocate the next sequence only after the consumer
  releases the outstanding fields. No frontier is inspected by this phase.
- Registered a typed `integration_schedule_due` contract with only `periodic|manual`
  triggers and `due|not_due|coalesced|disabled` domain outcomes. The handler validates
  project existence, uses the existing integration authority convention, denies
  sessions, scopes playbooks by exact project and capability, and delegates policy-free
  to the scheduler.
- Added Alembic head `d8e9f0a1b2c3` directly after `c7d8e9f0a1b2` without amending an
  earlier revision.
- `integration_batches.request_id` is non-null and unique per project. The `empty`
  lifecycle requires null base/branch, while every non-empty lifecycle requires both.
  Empty batches reject members, repair operations, and leases, including the reverse
  insertion order where a soft-reference repair/lease pre-exists the batch row.
- SQLite and PostgreSQL triggers freeze project, repository, request, trigger, manifest,
  base, integration branch, policy/artifact snapshots, and creation time as a batch
  leaves sealing. Progress/result fields remain writable, and non-sealing rows cannot
  return to sealing.
- `integration_batch_members.review_evidence_id` is non-null with a RESTRICT FK to
  append-only `integration_review_evidence`; the full frozen JSON remains present.
  Existing member rows receive deterministic legacy evidence rows. Re-upgrade reuses
  only an exact matching evidence identity and fails closed on a collision.
- Existing member guards and batch revision guard are explicitly removed/recreated
  around SQLite batch-table rewrites. Task 7a's one-operation-per-batch uniqueness was
  not changed.

## TDD evidence

### Slice 1 — schema and migration

Initial SQLite RED:

```text
$ .venv/bin/pytest -q tests/test_integration_state.py -k 'batch_request_and_empty_structure or post_sealing_batch_identity or batch_member_requires_append_only or task8a_migration_cycle' -x
FAILED tests/test_integration_state.py::test_batch_request_and_empty_structure_are_database_invariants[sqlite]
E sqlalchemy.exc.CompileError: Unconsumed column names: request_id
1 failed, 30 deselected in 0.56s
```

Initial SQLite GREEN after metadata/revision/guards:

```text
$ .venv/bin/pytest -q tests/test_integration_state.py -k 'batch_request_and_empty_structure or post_sealing_batch_identity or batch_member_requires_append_only or task8a_migration_cycle' -x
4 passed, 3 skipped, 30 deselected in 5.71s
```

The first PostgreSQL run exposed the dialect-specific JSON comparison bug rather than
silently skipping the backend:

```text
$ POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/aq_task8a_sol_5e3a91c2' .venv/bin/pytest -q tests/test_integration_state.py -k 'batch_request_and_empty_structure or post_sealing_batch_identity or batch_member_requires_append_only'
FAILED tests/test_integration_state.py::test_post_sealing_batch_identity_is_frozen_and_cannot_return[postgres]
E asyncpg.exceptions.UndefinedFunctionError: operator does not exist: json = json
1 failed, 5 passed, 31 deselected in 8.08s
```

Casting PostgreSQL JSON snapshots to text in the immutability comparison produced the
cross-dialect GREEN:

```text
$ POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/aq_task8a_sol_5e3a91c2' .venv/bin/pytest -q tests/test_integration_state.py -k 'batch_request_and_empty_structure or post_sealing_batch_identity or batch_member_requires_append_only'
6 passed, 31 deselected in 11.98s
```

The SQLite c7/head/c7/head cycle was extended with a pre-upgrade member. Its first
re-upgrade RED found that the deterministic evidence row correctly survived downgrade
but needed exact replay reuse:

```text
$ .venv/bin/pytest -q tests/test_integration_state.py -k 'task8a_migration_cycle'
FAILED tests/test_integration_state.py::test_sqlite_task8a_migration_cycle_from_previous_head
E sqlite3.IntegrityError: UNIQUE constraint failed: integration_review_evidence.id
1 failed, 36 deselected in 3.27s
```

After exact identity validation/reuse:

```text
$ .venv/bin/pytest -q tests/test_integration_state.py -k 'task8a_migration_cycle'
1 passed, 36 deselected in 3.19s
```

Reverse insertion order produced another focused RED, proving that insert-time empty
batch validation was necessary in addition to repair/lease insert validation:

```text
$ .venv/bin/pytest -q tests/test_integration_state.py -k 'batch_request_and_empty_structure' -x
FAILED tests/test_integration_state.py::test_batch_request_and_empty_structure_are_database_invariants[sqlite]
E Failed: DID NOT RAISE any of (IntegrityError, DBAPIError)
1 failed, 35 deselected in 0.30s
```

After adding empty-batch INSERT guards:

```text
$ .venv/bin/pytest -q tests/test_integration_state.py -k 'batch_request_and_empty_structure' -x
1 passed, 1 skipped, 35 deselected in 2.76s
```

Fresh unique PostgreSQL upgrade/downgrade/upgrade after the final trigger change:

```text
$ POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/aq_task8a_sol_5e3a91c2' .venv/bin/pytest -q tests/test_integration_state.py -k 'postgres_migration_cycle_from_prior_revision_to_final_and_back'
1 passed, 36 deselected in 3.99s
```

### Slice 2 — scheduler

RED:

```text
$ .venv/bin/pytest -q tests/test_integration_schedule.py
ERROR tests/test_integration_schedule.py
E ModuleNotFoundError: No module named 'src.integration.scheduler'
1 error in 0.21s
```

First GREEN for missed-window arithmetic, coalescing, disable/manual, interval edit,
restart, and duplicate delivery:

```text
$ .venv/bin/pytest -q tests/test_integration_schedule.py
4 passed in 0.61s
```

After command and concurrent duplicate cases were added, the final single-file result
was:

```text
$ .venv/bin/pytest -q tests/test_integration_schedule.py
7 passed, 1 warning in 3.73s
```

### Slice 3 — typed adapter and authority

RED:

```text
$ aq test tests/test_integration_schedule.py tests/test_integration_contracts.py -k 'schedule_command or schedule_contract' -x
FAILED tests/test_integration_contracts.py::test_schedule_contract_is_typed_and_retry_safe
E src.commands.contracts.registry.UnknownContract: 'integration_schedule_due'
FAILED tests/test_integration_schedule.py::test_schedule_command_delegates_for_trusted_local
E {'error': 'Unknown command: integration_schedule_due'}
2 failed, 11 warnings in 2.65s
```

GREEN:

```text
$ aq test tests/test_integration_schedule.py tests/test_integration_contracts.py -k 'schedule_command or schedule_contract' -x
3 passed, 11 warnings in 2.85s
```

Focused compatibility for every pre-existing batch/member fixture changed by the new
non-null schema:

```text
$ aq test tests/test_integration_repair.py tests/test_integration_transfer_commands.py tests/test_integration_parent_completion.py -k 'batch_operation_reservation_is_terminal_stable or parent_green_waits or parent_green_and_timeout or transfer_accepts_a_collector or sealed_batch_member_protects' -x
5 passed, 11 warnings in 3.18s
```

## Final affected-area gate

The final gate was run once after the final empty-insert hardening, with the supplied
PostgreSQL endpoint pointed only at the unique `aq_task8a_sol_5e3a91c2` prefix:

```text
$ POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/aq_task8a_sol_5e3a91c2' aq test tests/test_integration_schedule.py tests/test_integration_contracts.py tests/test_integration_state.py tests/test_integration_repair.py tests/test_integration_parent_completion.py tests/test_integration_transfer_commands.py -k 'integration_schedule or schedule_contract or unimplemented_integration_operations or batch_request_and_empty_structure or post_sealing_batch_identity or batch_member_requires_append_only or task8a_migration_cycle or active_batch_is_unique or sealed_batch_membership or member_cannot_move or candidate_member_results_are_ordered or all_durable_records_round_trip or read_projections_and_receipts or batch_operation_reservation_is_terminal_stable or parent_green_waits or parent_green_and_timeout or transfer_accepts_a_collector or sealed_batch_member_protects'
33 passed, 11 warnings in 26.06s
```

The warnings are the existing `pkg_resources`/namespace-package/audioop deprecations;
this phase did not broaden scope to silence them.

Changed-file lint, whitespace, and migration-head verification:

```text
$ ruff check src/commands/contracts/integration.py src/commands/integration_commands.py src/database/adapters/postgresql.py src/database/adapters/sqlite.py src/database/queries/integration_schedule_queries.py src/database/tables.py src/integration/scheduler.py migrations/versions/d8e9f0a1b2c3_durable_integration_sweeps.py tests/test_integration_contracts.py tests/test_integration_parent_completion.py tests/test_integration_repair.py tests/test_integration_schedule.py tests/test_integration_state.py tests/test_integration_transfer_commands.py
All checks passed!
$ git diff --check
$ .venv/bin/alembic heads
d8e9f0a1b2c3 (head)
```

Disposable PostgreSQL cleanup was explicit and verified:

```text
removed=['aq_task8a_sol_5e3a91c2', 'aq_task8a_sol_5e3a91c2_gw0', 'aq_task8a_sol_5e3a91c2_gw1', 'aq_task8a_sol_5e3a91c2_gw2', 'aq_task8a_sol_5e3a91c2_master']
survivors=[]
```

## Changed files

- `migrations/versions/d8e9f0a1b2c3_durable_integration_sweeps.py`
- `src/database/tables.py`
- `src/database/queries/integration_schedule_queries.py`
- `src/database/adapters/sqlite.py`
- `src/database/adapters/postgresql.py`
- `src/integration/scheduler.py`
- `src/commands/contracts/integration.py`
- `src/commands/integration_commands.py`
- `tests/test_integration_schedule.py`
- `tests/test_integration_state.py`
- `tests/test_integration_contracts.py`
- `tests/test_integration_repair.py`
- `tests/test_integration_parent_completion.py`
- `tests/test_integration_transfer_commands.py`

## Self-review and concerns

- Reservation identity is allocated deterministically from the locked durable sequence,
  and the schedule update and outbox insertion share the same transaction. Concurrent
  duplicate calls therefore return one `due` and one `coalesced`, not two events.
- Periodic advancement occurs before coalescing, so a long-running active batch does not
  leave the clock one boundary at a time behind. Provenance fields are never included in
  a later coalescing update.
- The schema protects empty batches in both reference orderings despite the deliberately
  soft batch references on repair operations and leases.
- The migration preserves pre-existing member JSON in a linked append-only evidence row
  and safely reuses that row through U-D-U rather than deleting append-only proof.
- The broad pre-existing `tests/test_integration_state.py` file contains unrelated stale
  assertions/fixtures at base `c7d8e9f0a1b2`: the historic prepared-identity and repair
  stage monotonic SQLite triggers are already absent at that revision, and a parent
  repair fixture omits its now-required episode row. Task 8a did not repair or mask those
  unrelated baselines; the final gate selects every Task 8a invariant and every legacy
  fixture affected by the new batch/member fields.
- No runtime concern remains within the prescribed Task 8a scope. Task 8b must retain the
  schedule's outstanding provenance while its batch is active and clear it only when the
  consumed batch is released; that is the reviewed handoff contract that makes subsequent
  triggers coalesce to the first request.

## Fix round 1 — legacy nullable batch identity

Review found that c7 legally permitted a non-empty batch with a null `base_sha`, null
`integration_branch`, or both, while the Task 8a structural constraint requires both for
every non-empty lifecycle. The revision now begins `upgrade()` with a read-only
compatibility query. If any such row exists, it raises:

```text
cannot upgrade integration batch sealing schema while legacy non-empty batches have a
null base SHA or integration branch; drain or reconcile those batches before upgrading
```

This check executes before existing-trigger drops, column creation, backfill, or batch
rewrite. It does not invent Git identity from optional history.

The regression tests parameterize all three legal c7 null combinations on both SQLite
and PostgreSQL. For every case they snapshot the row, batch columns, and complete trigger
set; assert the explicit refusal, unchanged row/schema/guards, and c7 Alembic version;
then delete the incompatible row and prove upgrade/downgrade/upgrade succeeds.

### Fix-round RED

The corrected SQLite RED reached the old behavior's accidental structural-constraint
failure rather than the required compatibility refusal:

```text
$ .venv/bin/pytest -q tests/test_integration_state.py -k 'sqlite_task8a_upgrade_refuses_legacy_null_batch_identity' -x
FAILED tests/test_integration_state.py::test_sqlite_task8a_upgrade_refuses_legacy_null_batch_identity_before_ddl[null-base]
E sqlite3.IntegrityError: CHECK constraint failed: ck_integration_batches_empty_identity
1 failed, 40 deselected in 9.31s
```

### Fix-round GREEN

```text
$ .venv/bin/pytest -q tests/test_integration_state.py -k 'sqlite_task8a_upgrade_refuses_legacy_null_batch_identity'
3 passed, 40 deselected in 44.08s
```

The PostgreSQL proof used only the unique disposable
`aq_task8a_fix1_8f23a4d1` prefix:

```text
$ POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/aq_task8a_fix1_8f23a4d1' .venv/bin/pytest -q tests/test_integration_state.py -k 'postgres_task8a_upgrade_refuses_legacy_null_batch_identity'
3 passed, 40 deselected in 9.54s
```

### Fix-round final focused migration gate

This selection includes the six null-combination refusal/drain/U-D-U cases, the seeded
SQLite Task 8a U-D-U case, and the PostgreSQL prior-revision U-D-U case:

```text
$ POSTGRES_TEST_DSN='postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/aq_task8a_fix1_final_6c19b2e7' .venv/bin/pytest -q tests/test_integration_state.py -k 'task8a_migration_cycle or task8a_upgrade_refuses_legacy_null_batch_identity or postgres_migration_cycle_from_prior_revision_to_final_and_back'
8 passed, 35 deselected in 64.68s (0:01:04)
```

Changed-file checks:

```text
$ ruff check migrations/versions/d8e9f0a1b2c3_durable_integration_sweeps.py tests/test_integration_state.py
All checks passed!
$ git diff --check
```

The final disposable prefix was removed and verified:

```text
removed=['aq_task8a_fix1_final_6c19b2e7', 'aq_task8a_fix1_final_6c19b2e7_master']
survivors=[]
```

Fix implementation commit: `97bef63f` (`fix: guard legacy integration batch upgrade`).

Self-review: the guard is intentionally a precondition, not a repair. Its predicate
matches exactly the population rejected by the new non-empty identity constraint; it
does not reject empty rows (which cannot exist at c7) and it performs no mutation before
raising. The before/after trigger-set and column snapshots establish that the explicit
failure leaves both dialects at the untouched c7 schema. No warning cleanup or Task 8b
consumer behavior was included.
