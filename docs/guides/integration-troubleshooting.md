# Integration troubleshooting with durable Subjects

The reconciler owns enabled root, parent and development delivery. Read the
Subject's recorded phase, policy pin, wait reason, last decision and next visit:

```bash
aq integration status PROJECT_ID
aq task show TASK_ID
aq doctor --check integration.subjects_overdue
aq doctor --check integration.subjects_held
```

These are read-only projections. An overdue visit is retried from durable state
once the daemon can run it. A held Subject needs the named gate resolved;
configuration changes and daemon restarts do not clear that gate or grant a
writer authority. Status can read Git to verify delivery evidence; it never advances delivery.

Inspect App credentials and branch protection with `aq integration app-verify
PROJECT_ID`. `app-setup` prints the fixes; applying GitHub settings is a separate
repository administrator action. See [App-mode setup](../config/app-mode-train.md).

## A passed task blocked at delivery

Passing close records execution, while delivery requires the exact revision to
be present on its target. Check `task show` for its checkpoint, current receipts
and blockers. Resolve missing source evidence or CI under the reviewed policy.
For a missing no-code receipt, a local operator records a no-code child's disposition with `aq integration record-noop`.
No worker or supervisor session can invoke that operator control.

A completed child missing from a parent's collection can be inspected through
`aq integration redrive-child CHILD_ID`. A completed root missing its PR can be
inspected through `aq integration redrive-root ROOT_ID`. Preview first; apply
uses the exact head and reason and repeats the proof. Changed remote ancestry,
source identity, live writers or human holds are blockers, never retry overrides.

## Human holds and repair progress

A human-held parent Subject reports its gate id and current version. After
reviewing the gate answer, release that exact hold:

```bash
aq integration release-held-gate SUBJECT_ID GATE_ID --apply --expected-version VERSION --reason REASON
```

