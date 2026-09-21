# Releasing the delegates of an ended integration operation

Status: implemented (keen-crest.3) **except §2's rule and §3's constraint drop,
which were withheld at integration** (development repair
`development-repair-71c73b9f6d83b803115f`). Main had meanwhile put the same
schema change on hold — `2026-09-20-archive-tasks-with-integration-history-design.md`
§12, findings B1–B3 — and declared the four references `refused` in
`src/database/queries/task_references.py`. What shipped: the live-owner guard
(§4), the release and its `integration_delegate_releases` audit table (§5,
migration `a00000000011`, create only), `integration.stranded_delegates`, and
`aq integration release-delegates`. The four foreign keys remain, so a settled
delegate or finished root that history still names stays unremovable, refused
with `integration_owned` naming the table. §2–§3 below are kept as the proposal
they now are.
Date: 2026-09-20

## 1. The defect

Seven tasks on the operator's install can be neither deleted nor archived, and
the dashboard gives no reason. Jack tried the delete five times.

| task | status | refused by |
| --- | --- | --- |
| `verify-81d0aaee-0c3a-482c-b04c-d3afe6631cbe` | `FAILED` | `integration_repair_operations.verifier_task_id` |
| `repair-repair-batch-integration-batch-2c48…-1` | `BLOCKED` | `integration_candidate_resolutions.repair_task_id` |
| `keen-harbor`, `nimble-dune`, `noble-ridge`, `smart-dune`, `sound-current` | `COMPLETED` | `integration_parent_episodes.parent_task_id` |

Every one of those refusals is a **foreign key**, not a guard:

```
integration_repair_operations.verifier_task_id     -> tasks.id  ON DELETE RESTRICT
integration_parent_episodes.parent_task_id         -> tasks.id  ON DELETE RESTRICT
integration_parent_verifications.parent_task_id    -> tasks.id  ON DELETE RESTRICT
integration_candidate_resolutions.repair_task_id   -> tasks.id  ON DELETE NO ACTION
```

`archive_task` and `delete_task` both *remove* the `tasks` row — archive moves it
to `archived_tasks`, delete drops it — so PostgreSQL raises
`ForeignKeyViolationError` and the operator sees a bare failure with no subject,
no cause and no remedy.

`RepairService.retire_terminal_delegates` (shipped earlier) already settles the
*status* half: a delegate of a `completed`/`cancelled` operation with no live
writer becomes terminal `FAILED` carrying `integration_retirement`. Nothing ever
settles the *reference* half, so the retired ticket is still undeletable —
"obsolete, terminal, and permanently in the way".

## 2. The rule

> **Integration history references a task by id, never by foreign key.**
>
> A finished episode, verification, resolution or operation is a record of what
> happened to a task. It must not be able to veto that task leaving the active
> queue. The veto belongs to *live* integration state, and it is expressed as a
> guard that can name the operation, its state and the command that releases it
> — never as a constraint that raises at the operator.

This is the same rule `task_branch_origins` already follows (it deliberately
carries no FK to `tasks` so a discard row can outlive the task it describes).

Two consequences:

1. **Removal is unblocked structurally** (§3). The four constraints go; the
   columns and their values stay, so history still names the task and the
   `archived_tasks` row still answers "which task was that".
2. **Liveness is enforced explicitly** (§4). `guard_integration_mutation` grows
   a live-owner check covering strictly *more* than the constraints did — the
   repair-stage delegate of an active operation had no FK at all and was
   deletable out from under a running writer.

## 3. Schema (`a00000000011`)

Drop, idempotently (the revision inspects `pg_constraint` first, per
[migrations guide](../../guides/migrations.md) — the squashed baseline builds
from live metadata):

- `fk_integration_parent_episodes_parent_task`
- `fk_integration_parent_verifications_parent_task`
- `fk_integration_repair_operations_verifier_task`
- `fk_integration_candidate_resolutions_task`

Nothing else changes about those tables: the composite targets
`(parent_task_id, id)` that five other tables reference stay exactly as they
are, so no dependent constraint moves.

Add `integration_delegate_releases` — the audit trail. It records one row per
delegate released, and it carries **no** foreign key to `tasks` or to
`integration_repair_operations`, because its whole job is to outlive both:

| column | meaning |
| --- | --- |
| `id` | `rel-<uuid4[:12]>` |
| `operation_id`, `operation_state` | the operation that ended, and how |
| `task_id`, `project_id` | the delegate |
| `role` | `verifier` \| `repair_stage` \| `candidate_member` |
| `disposition` | `cancelled` (operation was cancelled) \| `superseded` (it completed without this delegate) |
| `previous_status` | what the ticket was before the release |
| `reason` | the sentence shown to operators |
| `released_by` | `integration_service` \| `integration_abort` \| `integration_cancel_preserving` \| `integration_release_delegates` \| `doctor` |
| `released_at` | clock |
| `cleanup` | what the delegate still holds (`{"state": …, "blockers": […]}`) |

## 4. The live-owner guard

`guard_integration_mutation(task_id, "delete"|"archive", …)` refuses when any
task in the subtree is owned by an operation that is still running:

- an `integration_repair_operations` row in `active` / `escalated` /
  `human_required` naming the task as `verifier_task_id`, as `parent_task_id`,
  or through an `integration_repair_stages.repair_task_id`;
