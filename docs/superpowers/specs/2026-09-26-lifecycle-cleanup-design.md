# Lifecycle cleanup: stale containers, stale-open tasks, obsolete closes

Task: azure-lantern (2026-09-26). Status: implemented.

## Problem

On 2026-09-26 finished and obsolete work lingered open for 10-13 hours:

- The epics **sharp-impact** (14/14 children COMPLETED) and **agile-torrent**
  (15/15) stayed BLOCKED and PAUSED respectively, both left over from restarts
  and updates. §7 settlement only completes IN_PROGRESS containers, so nothing
  moved them once the last child was delivered.
- **solid-cascade** and **crisp-apex** (duplicate Alembic revision) stayed
  BLOCKED after main renumbered the migrations. The only way to close them was
  a failing close plus a status edit. That left each with an unreleased branch
  owner row and in a development batch (one parked, one publishing), so
  `aq task delete` was refused (`integration_cleanup_blocked`, then
  `integration_owned`). Worse, a COMPLETED task with a `branch_name` is a
  publisher candidate, so the superseded source was collected again and parked
  its batch.

## Decisions

### 1. Stale-status settlement leg (`hierarchy_queries`)

`settle_containers` gains a second leg selected by `stale_container_clauses()`:
a container in BLOCKED or PAUSED, with the usual §7 conditions (container flag,
no live session, no non-COMPLETED child, not a childless held-open container,
not owned by a hierarchy/train collection episode), **at least one child**, and
**every non-container child delivered**. Delivery is the development
publisher's receipt (`_development_delivery_pending(child,
include_foreign_repos=True)` is false): a delivered or adopted batch to the
default branch lists the child's latest completion source. Outside development
mode, or for a branchless child, there is nothing to deliver. A container child
proved its own children's delivery when it settled.

`_settle_stale_container` re-checks the status under the row lock. It skips a
pause whose session cleanup is pending. Otherwise it deletes the
`manual_pause` / `manual_pause_withholds_children` hold and completes the
container with `force` and `_manual_pause_control`. It records
`task.stale_container_settled` with the status it left.

The leg runs in three places:

- **Event path:** a child's completion seeds its parent, and the parent's own
  completion recurses upward, so a stale grandparent settles through an
  IN_PROGRESS parent.
- **Backstop sweep** (`reconcile_stale_containers`, every
  `container_sweep_interval_seconds`): a delivery landing after the last
  completion has no event.
- **Daemon start** (`Orchestrator.initialize`, after `_recover_stale_state`):
  restarts are what strand these containers.

The ordinary IN_PROGRESS leg is unchanged. It still settles on children
COMPLETED alone. The stricter delivery requirement applies only when
overriding a stale status.

### 2. Stale-open re-evaluation (`src/orchestrator/lifecycle.py`)

Every `work_graph.lifecycle_sweep_interval_seconds` (300s) the lifecycle sweep
first settles stale containers. It then takes every BLOCKED/PAUSED task whose
`updated_at` is older than `work_graph.stale_open_after_seconds` (6h). A task
already carrying another `needs_attention` code belongs to whoever raised it.

- **BLOCKED:** recompute the `is_blocked` projection. If the task has no
  terminal mark, has graph blockers (edges or gates), and they are all
  satisfied now, it goes to READY (context `stale_open_recheck`). This
  catches a projection left stale by a restart between a dependency's
  completion or delivery and its recompute.
- **PAUSED:** a pause with no timer and no `manual_pause` snapshot (the hold is
  gone) is resumed through `recover_orphaned_pause`. An expired timer is left
  to the backoff sweep.
- **Everything else** is flagged by `flag_stale_open`. That is a
  compare-and-set on the status and `updated_at` the sweep read. It writes
  `needs_attention=stale_open`, a `stale_open_detail` (status, since, reason,
  terminal close or hold, and up to 20 unmet blockers), a `task.stale_open`
  event and one supervisor inbox message (`msg-stale-open-<task>-<updated_at>`,
  `ON CONFLICT DO NOTHING`), all in one transaction. The message names the
  choices: unblock or resume, re-route, or `aq task close --obsolete`.

`stale_open` is the one **advisory** attention code:

- the promotion cascade still promotes a flagged BLOCKED task;
- `incident_reason` ignores it, so it never opens or re-keys a recovery
  incident;
- phase holds do not count a flagged child as failed;
- collecting-parent recovery treats it as no attention;
- `_apply_transition` removes it, with its detail, on any transition out of
  BLOCKED/PAUSED.

### 3. Obsolete close (`src/integration/obsolete_close.py`)

`aq task close <id> --obsolete --reason "..."` (`task_close` with
`obsolete: true`; `outcome` is no longer schema-required and is still demanded
by the handler otherwise). Authority is `integration_operator`: the local
operator or the project's live named supervisor. A worker session is refused.

**Refusals:** hierarchy/train projects (the parent's collection owns a child's
branch there), a live session, an agent still assigned to an
ASSIGNED/IN_PROGRESS task, or open children.

**The close, in one transaction:**

- the `obsolete` marker (`reason`, `closed_by`, `closed_at`,
  `previous_status`, `cleanup`) and `work_outcome=abandoned`;
- holds, attention and publisher-skip metadata are dropped;
- the task moves to COMPLETED from any status, or, when it is already
  COMPLETED, its dependents are recomputed.

The marker is honored by `_development_delivery_pending`, so dependents stop
waiting for a delivery that will never come. The publisher's candidate query
ignores the task, and so does its dependency check (`requires_publication`).
The 14-day stale-branch cleanup for abandoned work still applies to its branch.

**The cleanup** is idempotent. Its result is recorded in `marker.cleanup`:

- every unreleased non-collector branch-owner row goes through
  `ObsoleteOwnerRelease`. That is `OwnerRecovery` with `reserved` added to
  `recoverable_states`: writer gone, branch secured (work origin lacks is
  pushed to `aq/preserved/<row>`), fenced compare-and-swap. A refusal stays
  pending with its reason;
- each unsettled development batch that lists the task:
  - a **parked** batch is cancelled under `publisher_exclusion`, the
    publisher's advisory lock, extracted from
    `DevelopmentIntegration.exclusion`. Its evidence gets
    `released.conclusion=obsolete_member`, the event is
    `integration.development_batch_released`, and its members' projection is
    recomputed. The other members return to the publisher and are batched
    again without the obsolete source. Cancelling is chosen over rewriting the
    manifest because a repair's identity is a hash of the manifest;
  - a **prepared or publishing** batch stays pending: the task is retried after
    the batch finishes;
  - while an open development repair lists the task as a source, parked
    batches are left alone and stay pending as `repair_in_flight`;
- active train batch membership is reported as pending (`sealed_batch`).

The lifecycle sweep's `retry_obsolete_cleanup` re-runs the cleanup until
`state=done` (event `task.obsolete_cleanup_done`). A cleaned-up task holds
nothing the removal guard refuses on, so `aq task delete` works.

### 4. Doctor: `tasks.dangling_lifecycle`

Report-only. It lists:

- COMPLETED tasks with unreleased non-collector owner rows;
- COMPLETED tasks in unsettled development batches or active train batches;
- obsolete closes with pending cleanup;
- open containers whose children are all COMPLETED or FAILED.

Batch membership that is only still publishing is INFO; anything else is WARN.
On the live install at the time of writing it reports 83 owner rows (the known
`integration.finished_branch_owners` backlog) and 3 parked memberships.

## Configuration

```yaml
work_graph:
  container_sweep_interval_seconds: 60     # also runs the stale-status leg
  lifecycle_sweep_interval_seconds: 300    # 0 disables stale-open + obsolete retries
  stale_open_after_seconds: 21600          # 0 disables stale-open flagging
```

## Out of scope / follow-ups

- **amber-falcon:** 10 COMPLETED tasks (6 settled containers, sharp-impact
  among them) are skipped by the development publisher every tick with
  `missing_ref`. Their `branch_name` never reached origin, so
  `_development_delivery_pending` stays true forever.
- The edit to the shipped `aq-tasks` skill reaches an existing install only
  after the skill is reseeded (`aq doctor --check skills.installed_drift`).

## Tests

`tests/test_lifecycle_cleanup.py` has one class per part:

- `TestStaleContainerSettlement`
- `TestStaleOpenReevaluation`
- `TestObsoleteClose` and `TestObsoleteCloseCommand`
- `TestDanglingLifecycleDoctor`
