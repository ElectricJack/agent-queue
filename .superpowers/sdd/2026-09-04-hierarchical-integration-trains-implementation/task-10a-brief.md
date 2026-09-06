# Task 10a — Bounded durable reconciliation substrate

This is the first independently reviewed Task 10 phase. Read this file first; it is the
complete phase requirement for schedule catch-up, bounded durable selectors, the daemon service
lifecycle, and integration-outbox dispatch. It deliberately does not build candidates, observe or
publish CI, promote main, release a train, or perform cleanup.

## Dependency gate

Do not begin runtime work until Task 9b2 fix 1a and fix 1b have each passed independent review.
The reviewed Task 9b2 surface must provide the exclusive root-main mutation protocol, mandatory
pre-main attestation resolver, complete immutable member receipts, terminal
`lifecycle='promoted'` with `cleanup_state='pending'`, and registered operation-bound
`integration.root_delivered`, `integration.batch_promoted`, and
`integration.cleanup_requested` outbox facts. Consume those surfaces; do not repair, wrap, or
duplicate their authority logic in this phase.

Task 10b may start only after 10a passes review. Task 10c may consume the selectors and service
extension seams defined here, but it owns release, cleanup, command routes, and root prose.

## Outcome and file ownership

Create:

- `src/database/queries/integration_reconciliation_queries.py` — bounded, ordered, read-only
  selectors used by one daemon tick.
- `src/integration/service.py` — `IntegrationService` lifecycle, overlap guard, bounded
  schedule/deadline/outbox work, and extension points for 10b/10c handlers.
- `tests/test_integration_service.py` — service, paging, overlap, restart, disable, and outbox
  crash-boundary acceptance.
- One Alembic-generated revision named `integration schedule catchup policy`; generate its revision
  ID from the actual implementation head and do not hand-select or rewrite an existing revision.
- `tests/test_migration_integration_service.py` — SQLite/PostgreSQL upgrade, downgrade guard, and
  re-upgrade coverage for this phase's state.

Modify only as required:

- `src/database/tables.py` — catch-up tuple and its named constraints.
- `src/database/queries/integration_schedule_queries.py` — conn-owned schedule/CAS helpers.
- `src/database/base.py`, `src/database/adapters/sqlite.py`, and
  `src/database/adapters/postgresql.py` — expose/compose the reconciliation query mixin.
- `src/integration/models.py` — frozen root move and cleanup retry policy value types.
- `src/integration/scheduler.py` — active-request versus first-catch-up coalescing.
- `src/integration/repair.py` — make deadline paging bounded; preserve all timeout semantics.
- `src/integration/outbox.py` — only if a focused crash/dispatch test exposes a missing bounded
  cursor or retry projection. Its existing `dispatch_due(now) -> int` and durable artifact pin
  protocol are the authority.
- `src/orchestrator/core.py` — construct/start the service after command and V2 playbook
  initialization, tick it from the normal cycle, and stop it before database shutdown.
- Existing focused schedule, repair, outbox, orchestrator, and integration-policy tests when a
  compatibility assertion belongs with their current owner.

Do not create command contracts in 10a. `integration_release`, build/CI/rebuild adapters, cleanup,
the workflow, attestation script, and root playbook belong to later phases.

## Exact persisted contract

Extend `project_integration_schedules` with one nullable all-or-none catch-up tuple:

- `catchup_trigger`: `periodic|manual`;
- `catchup_requested_at`: UTC epoch float for the first coalesced trigger;
- `catchup_after_sequence`: the nonnegative active `request_sequence` behind which it coalesced.

Name the check constraints. The tuple is NULL when no catch-up is pending. While an outstanding
request owns a nonempty active batch, the first later manual or due periodic trigger stores this
tuple without changing `outstanding_request_id`, `outstanding_trigger`,
`outstanding_requested_at`, or `request_sequence`; later triggers are canonical replays and do not
replace the first provenance. Periodic arithmetic still advances `next_due_at` and
`last_observed_window` by all elapsed boundaries. A terminal empty sweep consumes its outstanding
request immediately and does not manufacture catch-up state. Task 10c alone converts the tuple to
one new request during release.

The migration must preserve existing schedule rows as no-catch-up. Downgrade must abort with a
clear diagnostic naming the first project that has live catch-up state; after those tuples are
drained, downgrade restores the previous schema without rewriting outstanding request identity.

Extend the frozen `HierarchicalIntegrationPolicy` with:

```python
class IntegrationCleanupPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_attempts: int = Field(default=5, gt=0)
    retry_base_seconds: float = Field(default=30.0, gt=0)
    retry_max_seconds: float = Field(default=3600.0, gt=0)

    @model_validator(mode="after")
    def ordered_backoff(self) -> "IntegrationCleanupPolicy": ...

class HierarchicalIntegrationPolicy(BaseModel):
    # existing fields remain unchanged
    on_main_moved: Literal["rebuild", "wait"] = "wait"
    cleanup: IntegrationCleanupPolicy = Field(default_factory=IntegrationCleanupPolicy)
```

`ordered_backoff` requires `retry_max_seconds >= retry_base_seconds`. Add explicit compatibility
defaults exactly as shown so old stored policies still parse; newly sealed batches must include the
resolved values in `policy_snapshot`. `wait` is the fail-closed legacy default. Never reread
mutable project policy for an active operation.