- an `integration_candidate_resolutions` row in `reserved` / `pushed` naming it
  as `repair_task_id`.

It raises `HierarchyError("integration_owned", …)` whose detail names the
operation, its state, the role the task plays in it, and the release path, and
whose context carries the same fields structurally so a surface can render them.

An **ended** operation (`completed` / `cancelled`) never refuses. That is the
point: its delegates are released, and the removal proceeds.

The existing narrower check in `archive_task` (active operation via a stage) is
left in place; it is a subset and costs one indexed lookup.

## 5. The release

`src/integration/delegate_release.py` owns one implementation, used by every
caller: the reconciliation tick, `integration_abort`, `cancel_preserving`, the
scoped operator command and the doctor fix.

```
release_delegates(db, *, now, operation_ids=None, released_by, limit=100) -> list[Release]
```

For every operation in `completed` / `cancelled` (optionally narrowed to
`operation_ids`), for every delegate task it owns that is **not yet terminal**
and has **no live writer** (no assigned agent, no session that is anything but
fully stopped):

1. force-transition the ticket to `FAILED`, context
   `integration_delegate_retired` — terminal, non-success, never a manufactured
   pass;
2. write `integration_retirement` task metadata (unchanged shape: operation,
   state, disposition, previous status/attention/hold, `cleanup`);
3. drop the holds that a cancellation left (`needs_attention`,
   `claim_prepare_backoff_until`, `blocked_terminal`, `manual_pause`);
4. insert one `integration_delegate_releases` row;
5. `log_event("task.updated", …)`.

Preserved exactly as before: branches, stage evidence, retry counters, retained
branch owners and workspace locks. A retained owner or lock is **reported** as a
named cleanup blocker, never released here — `aq task explain` re-reads it live.

`RepairService.retire_terminal_delegates` becomes a thin call into this, so the
orchestrator tick, the abort path, the operator command and doctor cannot drift
apart. Three callers settling a delegate three slightly different ways is how an
earlier version left tickets `PAUSED` that a later one had to roll forward.

### 5.1 Cancelling releases in the same transaction

`IntegrationRecoveryControls.abort` sets the operation `cancelled`; it now
releases that operation's delegates in the **same** transaction, `released_by =
integration_abort`. Acceptance criterion: cancelling an operation leaves no
integration-owned delegate behind — not "leaves one until the next tick".

### 5.2 `aq integration release-delegates OPERATION_ID`

The scoped operator control, LOCAL authority only
(`IntegrationRecoveryControls.release_delegates`, contract
`integration_release_delegates`). Outcomes: `released`, `nothing_to_release`,
`invalid_state` (the operation is still running — stop it first), `not_found`.
`aq integration retry-cleanup` is deliberately *not* overloaded for this: it
requeues `integration_cleanup_items` for a batch, which is a different subject
with a different identity.

### 5.3 `aq doctor --check integration.stranded_delegates [--fix]`

Reports delegate tasks of an ended operation that are still non-terminal and
have no live writer — exactly the set `release_delegates` would settle. `--fix`
runs the release with `released_by = doctor` and reports what it released.

The check deliberately does **not** list a task merely because history still
names it: after §3 that is no longer a problem, and listing every historical
verifier forever would be noise.

## 6. Actionable operator errors

One message builder, `retired_delegate_message(operation_id, state, action)`:

> `Integration operation <id> is <state>; its delegate is no longer required and
> cannot be <action>. Release it with
> \`aq doctor --check integration.stranded_delegates --fix\`, then delete or
> archive the task.`

Used by:

- `transition_task` — the refusal `restart_task` / any re-activation already hit,
  now carrying the remedy;
- `db.resume_task` — previously answered `Task is not paused (status: FAILED)`,
  which named neither the operation nor the cause;
- the live-owner guard (§4), with the operation's own controls named instead
  (`aq integration abort <id> --reason …`), because a *running* operation is not
  released, it is stopped.

## 7. `sessions_task_id_fkey` (epic item 4)

Confirmed fixed on main: `_delete_one` sets `sessions.task_id = NULL` before the
`tasks` DELETE (a session row is the historical record of an agent run and stays
true after the task is gone). It had no regression test — §8 adds one, because
the crash is silent to re-introduce.

## 8. Tests

`tests/test_integration_delegate_release.py`

- each of the four historical references no longer blocks delete **or** archive,
  parametrised over the reference;
- a live operation *does* block both, with `integration_owned`, and the detail
  names the operation, its state and the remedy;
- cancelling via `abort` retires the delegate and writes the audit row in one
  transaction; no integration-owned delegate is left;
- the audit row survives deleting the delegate task;
- release is idempotent — a second pass returns nothing and rewrites nothing;
- doctor reports, `--fix` releases, and re-running reports clean;
- `aq integration release-delegates` refuses a running operation, settles one
  ended operation without touching another, and then reports
  `nothing_to_release`;
- `resume_task` / `restart_task` / `delete_task` name the operation and the
  remedy;
- deleting a task that has a session row succeeds and keeps the session with
  `task_id IS NULL` (§7).

Existing `tests/test_integration_repair.py` retirement tests continue to pass
unchanged — they are the contract `release_delegates` had to preserve.