Repair delegates keep their frozen starting commit, fence, deadline and attempt
budget. A stopped writer's preserved work can be inspected with
`recover-preserved-repair`; detached attachments can be inspected with
`rebind-detached-repair`. These controls repeat their Git and ownership proofs
before applying. See [repair recovery](hierarchical-integration-trains.md#repair-and-collection-recovery).

A historical cancelled parent collection can be previewed with
`aq integration reopen-collection PARENT_ID`. It reuses the existing episode and
receipts, and refuses ambiguous writes, attached writers, changed targets and
human gates. The old cancellation control is retired.

## A finished task still owns its branch

A branch-owner row in `integration_branch_owners` with `handoff_state` of
`attached` or `handoff_pending` fences its branch, pins the writer's claim and
workspace lock, and keeps the branch off the deletion path even after the task
is done. Several paths strand such a row: delegate retirement and
historical cancellation keep it by design, a failed ownership transfer leaves it
`handoff_pending` forever, archive and delete drop the proof rows the exit-time
confirmers need, and slot reuse makes the "no newer session" check impossible.
The row stays `attached` and the branch stays held indefinitely.

**Spot it.** Either of these symptoms points here:

```text
# One task's claim fails on every attempt
canonical branch is not reserved by this task

# A delivered branch is still on the remote, held by an owner row
integration owner … is reserved
```

Preview the owner's current proof using its task id or row id:

```bash
aq integration release-owner --task-id TASK_ID --dry-run
aq integration release-owner --owner-row-id OWNER_ROW_ID --dry-run
```

The command resolves its project server-side and checks the caller's scope.

**What the recovery proves** (for each row, independently, in one transaction
when it passes):

1. *The writer is gone.* The row's session is `stopped` in both `state` and
   `desired_state`, **and** the session provider confirms the process is no
   longer running by a fresh probe. A tmux session that no longer exists counts
   as gone. `writer_live` is returned if either check fails.
2. *The branch is safe.* After `git fetch origin`, every worktree holding the
   branch is inspected. Anything origin does not already carry — a dirty tree,
   a commit the remote is missing — is first pushed, never forced, to
   `aq/preserved/<owner-row-id>`. The worktree is then detached. A branch whose
   origin ref was already deleted by branch-cleanup policy but whose tip is
   reachable from `origin/main` is safe without preservation. `checkout_in_use`
   is returned if a live session holds the worktree. `origin_unreachable` is
   returned if `fetch` or the preservation push fails.
3. *The release is atomic.* The row is re-read inside a `BEGIN … COMMIT` block
   and compare-and-swapped on `(id, fence_token, handoff_state)`. If it moved
   while the check was running — a new writer attached, the fence bumped —
   `stale_fence` is returned and nothing is written.

The row is the only thing touched: no checkout, workspace lock, session, or
task status is modified. A fresh fence token is assigned so any in-flight
confirmers holding the old token refuse rather than double-release.

**What gets preserved.** Uncommitted work in a stranded worktree is snapshotted
through a temporary index (the working tree and the live index are never
touched), committed as a single commit on a temporary ref, and pushed to
`aq/preserved/<owner-row-id>` on origin. The resulting outcome is
`preserved_and_released`; the `evidence` JSON column records the preserved ref
and its sha. The work can be pulled back from any clone:

```bash
git fetch origin refs/heads/aq/preserved/<owner-row-id>
git branch recovered/<owner-row-id> FETCH_HEAD
```

**Example: dry-run then real run.**

```bash
# First, see what the check would do (no writes, no pushes, no audit row).
# In a dry run the concrete push is planned but not performed, so the evidence
# carries a "planned" list instead of a pushed sha:
aq integration release-owner --task-id repair-repair-batch-…-1 --dry-run
# => {
#     "success": true,
#     "outcome": "preserved_and_released",
#     "outcomes": [{
#         "owner_row_id": "o-a3f7…",
#         "outcome": "preserved_and_released",
#         "reason": null,
#         "dry_run": true,
#         "evidence": {
#             "handoff_state": "attached",
#             "fence_token": 3,
#             "session_id": "4baaded6",
#             "workspace_id": "slot-3",
#             "planned": [{
#                 "action": "push",
#                 "ref": "aq/preserved/o-a3f7…",
#                 "source": "snapshot of slot-3",
#                 "sha": null
#             }]
#         }
#     }]
# }

# Satisfied. Run it for real — this pushes the preserved ref and writes the
# audit row in the same logical step as the release.
aq integration release-owner --task-id repair-repair-batch-…-1
# => same shape, dry_run: false, one row now in integration_owner_recoveries
```

You can also target a specific owner row directly instead of a task:

```bash
aq integration release-owner --owner-row-id o-a3f7…
```

`release-owner` takes no `--project-id`: the task (or the owner row's repository) names the
project, and the caller's token has to name the same one. A project's supervisor session may
therefore run this recovery for its own work and is refused for another project's — see
[aq-surface §7.3](../specs/design/aq-surface.md#73-elevated-scopes-and-the-commands-that-carry-no-project_id).

**Refusal reasons and how to resolve them.**

Every refusal is recorded in `integration_owner_recoveries` with the row's
`evidence` field populated — the `evidence.detail` string has the same wording
as the recovery result. Dry runs do not write an audit row.

| Refusal | Meaning | Fix |
|---|---|---|
| `writer_live` | The provider probe says the writer is still running, or a newer live session names the task. | Wait for the writer to exit, or confirm the task is truly cancelled/archived, then retry. A drain-acked pool worker holding a retired repair delegate, or still bound to a task whose close already committed, is stopped automatically (see the repair-delegate section and the paragraph below this table). |
| `checkout_in_use` | A live session holds the worktree the branch is checked out in. | Close that session or hand it off, then retry. |
| `origin_unreachable` | `git fetch` or the preservation push failed (network, auth, rate limit). | Fix connectivity or auth, then retry. |
| `stale_fence` | The row changed between the check and the release — a new writer attached, or the fence was bumped by another recovery attempt. | Re-run; the new writer will be the one the check evaluates. |
| `not_found` | The owner row id is wrong, or the row was already released by the time the command ran. | Preview the current task with `aq integration release-owner --task-id TASK_ID --dry-run`. |
| `not_recoverable_state` | The row's `owner_role` is `collector`, or its `handoff_state` is `released`. Only `worker`/`repair` rows in `attached` or `handoff_pending` are recoverable. | Do nothing — the row is in a correct state. |

**A worker still bound to a closed task.** A daemon restart during
`aq task close` can commit the terminal transition and lose everything after
it: the branch handoff, the completion record and the claim release. The task
reads `COMPLETED`, but the worker's session still names it, the owner row stays
`attached` to that session, and `release-owner` answers `writer_live` while the
worker sits idle at its final summary. Once the worker runs
`aq session drain-ack`, the session reconciler stops it on its next tick
(`session.settled_claim_stopped`, plus a task comment from `session-reconciler`).
The stop needs the agent's own drain-ack and all of this durable proof:

- the task is `COMPLETED` or `FAILED`, holds no agent, and its claim epoch is the
  session's;
- a completion record or a `code`/`noop` delivery receipt was written after the
  session's attempt on the task began, or, before either exists, the task's
  `accepted_close` marker names this session and its claim epoch. The transition
  that accepts a close writes that marker in its own transaction.
  `close_session_id` never counts: the close writes it before it is accepted, so
  a refused close leaves it behind;
- no running integration operation owns the task in any seat;
- no completion of the task is still running in this daemon (its control lock is
  free), so an ack after a timed-out close never stops a worker mid-handoff.

Nothing about the task changes. The stopped session keeps its claim, checkout and
binding, so this owner recovery, run by hand or by the sweep below, passes the
writer check, snapshots unpushed work and then releases the branch.

**Automatic sweep.** The daemon runs this same guarded recovery every 300 s
against every candidate row quiet for at least 600 s. It ships on by default
(`integration.owner_recovery_sweep: true`): stranded-owner recovery is routine,
and the sweep takes no shortcut the manual command does not. A candidate's
session must be stopped, a live writer is refused with `writer_live`, a live
checkout with `checkout_in_use`, and an unreachable origin with
`origin_unreachable`; a refusal repeating its reason is recorded once per
throttle window. Once the writer is stopped and the branch is genuinely safe
the sweep succeeds within one tick and the row appears in
`integration_owner_recoveries` under principal `sweep`. Set it to `false` to
release stranded owners only by hand: an explicit `false` always stays off, and
the manual command remains available regardless.

```yaml
# config.yaml
integration:
  owner_recovery_sweep: false   # release stranded owners by hand only
```

**Reading the audit trail.**

Every non-dry recovery attempt — success or refusal — writes one row to
`integration_owner_recoveries`. The `principal` field records who triggered it:

| Principal | How it was triggered |
|---|---|
| `human:local-operator` | `aq integration release-owner …` from the CLI on the daemon's host |
| `supervisor session:<id>` | An elevated named supervisor session called the command |
| `sweep` | The automatic 300-s sweep |
| `delegate_retirement` | The retirement path inside an ending integration operation |

To read recent recoveries straight from the database, note that `created_at`
is an integer Unix-epoch timestamp. Run it against the daemon's database with
`psql`:

```bash
psql "$AQ_DATABASE_URL" \
  -c "SELECT created_at, owner_row_id, ref, task_id, outcome, reason,
            principal
      FROM   integration_owner_recoveries
      ORDER  BY created_at DESC
      LIMIT  20;"
```

To translate the epoch and narrow to one owner row:

```sql
SELECT to_timestamp(created_at) AS when,
       outcome, reason, principal, evidence
FROM   integration_owner_recoveries
WHERE  owner_row_id = 'o-a3f7…'
ORDER  BY created_at DESC;
```


## Source and related pages

[Reconciler operation](hierarchical-integration-trains.md) describes current
project configuration, exact-head recoveries and receipt requirements.
[Worker pools](worker-pools.md) describes claims and session handoff.
The shared stopped-writer implementation is `src/integration/owner_recovery.py`;
its durable audit table is retained for historical and current recoveries.
