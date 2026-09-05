# Task 2 report — durable integration outbox and registration boundary

## Result

Implemented the Task 2 durable acceptance path on
`feat/hierarchical-integration-trains`.

- Added transactional `enqueue_integration_event(...)` and bounded
  `IntegrationOutbox.dispatch_due(...)`. Outbox identity/content reuse is
  checked, acknowledgements occur only after the consumer reports durable
  acceptance, and failures receive bounded exponential backoff.
- Added `V2PlaybookRuntime.accept_integration_event(...)` as the concrete
  acceptance boundary. It identifies every enabled, ready, matching
  activation, persists one protected pending row per activation destination,
  and returns before running the playbook. A zero-target event is not
  acknowledged.
- Added stable destination and dispatch identities. Retrying after a partial
  destination insert completes the missing rows without duplicating those
  already accepted. Scope is part of destination deduplication because one
  playbook may have several scoped activations.
- Added protected integration rows to `playbook_pending_events` and migration
  `a7c4d9e2106b`. Protected rows are excluded from generic TTL purge,
  `drop_oldest`, ordinary quota counts, and operator discard. The ordinary
  pending-event policies remain unchanged.
- Added asynchronous replay after durable acceptance and restart replay from
  protected pending storage. A row is finalized only if every selected rule
  has a durable run ID; engine errors or partial run creation leave the row
  retryable.
- Added the integration command mixin and builtin registration hook. The hook
  intentionally registers no commands yet: Task 2 has no real integration
  mutation handlers, so unavailable names remain explicit `Unknown command`
  failures instead of success stubs. Tasks 3+ must register authority and
  redaction alongside each real handler.
- Registered all fourteen design event schemas with required `project_id` and
  `operation_id`, and added the integration-operation, branch-ownership, and
  delivery-evidence effect subjects. Existing generic clause rendering handles
  these typed subjects without an explanation renderer change.

## Durable route

The normal `EventBus` callback is not the acknowledgement boundary:
`V2PlaybookRuntime._on_event` creates a task and `_dispatch` catches errors.
Task 2 therefore uses this route:

```text
domain transaction -> integration_outbox
outbox page -> V2PlaybookRuntime.accept_integration_event
            -> protected playbook_pending_events rows (all destinations)
outbox acknowledgement
background/restart replay -> claimed engine dispatch -> durable run(s)
                          -> pending row resolved
```

Thus a process crash after protected-row acceptance but before outbox
acknowledgement replays the same event and activation identity. Database
uniqueness, rather than an in-memory cache, deduplicates both acceptance and
run dispatch.

## TDD evidence

1. Wrote the focused outbox and contract tests first and ran:
   `aq test tests/test_integration_outbox.py tests/test_integration_contracts.py -x`.
   Collection failed because `src.integration.outbox` did not exist.
2. Implemented the initial outbox, protected pending storage, runtime route,
   event schemas, subjects, and empty registration boundary. The focused run
   passed 12 tests.
3. Added the partial-run regression. It failed because a dispatch reporting
   two selected rules and only one run ID finalized the pending row. Requiring
   one durable run per selected rule made it pass.
4. Added accepted-but-unstarted restart replay. It initially found zero runs;
   bounded protected-backlog replay during runtime refresh made it pass.
5. Added same-playbook/multiple-scope destination coverage. It first failed on
   the existing `(playbook_id, dedup_key)` unique index; including scope in the
   protected destination dedup key made both destinations durable.

## Verification

Successful final checks:

```text
aq test tests/test_integration_outbox.py tests/test_integration_contracts.py -x
15 passed

aq test -p no:xdist tests/test_playbook_pending_events_policy.py \
  -k 'overflow or expiry or duplicate or concurrent_arrivals'
12 passed, 10 skipped, 32 deselected

pytest -q tests/test_migration_boolean_defaults.py
1 passed

ruff check <all changed Python files>
All checks passed

git diff --check
```

The focused outbox suite uses the real SQLite database adapter, pending-event
repository, activation/artifact storage, and V2 run table. It covers committed
restart survival, a simulated process crash after acceptance and before
acknowledgement, zero targets, partial destination writes, restart replay,
partial rule-run creation, protected retention, normal `drop_oldest`, bounded
pages, bounded exponential retry, and destination scope identity.

The existing retention regressions confirm ordinary overflow, expiry,
duplicate, and concurrent-arrival policy is unchanged. The migration Boolean
guard confirms the new server default uses `sa.false()` rather than an integer
default that PostgreSQL would reject.

## Pre-existing excluded tests

A broader diagnostic run selected 163 tests and reported 75 passed, 61
skipped, and 27 failed. Every failure in
`tests/test_playbook_pending_events_commands.py` and the command/replay half of
`tests/test_playbook_pending_events_policy.py` occurs while constructing
`PlaybooksConfig(v2_api=True, v2_storage_enabled=True,
v2_activation_writes=True)`. Those constructor arguments do not exist at the
Task 1 base commit `b834bc98`; its `PlaybooksConfig` begins with only
`enabled` and current V2 size/policy fields. The storage-only regression subset
above is green and was used for Task 2 verification.

`tests/test_v2_entry_points.py` was also excluded after collection failed on
its import of `v2_engine_enabled` from `src.playbooks.services`; that symbol is
likewise absent at `b834bc98`. No unrelated compatibility changes were made.

An attempted multi-file disposable-PostgreSQL diagnostic was not used as
evidence because those independently resetting files shared one database while
xdist was active. Task 2 did not migrate the operator database or alter worker
database variables.

## Self-review and limitations

- The outbox processes one ordered page per call and never treats no matching
  activation as success. The later integration orchestration service (Task 10)
  owns recurring polling. Runtime refresh also schedules one bounded page of
  protected restart backlog; normal acceptance schedules all newly accepted
  destinations immediately.
- Protected rows are server-owned: no generic retention or command API accepts
  a caller-provided protection flag. Explicit integration abort/release policy
  belongs to later control tasks.
- Dispatch claims can become stale during a very long playbook run. Stable
  `dispatch_id` plus the run-table uniqueness constraint makes a takeover
  idempotent; a protected row is still not finalized until durable runs exist.
- No Task 2 command has a truthful implementation. Consequently there is no
  authority/redaction declaration to register in this task. The design-name
  set is metadata only and is tested not to enter the callable registry.
- The existing explanation renderer already renders effect-subject values
  generically. Adding a special branch would duplicate behavior, so no renderer
  edit was needed.

## Changed files

- `src/integration/outbox.py`
- `src/commands/integration_commands.py`
- `src/commands/contracts/integration.py`
- `src/commands/contracts/builtin.py`
- `src/commands/contracts/models.py`
- `src/commands/handler.py`
- `src/database/queries/playbook_run_queries.py`
- `src/database/tables.py`
- `src/event_schemas.py`
- `src/playbooks/runtime.py`
- `migrations/versions/a7c4d9e2106b_protect_integration_pending_events.py`
- `tests/test_integration_outbox.py`
- `tests/test_integration_contracts.py`