## Exact query and service interfaces

`IntegrationReconciliationQueriesMixin` provides keyset pages, each validating `limit > 0` and
returning plain row dictionaries in the documented order:

```python
async def due_integration_schedule_page(
    self, *, now: float, after: tuple[float, str] | None, limit: int
) -> list[dict]

async def due_integration_repair_stage_page(
    self, *, now: float, after: tuple[float, str, int] | None, limit: int
) -> list[dict]

async def pending_candidate_ci_page(
    self, *, after: tuple[float, str, int] | None, limit: int
) -> list[dict]

async def unresolved_integration_intent_page(
    self, *, after: tuple[float, str] | None, limit: int
) -> list[dict]
```

Dispatch unresolved root intents by their persisted discriminator and batch/revision. Do not let
child finalization satisfy root delivery. Cursor advancement always uses the last scanned row,
including a stale row that a handler declines. Task 10c adds the cleanup selector only after its
normalized cleanup table exists; 10a exposes the handler slot but never returns hard-coded cleanup
success.

`RepairService.due_stages` becomes a page-shaped compatibility wrapper or is replaced by the
bounded query above; no production tick may call the current unbounded all-row query.

Implement:

```python
class IntegrationService:
    async def tick(self, now: float) -> None: ...
    def start(self) -> None: ...
    async def stop(self) -> None: ...
```

Construction receives the database, `IntegrationScheduler`, `RepairService`,
`IntegrationOutbox`, and narrow optional handler callables for candidate CI, unresolved intents,
and cleanup. A missing later-phase handler leaves its durable row untouched and logs a bounded
retryable blocker; it never reports success. `tick` uses a nonblocking in-process lock: an
overlapping call returns immediately. Cross-process authority remains row/CAS/dedup state, not the
local lock.

One tick processes at most the configured page size for each source, in stable source order:
due schedules, expired repair stages, pending candidate-CI subjects, unresolved typed intents,
pending cleanup, then `IntegrationOutbox.dispatch_due(now)`. Each selected item performs one
bounded transition and returns. Provider/Git work is forbidden in 10a. Isolate one item's error so
later sources still run, while cancellation propagates. `start` creates one named background loop
only after runtime construction; `stop` signals and awaits it deterministically. Restart derives
all work from durable rows, never an in-memory queue or lost event notification.

Wire the outbox accept callback directly to
`V2PlaybookRuntime.accept_integration_event(event_type, payload, event_id)`. Never use transient
`EventBus` acknowledgement. An unbound `integration.sweep_due` may resolve only a currently
enabled root activation. Once an event has `operation_id`, its operation's frozen artifact route
is authoritative even if the current activation is disabled or changed. If the V2 runtime is
absent, the callback returns false so the outbox remains retryable. Disabling train scheduling
suppresses new periodic sweeps but must not strand an already active train.

## TDD slices and acceptance

1. RED/GREEN catch-up schema and scheduler: active nonempty train plus manual and periodic triggers
   retains the exact outstanding request/provenance/sequence, stores only the first catch-up, and
   emits no second sweep event; empty consumes immediately. Cover periodic boundary arithmetic,
   concurrent writers, and request replay.
2. RED/GREEN bounded selectors with deliberately tiny pages and more than 200 rows. Prove every
   row appears once, scan cursors advance, `due_stages` is no longer unbounded, typed intent kinds
   stay distinct, and inactive/stale rows are not selected.
3. RED/GREEN `IntegrationService.tick`: local overlap returns, two services race safely through
   durable CAS/dedup, one handler failure does not skip other sources, cancellation propagates,
   and restart recovers active ownership, pending intent, expired stage, and lost notification.
4. RED/GREEN outbox routing: no EventBus correctness dependency; crash before acceptance, after
   retained pending run, after manifest page, and before acknowledgement converges byte-stably;
   absent runtime stays pending; disable/change affects only unbound events and frozen operation
   routing survives.
5. RED/GREEN orchestrator lifecycle: service starts only after commands/playbooks, runs without a
   second global timer, and stops before DB close with no orphan task.
6. Exercise fresh SQLite and a uniquely named disposable PostgreSQL database through upgrade,
   seeded catch-up downgrade refusal, drain, downgrade, and re-upgrade. Never touch `postgres`,
   `integration_test`, the operator database, or worker DB environment variables.

Focused commands during implementation use one file at a time. Required final gate:

```bash
aq test tests/test_integration_schedule.py tests/test_integration_service.py \
  tests/test_integration_outbox.py tests/test_integration_repair.py tests/test_orchestrator.py -x
```

Run changed-Python Ruff and `git diff --check`. Record exact RED/GREEN/final commands, counts,
migration head/cycles, files, commits, and self-review in `task-10a-report.md`; commit runtime/tests/
migration first, then the report. No whole suite or worker-count increase.

## Binding exclusions

No live forge/network/credentials, main or integration ref mutation, candidate construction,
attestation publication, workflow routing, release, cleanup action, root playbook, enablement,
operator DB/config mutation, daemon start, push, PR, Task 11 controls, or Task 12 E2E. Keep all
functionality flag-disabled.
