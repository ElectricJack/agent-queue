# Deleting tasks that own a materialized branch

**Date:** 2026-09-08
**Status:** implemented
**Supersedes:** the removal half of `2026-09-04-hierarchical-integration-trains-design.md` §6.8

## 1. Problem

In a project with `hierarchical_integration_mode` of `hierarchy` or `train`, a task
can never be deleted or archived once its branch has been created on the remote.

```
Could not delete task. API 422: hierarchy.delivery_target_fixed:
delete would discard a materialized origin
```

The refusal comes from the single hierarchy writer:

```python
# src/database/queries/hierarchy_queries.py:448
if retire_pending and any(bool(row["materialized"]) for row in origins):
    raise HierarchyError(
        "delivery_target_fixed", f"{mutation} would discard a materialized origin"
    )
```

`retire_pending=True` is passed by exactly two callers — `delete_task`
(`task_queries.py:1334`) and `archive_task` (`archive_queries.py:38`) — so both
paths are blocked for the same reason.

### 1.1 Why it is permanent

`task_branch_origins.materialized` flips to true when the branch actually appears
on the remote (`hierarchy.py:733`). Only two code paths ever set `retired_at`, and
both explicitly exclude materialized rows:

| site | what it retires |
|---|---|
| `hierarchy.py:789` (reparent) | `.where(materialized.is_(False))` |
| `hierarchy_queries.py:457` (the guard's own retire step) | `.where(materialized.is_(False))` |

A database trigger forbids clearing `materialized`. There is no CLI command, doctor
check, or service method that retires a materialized origin. The flag is a one-way
door with no exit, so the block is not "until the work settles" — it is forever.

### 1.2 Observed impact

On the operator's database, project `agent-queue` (the only one in `train` mode):

| status | live materialized origins |
|---|---|
| COMPLETED | 52 |
| FAILED | 2 |
| PAUSED | 1 |
| READY | 1 |

All 56 are permanently undeletable *and* unarchivable, so they cannot even be swept
out of the active queue; the count only grows. 55 of them have no delivery receipt
and belong to no active batch — every other guard in `guard_integration_mutation`
would have passed. Most are `Review: …` tasks, which by construction never deliver
code, yet each holds a materialized branch forever.

### 1.3 What the design actually intended

§6.8 of the trains design says "once code work starts, keep its delivery target
fixed; transfer later work through a new task instead." That reasoning is about
**reparenting** — moving a live task's delivery base out from under it. It was
applied to removal by analogy, and the removal case has no counterpart to the
"create a follow-up task" escape hatch. Deleting a task is not redirecting its
delivery; it is an operator saying the work should not exist.

## 2. Decisions

1. **Delete may discard a materialized branch, but never by accident.** When a
   delete's subtree contains a materialized origin and the caller has not stated a
   branch policy, the command still refuses — with a new, specific code that names
   the branches and the two ways forward. Interactive surfaces turn that refusal
   into a prompt; scripts and playbooks cannot destroy a branch by omission.
2. **Archive never touches git.** Archive is "move this out of my view", not
   "destroy this". It stops being blocked — it retires the origin so the task can
   leave the active queue — and always leaves the remote ref alone.
3. **Branch removal is durable and asynchronous.** The delete transaction records
   the intent; a reconciliation step performs the ref deletion with a
   compare-and-swap on the observed head, retries on transport failure, and parks
   conflicts for an operator. A slow or flaky remote never fails a delete.
4. **The other refusals are unchanged.** Sealed batches, live descendant sessions,
   and delivered branch identity still refuse unconditionally. This design relaxes
   exactly one check.

### 2.1 Why not the integration outbox

Symmetry argues for mirroring `integration.branch_materialization_pending`, which
`_reserve_origin` enqueues on the outbox. That symmetry is a trap: the outbox
dispatches **only to playbooks** —

```python
# src/orchestrator/core.py:1539
async def accept_integration_event(event_type, payload, event_id) -> bool:
    if self.playbook_manager is None:
        return False
    return await self.playbook_manager.accept_integration_event(...)
```

— and no shipped playbook triggers on `integration.branch_materialization_pending`.
Neither `hierarchical-delivery` nor `root-integration-train` lists it. On the
operator's box one such event has been retrying for 129 attempts with
`no enabled matching playbook durably accepted the event`. An outbox event for
branch discard would ship as a no-op for the same reason.

The intent behind the choice — durable, retried, crash-safe, CAS on the expected
SHA — is kept; the transport is a reconciliation drain that actually runs. See §6
for the separate materialization gap this uncovered.

## 3. Mechanism

### 3.1 The guard

`guard_integration_mutation` gains a `branch_policy` parameter, meaningful only
when `retire_pending` is set:

```python
async def guard_integration_mutation(
    self, task_id, mutation, *, conn,
    retire_pending: bool = False,
    branch_policy: Literal["keep", "discard"] | None = None,
) -> bool:
```

- `archive_task` passes `branch_policy="keep"`, always.
- `delete_task` passes the caller's choice, or `None`.
- With `retire_pending` and any materialized live origin in scope:
  - `branch_policy is None` → raise `HierarchyError("branch_discard_required", …)`
    carrying the affected `(task_id, branch, base_sha)` triples.
  - `"keep"` → retire the origins, leave the refs.
  - `"discard"` → retire the origins and mark each materialized one for discard.
- The retire `UPDATE` drops its `materialized.is_(False)` filter, so every live
  origin in scope is retired. Branch-owner release and surviving-parent generation
  bumps are unchanged and already correct.

Ordering is unchanged: the sealed-batch and delivery-receipt refusals run first, so
a delivered or sealed task still refuses regardless of `branch_policy`.

### 3.2 Recording the intent

`task_branch_origins` carries no foreign key to `tasks`, and `_delete_one` does not
touch it — retired origin rows already outlive their tasks as audit records. The
discard intent therefore lives on the row itself, with no new table:

| column | type | notes |
|---|---|---|
| `discard_state` | TEXT nullable | `pending` \| `complete` \| `conflict` \| `failed` |
| `discard_requested_at` | REAL nullable | set with `retired_at` in the delete txn |
| `discard_attempts` | INTEGER NOT NULL DEFAULT 0 | |
| `discard_next_attempt_at` | REAL nullable | exponential backoff |
| `discard_last_error` | TEXT nullable | |

Named check constraint `ck_task_branch_origins_discard_state`; a partial index on
`discard_state = 'pending'` keyed by `discard_next_attempt_at` for the drain scan.
Alembic revision generated from `tables.py` with an inspector guard, per the
project's idempotent-migration rule.

**The trigger has to move too.** Implementation turned up a second enforcement
point the original analysis missed: `task_branch_origin_materialized_immutable`
(`migrations/integration_guards.py:140`) raises on *any* UPDATE or DELETE of a
materialized row, not just on clearing `materialized`. That, not only the
Python guard, is what made retiring one impossible.

`migrations/integration_guards.py` is the immutable pre-squash snapshot by
contract ("Keep this snapshot immutable; subsequent behavior changes belong in
subsequent Alembic revisions"), so revision `a00000000004` narrows it with a
`CREATE OR REPLACE` that runs after the baseline installs it. Identity stays
frozen — `task_id`, `repository_id`, parent columns, `base_sha`,
`creation_generation`, `reserved`, `materialized`, `created_at`,
`materialized_at` — and DELETE of a materialized row stays forbidden. Only
`retired_at` and the discard bookkeeping may move. `downgrade` restores the
whole-row body.

### 3.3 The drain

`src/integration/branch_discard.py` — `BranchDiscardService.drain_due(now)`, wired
into the same reconciliation loop that already owns `integration_outbox.dispatch_due`
in `run_one_cycle`. Per due row, mirroring `IntegrationCleanupService._cleanup_remote_ref`:

1. Refuse if the short ref is the repository's default branch → `conflict`.
2. Refuse if `integration_branch_owners` still holds the ref with
   `handoff_state != 'released'` → `conflict` (something is writing to it).
3. Resolve the current head through the GitHub app client. Absent → `complete`.
4. `adelete_ref_with_app_auth(..., expected_old_oid=<observed head>)` against the
   retained store. A concurrent move re-observes and lands `conflict`.
5. Transport failure → `retryable`: bump `discard_attempts`, back off, keep
   `pending`, record `discard_last_error`.

Terminal `conflict` / `failed` rows are surfaced by a new
`aq doctor --check integration.branch_discards`, which names the ref and the
reason and can retry with `--fix`. That fix only re-arms the row to `pending`;
doctor never deletes a ref itself. A discard parks as `conflict` precisely when
the remote stopped matching what the operator was asked about, and doctor is
not the place to overrule that.

## 4. Surfaces

**Command.** `task_delete` accepts `branches: "keep" | "delete"`. Absent, the
existing behaviour for non-hierarchical projects is unchanged (there is nothing to
discard) and hierarchical projects get the new refusal. Contract in
`src/commands/contracts/`, response model in `src/api/models/task.py`, entry in
`src/tools/definitions.py`. `openapi.json` and both generated clients regenerated
per CLAUDE.md.

**Refusal payload.** The command layer already converts `HierarchyError` to
`{"error": "hierarchy.<code>: <detail>", "code": "hierarchy.<code>"}`. For
`branch_discard_required` it additionally returns `branches`, so a UI can name them:

```json
{
  "success": false,
  "code": "hierarchy.branch_discard_required",
  "error": "hierarchy.branch_discard_required: 1 task in this subtree has a branch on the remote",
  "branches": [{"task_id": "azure-beacon", "branch": "aq/azure-beacon", "base_sha": "999990a7…"}]
}
```

**CLI.** `aq task delete <id> [--branches keep|delete]`. The CLI is generated
from the tool definition, and an `enum` there becomes a `click.Choice`, so one
option carrying the wire vocabulary beats two hand-written flags that would
have to be kept in sync with it.

**Dashboard.** The delete action catches `hierarchy.branch_discard_required` and
opens a modal listing the branches with a keep/delete choice, then re-issues the
delete with `branches` set. Cancel is the default.

## 5. Existing rows

No backfill. Once the guard accepts a policy, the 56 stuck origins are reachable
through the ordinary surfaces: archiving the completed ones retires their origins
and leaves their refs, and delete with an explicit choice handles the rest.

## 6. Adjacent finding — branch materialization has no consumer

While tracing the transport, `HierarchyIntegration.materialize_origin`
(`hierarchy.py:676`) turns out to have **no production caller** on `main`:

```
$ git grep -n "materialize_origin"
src/integration/hierarchy.py:676:    async def materialize_origin(...)
tests/test_integration_hierarchy.py:514:    await service.materialize_origin(...)
```

`_reserve_origin` enqueues `integration.branch_materialization_pending`, the outbox
routes it to playbooks, and no shipped playbook lists that trigger. On the
operator's box the most recent such event has failed 129 times with
`no enabled matching playbook durably accepted the event`, and its task
(`sound-current.9`) sits with `materialized = false` — meaning it can never be
claimed, since `materialized_origin_when_hierarchical()` gates claiming on that
flag. 56 older origins were materialized promptly, so this regressed rather than
never having worked.

This is a separate defect from the one this design fixes and wants its own task.
It is recorded here because it is the reason §2.1 rejects the outbox as the
discard transport.

## 7. What shipped

| area | file |
|---|---|
| schema + trigger | `src/database/tables.py`, `migrations/versions/a00000000004_task_branch_origin_discard.py` |
| guard | `src/database/queries/hierarchy_queries.py` (`branch_policy`, `HierarchyError.context`) |
| callers | `task_queries.delete_task`, `archive_queries.archive_task`, `database/base.py` |
| drain | `src/integration/branch_discard.py`, wired via `IntegrationService(branch_discard_handler=…)` and `Orchestrator._drain_branch_discards` |
| command | `task_commands._cmd_delete_task` (`branches`), `_hierarchy_failure`, `_materialized_branches_under` |
| surface | `src/tools/definitions.py`, `src/api/models/task.py`, regenerated `openapi.json` + both clients |
| doctor | `integration.branch_discards` in `src/doctor/integration_checks.py` |
| dashboard | `dashboard/src/api/branchDiscard.ts`, `components/BranchDiscardPrompt.tsx`, wired in `TaskActions.tsx` and `panes/task-detail/` |

## 8. Tests

`tests/test_integration_hierarchy.py::test_materialized_child_cannot_be_deleted_or_archived`
is replaced by:

- delete with no policy on a materialized subtree → `branch_discard_required`,
  payload names every branch in the subtree
- delete with `keep` → task gone, origin retired, `discard_state IS NULL`
- delete with `discard` → task gone, origin retired and `pending`
- archive → succeeds, origin retired, ref untouched, no discard row
- sealed batch and delivery receipt still refuse under both policies
- the surviving parent's checkpoint generation still advances in every accepting case
- an unknown `branch_policy` is a programming error, not a refusal

`tests/test_branch_discard.py` covers the drain: absent ref → `complete`; moved
head → `conflict`; live branch owner → `conflict`; default branch →
`conflict`; transport error → retry with backoff; exhausted attempts →
`failed`; a backed-off row is not attempted early; non-`pending` rows are left
alone.

`tests/test_doctor_integration_checks.py` covers the check and that its fix
re-arms rather than deleting. `dashboard/src/api/__tests__/branchDiscard.test.ts`
covers the refusal parser in both directions — a missed prompt makes delete
look permanently broken, a false positive asks about branches on an unrelated
error.
