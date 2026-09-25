# Integration troubleshooting

Symptoms you will actually see when a finished branch is not reaching your
default branch, and the command that answers each one.

Every command here is read-only unless it says otherwise. Start with the
[integration concept page](../concepts/integration.md) if the words *batch*,
*manifest*, *journal* or *parked* are new; start with the
[development integration guide](development-integration.md) if you are looking
for how to run this rather than how to unstick it.

## Start here

```bash
aq integration status <project>
```

Three fields answer most questions:

| Field | Reading |
|---|---|
| `pending_publications` | Non-empty → a push happened whose outcome is not yet confirmed. Wait one sweep. |
| `parked` | Non-empty → something is assembled but undelivered. Read its journal row's `reason` and `evidence`. |
| `blockers` | Non-empty → the daemon is telling you what it is waiting for. |

Then, for the fleet-wide view:

```bash
aq doctor --check integration.operational
aq doctor --check integration.stranded_fences
aq doctor --check integration.stranded_delegates
aq doctor --check integration.finished_branch_owners
aq doctor --check integration.branch_discards
aq doctor --check integration.unreviewed_prs
aq doctor --check integration.development_publisher_stalled
aq doctor --check git.stale_branches
```

## Symptom index

| Symptom | Cause | Go to |
|---|---|---|
| Task is `COMPLETED`, work is not on `main` | Normal: delivery is batched | [Nothing is wrong yet](#nothing-is-wrong-yet) |
| `blockers: [{"code": "publication_pending"}]` | A push is unconfirmed | [Publication pending](#publication-pending) |
| Journal row `parked`, evidence `kind: merge_conflict` | A source would not merge | [A member conflicted](#a-member-conflicted) |
| Journal row `parked`, evidence `conclusion: failed` | Validation failed | [Validation failed](#validation-failed) |
| `blocked: validation modified the candidate; refusing publication` | A check wrote to the tree | [Validation modified the candidate](#validation-modified-the-candidate) |
| `blocked: repository publisher is already running` | Another sweep holds the lock | [The publisher is busy](#the-publisher-is-busy) |
| `blocked: remote write … needs reconciliation: target changed` | Someone else moved the ref | [The remote moved under a publication](#the-remote-moved-under-a-publication) |
| `blocked: development publication must preserve target history` | The candidate would drop history | [History would be lost](#history-would-be-lost) |
| `blocked: legacy default-branch mutation must be reconciled first` | Old strict-mode writes are in flight | [Switching modes is blocked](integration-migration.md#the-switch-is-blocked) |
| `unauthorized: … requires LOCAL operator authority` | Wrong caller | [Who may run what](#who-may-run-what) |
| Every claim of one task fails "canonical branch is not reserved by this task" | A stranded ownership fence | [A branch is held by a writer that is gone](#a-branch-is-held-by-a-writer-that-is-gone) |
| A task sits `READY` in a hierarchy project and is never claimed | Its branch origin was never cut | [A branch origin was never materialized](#a-branch-origin-was-never-materialized) |
| A deleted task's branch is still on the remote | A parked branch discard | [A branch discard is parked](#a-branch-discard-is-parked) |
| `integration status` shows `draining: true` and the drain never finishes | Stale owners, leases or cleanup from an old train run | [A drain never completes](#a-drain-never-completes) |
| Observe status lists `missing_receipt` with cause `no_parent_collection` | Children of parents that finished before the train | [Legacy children block observe readiness](#legacy-children-block-observe-readiness) |
| A delivered branch is kept because `integration owner … is reserved` | An ownership row a finished task never let go | [A finished task still owns its branch](#a-finished-task-still-owns-its-branch) |
| Stale `aq/…` branches pile up on the remote | Held, older than cleanup, or cleanup exhausted | [Delivered branches are still on the remote](#delivered-branches-are-still-on-the-remote) |
| `hierarchy.delivery_pending` when archiving | The work has not reached `main` | [Delivered branches are still on the remote](#delivered-branches-are-still-on-the-remote) |
| Claim after claim fails preparing the slot | Bounded slot-reset retries | [Slot reset keeps failing](#slot-reset-keeps-failing) |
| `development-repair-…` tasks appearing | Parked content needs a human-shaped fix | [Repair tasks](#repair-tasks) |
| Parked batches never progress; `daemon.log` grows fast | The publisher is stalled on one batch | [The development publisher has stopped making progress](#the-development-publisher-has-stopped-making-progress) |
| A repair closed `pass` but its branch is in no batch | The publisher is not collecting | [The development publisher has stopped making progress](#the-development-publisher-has-stopped-making-progress) |

## Nothing is wrong yet

A task closing `pass` means its branch is pushed and finished. Delivery is a
separate batched pass that runs on the project's `interval_seconds` (default
300). Between those two events the work is finished and undelivered, and that
is the normal state of a busy fleet.

Check when the last batch landed:

```bash
aq integration status <project>
```

Look at the newest `deliveries` row. If the newest row is minutes old and your
task is not in any manifest, its branch was either not pushed, is blocked, or
depends on something that parked.

## Publication pending

A journal row in state `publishing` means the push ran and the remote has not
yet been read back. AQ does not guess: the next sweep reconciles the row
against the real remote and moves it to `delivered` (the ref carries the
prepared SHA or a descendant) or `parked` (the ref is still at the old SHA).

Do nothing except wait one interval. If it never clears, the sweep itself is
failing — check the daemon log for `Development batch remains recoverable for
<project>`, which is what a failed sweep logs before retrying.

## A member conflicted

```text
"evidence": {"kind": "merge_conflict", "detail": "CONFLICT (content): Merge conflict in …"}
"reason": "source conflict; independent work may continue"
```

The rest of the batch continued without that member. Nothing was lost: the
worker's branch is untouched and the conflict text is in the journal.

What happens next, automatically:

* If a later batch delivers the same content by another route, the parked row
  becomes `adopted` on the next sweep and nothing else happens.
* Otherwise AQ files one repair task — see [Repair tasks](#repair-tasks).

What you can do:

* Nothing, if the conflict is between two tasks that are still moving. A
  worker pushing new commits makes its member eligible again by itself.
* `aq integration sweep <project> --retry` to retry unchanged parked content
  under the current policy.
* Resolve it yourself and record it with `aq integration adopt` — see
  [the guide](development-integration.md#record-work-you-delivered-by-hand).

> **A very common cause.** Two tasks that both edit a generated file conflict
> every time. If the file is regenerated rather than hand-edited, arrange for
> exactly one ticket to regenerate it rather than having each ticket refresh it.

## Validation failed

```text
"evidence": {"kind": "local", "validation": "focused",
             "checks": [{"command": "…", "exit_code": 1, "outcome": "failed",
                         "failing_tests": [{"id": "tests/test_x.py::test_y", "reason": "…"}],
                         "duration_seconds": 231.4, "slot_wait_seconds": 12.0,
                         "run_seconds": 219.4, "summary": {"failed": 1, "passed": 118},
                         "output": "…"}],
             "failing_tests": […],
             "conclusion": "failed"}
"reason": "selected validation failed"
```

A batch parks only when tests ran and **failed**. The batch was assembled and
preserved as a ref, and nothing was published. `failing_tests` names what
failed (parsed from pytest's short test summary), and the `output` field holds
the last 8,000 characters of that command's combined output — read it before
anything else; it is the actual failure. The repair task the publisher files
quotes both, names the journal row, and keeps a compact copy in its
`development_repair_evidence` task metadata.

## Validation could not finish (deferred)

```text
{"outcome": "deferred", "reason": "timeout", "deferral": {"id": "…", "consecutive": 1, …}}
```

A validation that verified nothing is **infrastructure**, not a failure: the
run hit `timeout_seconds`, no test slot came free within `slot_wait_seconds`
(or `aq test` exited 75), the process was killed, pytest collected nothing or
could not start (exit 3/4/5), the command could not be executed (126/127), or
every failing test failed on a database/box outage (connection refused, too
many clients, …). The batch is **deferred**: it is not parked, no repair is
filed, and the next tick validates it again.

`timeout_seconds` covers the run only. Time the command spent queued for a
test slot is reported by `aq test` (`$AQ_TEST_SLOT_REPORT`, see
[resource gating](resource-gating.md)) and bounded separately by
`slot_wait_seconds` (default 600). Each check records `slot_wait_seconds`
and `run_seconds` so you can see which one ran out.

Each streak of deferrals is one `cancelled` journal row with
`evidence.kind: "validation_deferred"`, visible in `aq integration status
<project>`: it counts the consecutive deferrals and keeps the full evidence
(command, exit code, timing, output tail) of the last five. After three in a
row the publisher logs an error, messages `supervisor-<project>` once, and
`aq doctor --check integration.development_publisher_stalled` reports
`validation_infrastructure`. Fix the environment — the test database, the
slots, or the budgets (`aq integration develop … --timeout-seconds N
--slot-wait-seconds N --reason …`, maximum 3600 each). The first validation
that reaches a conclusion closes the streak.

A batch parked as `selected validation failed` before outcomes were
classified (for example exit code 124 with the output `validation timed
out`) and not yet given a repair is released for revalidation rather than
repaired; its row becomes `cancelled` with `evidence.released`. One whose
repair was already filed is left to that repair.

The candidate itself is on the remote as
`refs/heads/aq/development/<project digest>/<head sha>`, so you can check it
out and reproduce the failure locally.

Re-run after a fix with `aq integration sweep <project> --retry`.

## Validation modified the candidate

```text
blocked: validation modified the candidate; refusing publication
```

A validation command moved `HEAD` or left the working tree dirty. AQ refuses to
publish something other than what it validated. Generated artefacts, caches
written into the checkout, or a check that commits are the usual causes. Make
the command write outside the checkout, or add its outputs to the repository's
ignore rules.

## The publisher is busy

```text
blocked: repository publisher is already running
```

One publisher per repository, enforced by a PostgreSQL advisory lock. Another
sweep — scheduled or manual — holds it. Wait and retry; there is nothing to
clean up, because the lock dies with the process that held it and the durable
journal is what survives.

## The remote moved under a publication

```text
blocked: remote write <id> needs reconciliation: target changed
```

A row was left `publishing`, and the ref is now at neither the SHA that was
being published nor the SHA it was published from. Something outside AQ wrote
to that branch. The sweep stops rather than guessing.

Establish what happened before you do anything else: `git log` the target
branch, and compare against the row's `expected_sha` and `prepared_sha`. If the
work is on the branch by another route, record it with
`aq integration adopt … --accept-equivalent`. This is the one integration
failure that genuinely needs a human to look at the repository.

## History would be lost

```text
blocked: development publication must preserve target history
```

The candidate does not contain the current target as an ancestor, so publishing
it would drop commits. AQ refuses. This normally means the default branch was
force-pushed. Check `aq integration status <project>` for `repository_id` and
compare it with what the project is really pointed at: a mismatched retained
clone raises its own message, `retained repository URL differs from configured
repository`, from
[`src/integration/development.py`](../../src/integration/development.py).

## Who may run what

`develop`, `sweep`, `adopt` and `cancel-preserving` require **local operator
authority**: they must come from the machine running the daemon, not from a
worker session's token. A worker calling them gets:

```text
unauthorized: manual sweep requires LOCAL operator authority
```

`aq integration status` is readable by a worker session for its own project,
which is why a worker can diagnose but not act.

## A branch is held by a writer that is gone

```text
canonical branch is not reserved by this task
```

Every claim of one particular task fails with this, the task stays `READY`, and
the scheduler keeps offering it — burning a pool worker each time.

```bash
aq doctor --check integration.stranded_fences
```

That check reports an ownership row still marked `attached` or
`handoff_pending` while the database says no writer is left: no live session for
the owning task, no workspace locked by it, and the task not `ASSIGNED` or
`IN_PROGRESS`.

It is a **report, not a repair**, and deliberately so: nothing it can see proves
the old writer's process is stopped, its checkout clean, or its work published.
The repair is the guarded integration recovery path, which takes those proofs —
in a development-mode project, `preserve_stopped_owners` does exactly that on
each sweep, once the session provider confirms the process is really gone. It
only covers a writer still attached to its own checkout, though: when the task
has finished or been deleted and its slot was reused, see
[A finished task still owns its branch](#a-finished-task-still-owns-its-branch).

## A finished task still owns its branch

A branch-owner row in `integration_branch_owners` with `handoff_state` of
`attached` or `handoff_pending` fences its branch, pins the writer's claim and
workspace lock, and keeps the branch off the deletion path even after the task
is done. Several paths strand such a row: delegate retirement and
`cancel_preserving` keep it by design, a failed ownership transfer leaves it
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

Diagnose it:

```bash
aq doctor --check integration.stranded_fences          # what is held, and by whom
aq doctor --check integration.stranded_fences --fix    # run the guarded recovery on every row
```

The `--fix` path runs the same guarded check that `aq integration release-owner`
runs, under the principal `doctor`. It refuses the same refusals; it is not a
bypass.

There is also the broader doctor check that covers finished tasks only:

```bash
aq doctor --check integration.finished_branch_owners          # finished-task rows, report only
aq doctor --check integration.finished_branch_owners --fix    # release finished-task rows
```

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

**Refusal reasons and how to resolve them.**

Every refusal is recorded in `integration_owner_recoveries` with the row's
`evidence` field populated — the `evidence.detail` string has the same wording
as the doctor check's report. Dry runs do not write an audit row.

| Refusal | Meaning | Fix |
|---|---|---|
| `writer_live` | The provider probe says the writer is still running, or a newer live session names the task. | Wait for the writer to exit, or confirm the task is truly cancelled/archived, then retry. |
| `checkout_in_use` | A live session holds the worktree the branch is checked out in. | Close that session or hand it off, then retry. |
| `origin_unreachable` | `git fetch` or the preservation push failed (network, auth, rate limit). | Fix connectivity or auth, then retry. |
| `stale_fence` | The row changed between the check and the release — a new writer attached, or the fence was bumped by another recovery attempt. | Re-run; the new writer will be the one the check evaluates. |
| `not_found` | The owner row id is wrong, or the row was already released by the time the command ran. | Look up the current row id: `aq doctor --check integration.stranded_fences`. |
| `not_recoverable_state` | The row's `owner_role` is `collector`, or its `handoff_state` is `released`. Only `worker`/`repair` rows in `attached` or `handoff_pending` are recoverable. | Do nothing — the row is in a correct state. |

**Automatic sweep.** Set `integration.owner_recovery_sweep: true` in
`config.yaml` to have the daemon run this same guarded recovery every 300 s
against every candidate row quiet for at least 600 s. It ships off by
default because a live writer holding a row will be refused with
`writer_live` on every sweep tick, and a failed push (origin unreachable) will
refuse every sweep tick — both are noisy and unnecessary when no one has asked
for a release. Once the writer is stopped and the branch is genuinely safe the
sweep succeeds within one tick and the row appears in
`integration_owner_recoveries` under principal `sweep`. Enable it alongside
supervision: `doctor` and the manual command remain available regardless.

```yaml
# config.yaml
integration:
  owner_recovery_sweep: true
```

**Reading the audit trail.**

Every non-dry recovery attempt — success or refusal — writes one row to
`integration_owner_recoveries`. The `principal` field records who triggered it:

| Principal | How it was triggered |
|---|---|
| `human:local-operator` | `aq integration release-owner …` from the CLI on the daemon's host |
| `supervisor session:<id>` | An elevated named supervisor session called the command |
| `sweep` | The automatic 300-s sweep |
| `doctor` | `aq doctor --check integration.stranded_fences --fix` |
| `delegate_retirement` | The retirement path inside an ending integration operation |
| `cancel_preserving` | A `cancel_preserving` sweep |

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

## A drain never completes

`aq integration enable <project> --mode disabled` on a project with integration
work still in flight records `desired_mode: disabled` and `draining: true` and
leaves the current mode effective. The daemon finishes the drain on its own as
soon as nothing is left: no active batch, no promoted batch with unfinished
cleanup, no running repair operation, no `integration_branch_owners` row that is
not `released`, no project integration lease, no unsettled promotion intent and
no pending cleanup item. `aq integration status <project>` shows `desired_mode`
and `draining` in every mode, `development` included.

A train run that ended long ago can leave all of these behind. Clear them in
this order, as a local operator or the project's supervisor:

```bash
aq integration release-stale-owners --project-id <project> --dry-run  # read the verdicts
aq integration release-stale-owners --project-id <project>            # release the safe rows
aq integration retry-cleanup <batch-id>    # each promoted batch whose cleanup is pending
# ...once that cleanup completes:
aq integration release-stale-owners --project-id <project>            # its integration-branch owner
```

**`release-stale-owners`** releases a `reserved` owner row only when it can
prove the release safe. The row must name no session or workspace. Its owner
must be finished: a task that is `COMPLETED` or `FAILED`, archived or deleted,
or an operation that ended, with no live session, workspace lock or running
operation, and in no hierarchy/train project. Nothing may still rely on its
fence. And after a pruned `git fetch`, its branch tip must be on the default
branch, or the branch must be gone and its owner terminal. A `FAILED` task can
be retried, so a gone branch proves nothing for it. The release is the one
`release-owner` makes, with a fresh fence, an `integration_owner_recoveries` row
and an `integration.owner_recovered` event. The command also drops the project's
lease once it has expired and its batch is finished. It reports every other row
with its reason, and it is safe to repeat. `--older-than 2d` limits it to rows
unchanged for that long.

| Reason kept | Meaning | Next step |
|---|---|---|
| `not_reserved` | The row names a writer. | `aq integration release-owner --owner-row-id <id>` |
| `owner_active` | The owning task or operation is still running. | Let it finish. |
| `owner_blocked` | A live session, workspace lock, ref mutation or operation still acts for the owner, or a hierarchy/train project owns the row. | Resolve what the detail names. |
| `batch_cleanup_pending` | The row owns the integration branch of a promoted batch whose cleanup is not complete. | `aq integration retry-cleanup <batch>`, then re-run. |
| `batch_active` / `promotion_unsettled` | A batch or promotion intent still uses the branch. | Let it finish. |
| `branch_not_on_default` | origin has commits on the branch that `main` does not. | Deliver the work, or have a human confirm it is abandoned. `aq doctor --check integration.finished_branch_owners --fix` then releases the row without a branch proof. |
| `failed_owner_ref_gone` | The branch is gone, but its owner `FAILED` and may be retried. | Decide the task's fate first. |
| `local_work_unpublished` / `checkout_in_use` | A worktree or local branch holds work origin does not have, or a live session has the branch checked out. | Publish or discard that work. |
| `origin_unreachable` / `inspection_failed` | The fetch or a Git inspection failed. | Fix access and re-run. |

**`retry-cleanup`** requeues a batch's cleanup items that are waiting to retry.
A promoted batch whose cleanup never produced any items has nothing to requeue:
for it, `retry-cleanup` materializes the cleanup (outcome `materialized`) and
the daemon runs it. Cleanup refuses to delete a source branch whose owner row is
not released, which is why `release-stale-owners` runs first.

## Legacy children block observe readiness

```text
{"code": "missing_receipt", "detail": "no current parent collection exists for the terminal child", "cause": "no_parent_collection"}
```

A parent completes under the train only after each child has a delivery
receipt bound to the parent's collection. A parent that finished before the
project entered observe, hierarchy or train mode was never collected and never
will be; neither will a finished parent whose collection was cancelled when the
project switched to development. Its terminal children therefore stay flagged
unless their delivery is proven another way.

Status already accepts a child that the development publisher delivered to the
default branch. That `delivered` or `adopted` development delivery, bound to
the child's latest completion, is its receipt. Run the control for the rest,
as a local operator or the project's supervisor:

```bash
aq integration adopt-legacy-deliveries --project-id <project> --dry-run
aq integration adopt-legacy-deliveries --project-id <project>
```

It fetches the designated repository once. It adopts a child when a
development delivery lists it and the child's source commit or the delivery's
published commit is on the default branch (`development_delivery`), when the
child's branch tip is (`branch_tip`), or when merging a delivery commit, the
branch tip or the latest completion commit into the default branch changes
nothing, because the work landed under other commits (`content_equivalent`).
It writes one `integration_legacy_deliveries` row per adopted child, and it is
safe to repeat.

| Reason listed | Meaning | Next step |
|---|---|---|
| `not_on_default_branch` | No commit of the child is on the default branch, and merging its work would still change it. `undelivered` names the commit examined, the diffstat and the paths (or a conflict); it is `null` when no commit could be examined, for example because the branch is gone. | Find where the work went. If SHA on the default branch re-delivered it: `--supersede <task> --by <sha> --reason '...'`. If it was abandoned: `--retire <task> --reason '...'` (deletes nothing). If nothing is owed: `--accept <task> --reason '...'`. Otherwise deliver the work. |
| `child_not_completed` | The child failed, so it has nothing to deliver. | Have a human decide, then `--retire` or `--accept` it, or leave it. |
| `parent_not_terminal` | The parent is still open. | Nothing to adopt: its completion needs real train receipts. |
| `state_changed` | The child or parent was reopened while the command ran. | Re-run. |

`--supersede` refuses a commit that is not on the default branch. Each child
takes one decision per run, and every decision needs `--reason`.

`repository_not_designated` names the task in `ref`, with `cause`:
`task_repository_unset` (the task has no repository; tasks created by `aq task
create --graph`, or while the project was in observe mode, are not bound to
it), `task_repository_mismatch`, or `project_repository_unset`.

## A task will not delete, archive, resume or restart

```text
Integration operation <id> is cancelled; its delegate is no longer required
and cannot be resumed or restarted.
```

An integration operation files *delegate* tasks so someone does a piece of its
work: a verifier, a repair-stage writer, a candidate-member resolver. When the
operation is cancelled, superseded by a newer generation, or completes without
ever needing that delegate, the ticket is obsolete — nothing will schedule it,
nothing waits on it, and nobody will ever close it.

```bash
aq doctor --check integration.stranded_delegates          # who is stuck, and why
aq doctor --check integration.stranded_delegates --fix    # settle all of them
aq integration release-delegates OPERATION_ID             # settle just one operation's
```

The fix retires each ticket as a terminal **non-success** — never a
manufactured pass — and records the operation, its state, the disposition
(`cancelled` or `superseded`) and what the delegate still held in
`integration_delegate_releases`. `aq task explain <id>` reads the same facts
back. Cancelling an operation with `aq integration abort` now does this in the
same transaction, so only delegates of operations that ended before this shipped
need the fix.

It settles the *ticket* only. A retained branch owner or workspace lock is
preserved exactly as found and reported as a named cleanup blocker: releasing
either needs proof doctor cannot take, and belongs to the guarded integration
recovery path.

Settling a ticket does not by itself make the task deletable or archivable.
While integration history still names it — an episode or parent verification
(for a root), the operation's `verifier_task_id`, or a candidate resolution —
removal is refused with `integration_owned` naming that table, even after the
operation is over. That is deliberate for now: those four references keep their
foreign keys until
`docs/superpowers/specs/2026-09-20-archive-tasks-with-integration-history-design.md`
is revised and approved. A repair-stage writer with no such reference is
removable once settled.

If the refusal instead names an operation that is `active`, `escalated` or
`human_required`, the delegate is not stranded — it is owned by work still in
flight. Stop that work first:

```bash
aq integration abort <operation-id> --reason "..."
```

## A branch origin was never materialized

Hierarchy and train projects reserve a child's branch before cutting it, and
claiming is gated on that branch existing. A reservation that was never
materialized leaves the task permanently unclaimable.

`BranchMaterializationService`
([`src/integration/branch_materialization.py`](../../src/integration/branch_materialization.py))
drains those reservations from the integration loop. If tasks are still stuck,
check the daemon log for materialization failures, and confirm the branch does
not already exist on the remote with an unexpected tip — the service refuses to
adopt a ref it did not create.

This does not apply to development-mode projects: they cut ordinary task
branches with no reservation step.

## The development publisher has stopped making progress

In development mode the publisher sweeps every project on a timer, assembles a
batch, and files a repair task for each batch it had to park. Two durable
symptoms say it has stopped:

```bash
aq doctor --check integration.development_publisher_stalled
```

* **A batch carries a repeated diagnostic.** When the publisher cannot dispatch
  a parked batch it records `publisher_diagnostic` on that batch's `evidence`
  and moves on to the next one, counting `consecutive_ticks`. Two consecutive
  ticks on the same fault is a stall: the batch state the publisher reads is
  durable, so the tick that failed will keep failing. The check names the batch
  and the cause; read the batch with `aq integration status <project>` and
  either resolve it or cancel it.
* **A passing repair branch was never collected.** A repair closes `pass` on
  `aq/<repair-id>` and the next sweep should pick the branch up. An hour later
  — twelve sweeps at the default interval — with the branch in no batch
  manifest at all, the publisher is not collecting.

The check is report-only. Clearing the diagnostic by hand would only hide the
stall, because the next tick rewrites it.

Two faults this guards against are already fixed, and both came from the
publisher resolving tasks through the live `tasks` table alone. Archiving a
terminal task removes its row, so an archived source read back as *missing*:
the repair generation reset to 1 and defeated the chain bound, an archived
repair was re-filed as a fresh `READY` task on every tick, and a missing source
raised out of the whole sweep — starving every other parked batch in the
project and writing a rich traceback every five minutes. The publisher now
resolves sources and repairs through `archived_tasks` as well, treats an
archived terminal source as already satisfied (its provenance edge is
schema-impossible, since `task_dependencies.depends_on_task_id` references
`tasks.id`, so it is skipped and logged), and isolates each batch and project.

## A branch discard is parked

Deleting a task that owns a materialized branch takes an explicit choice
(`aq task delete <id> --branches keep|delete`). A `delete` marks the retired
origin `pending`, and
[`BranchDiscardService`](../../src/integration/branch_discard.py) removes the
ref asynchronously: it refuses a live branch owner or the default branch, and
compare-and-swaps on the head it observed.

```bash
aq doctor --check integration.branch_discards
aq doctor --check integration.branch_discards --fix
```

Transport failures back off from about 30 seconds towards an hour and are
retried up to eight times before the row parks as `failed`. A *conflict* — the
remote moved — never retries on its own, because it is a statement about the
repository rather than about the network. `--fix` re-arms parked discards for
another attempt.

## Delivered branches are still on the remote

Delivery pushes a branch per task (`aq/<task-id>`), a candidate per batch
(`aq/development/<project>/<head>`), parent assemblies
(`aq/development/parent/…`) and a branch per repair. Once a batch is confirmed
on the default branch, the publisher deletes what it made obsolete on the next
tick: each member's branch (still at the delivered revision, or on `main`), a
`-wip` sibling that is on `main`, every assembly whose members have all landed,
and the branch of a failed repair whose sources reached `main` on their own.

What happened is recorded on the batch's journal row, under
`evidence.branch_cleanup`: `deleted` (branch, sha, kind, and `backup` when it
was bundled), `kept` (branch and why), `missing`, `attempts`, `log`, and
`state` — `pending`, `complete`, or `exhausted` after eight unconfirmed
attempts. Each run that deletes something also logs a
`development.branches_deleted` event.

Everything else is `git.stale_branches` — the supervisor's stall sweep runs it:

```bash
aq doctor --check git.stale_branches        # what is stale, and what holds the rest
aq doctor --check git.stale_branches --fix  # back up and delete the stale ones
```

An `aq/` branch is stale by exactly one rule:

| Rule | When |
|---|---|
| `landed` | Its head is on `main`, or every commit beyond `main` has a twin there with the same author e-mail, author time and subject (a rebased or cherry-picked copy) and every merge beyond `main` is exactly Git's own merge of its parents. |
| `integration` | An `aq/integration/*` ref with at least one `integration_branch_owners` row, all `released`, and every operation tied to it finished. Nothing else lets one go. |
| `expired` | The branch of a FAILED or abandoned (`work_outcome: abandoned`) task, 14 days after it went terminal (the later of its last update and its last close). |

A stale branch stays when anything still references it: a task that can still
run or has a live session, COMPLETED work not delivered yet, an unsettled batch
(and every assembly carrying one of its members), an open repair's sources, an
`integration_branch_owners` row that is not `released`, a live legacy
operation, batch or promotion intent, a live hierarchy branch origin, or a
pending branch discard. `data.projects[].held_examples` names the reference;
on older installs the common one is an owner row left `reserved` by the
hierarchy era. Nothing outside `aq/`, the default branch, `main` or `gh-pages`
is ever deleted — the delete itself refuses.

### Every deletion is restorable

Before anything is pushed, each branch is appended to
`<data_dir>/backups/branch-deletions/<yyyy-mm>.tsv` as
`branch, sha, reason, bundle, recorded_at, repository` (the first two columns
match the supervisor's 2026-09-21 `deleted-branches-*.tsv`), and every tip the
default branch cannot reach is written to a new, verified bundle
`<data_dir>/backups/branch-deletions/<yyyy-mm>/<utc>-<repository>.bundle`. To
put one back, from any clone of the repository:

```bash
git bundle unbundle <bundle>                 # skip when the log says "-": it is on main
git push origin <sha>:refs/heads/<branch>
```

### Undelivered work is not archived

The archive sweeps (hourly auto-archive, and bulk `aq task archive --project-id`)
refuse a COMPLETED task whose delivery has not landed with
`hierarchy.delivery_pending`, including a task whose `repo_id` names another
project's repository, which the publisher never collects.
`aq doctor --check tasks.archive_blocked` lists them. An explicit single-task
archive is not held.

## Slot reset keeps failing

A claim that cannot prepare its worktree slot releases the claim and records
`slot_reset_failure` in the task's metadata with the actual error, the
workspace, the session, the attempt count and whether the next retry is
automatic.

```bash
aq task explain --task-id <id>
```

Retries are automatic with a 120–300 second backoff. After **three**
consecutive failures the task goes `BLOCKED` instead of repeating forever, and
other ready work stays claimable. After fixing the cause:

```bash
aq task resume --task-id <id>
```

That retries a `READY`/`BLOCKED` reset failure immediately and starts a fresh
retry cycle; dependency and approval checks still apply. A successful
preparation clears both the retry ladder and the stale attention flag.

## Repair tasks

A parked content set that the batch did not resolve produces one ordinary task:

* id `development-repair-<manifest digest>`, branch `aq/development-repair-…`,
  type `BUGFIX`, three task retries;
* its description names every parked source SHA and the base to resolve
  against;
* it is a normal queue task — waiting for a worker or a provider does not
  expire it, unlike the strict modes' wall-clock repair stages;
* at most **three** generations of repair are chained. Past that, the content
  stays parked for a human rather than starting an unbounded chain.

A repair may merge, cherry-pick or rewrite the parked changes. Its source
manifest identifies the exact revisions it is responsible for. A passing close
alone does not release their successors: the repair must also have an accepted
delivery to the project's default branch. AQ then records the repair task,
completion and delivery IDs as resolution evidence for the parked sources.
This also reconciles repair-of-repair chains without requiring the original
source SHA to survive a cherry-pick. Missing completion evidence, mismatched
source contracts and unpublished repairs keep the source parked.

Completion records may contain abbreviated Git SHAs. The publisher resolves
these with Git and records the full source SHA against the exact completion ID
in delivery evidence. Ambiguous or missing objects do not qualify. A newer
completion cannot borrow an older completion's resolution, and the original
reported SHA remains intact in the completion record.

If you would rather not have a repair episode run at all:

```bash
aq integration cancel-preserving <operation-id> --reason '…'
```

Refs and attached workspaces are retained; only detached reservations are
released; delegate tasks are paused. If the repair writer's session cannot be
proven stopped by its provider, the command refuses (`stop the current repair
writer before cancellation`, `provider termination proof is unavailable`)
rather than pulling a branch out from under a live process.

## Reading a failure message

Strict-mode repair IDs such as `repair-repair-batch-integration-batch-…-1`
combine the operation ID with a stage number; the repeated prefix does not
mean a repair recursively created another repair. Stage 0 is the primary
repair and stage 1 is its debug escalation.

A repair delegate uses its operation's existing branch. The scheduler accepts
that branch reservation only when the delegate identity, active stage,
operation, repository and branch match. It does not require a separate
task-branch origin for the delegate. Ordinary tasks still require their own
materialized origins in hierarchy and train modes.

When an operation completes or is cancelled, the integration reconciler
(`RepairService.retire_terminal_delegates`) retires its unfinished repair and
verifier tasks. A retired delegate is terminal `FAILED` — non-success, never
runnable again — with an `integration_retirement` record whose `disposition` is
`cancelled` (the operation was cancelled) or `superseded` (it completed without
this delegate). The record keeps the previous status, the previous
`needs_attention` code and any cancellation hold as evidence; retry counters,
branches and stage evidence are untouched, and nothing claims the delegate
passed. A delegate an earlier release left `PAUSED` rolls forward on the next
reconciler tick. Only a session that is not fully stopped defers retirement: the
writer's authority is never taken from it. A pool writer whose process was
confirmed stopped can keep its claim (`claim_phase`) on the stopped session row
as handoff evidence; that row is history, not a writer, and does not defer
retirement. Active operations and operations waiting for a
human decision are unchanged.

Retirement and cleanup are separate. A retained branch-owner row (an attached
dirty checkout, say) or a workspace still locked by the delegate does not keep
the ticket open, and retirement does not release it either. It stays exactly as
the cancellation left it. `aq task explain --task-id <id>` reports
`integration_delegate_retired` for the disposition and one
`integration_cleanup_blocked` reason for each resource still held, read live.
Release those only through the guarded integration ownership controls after
inspecting the checkout for unsent work.

The terminal operation also governs a delegate that has not been retired yet:
explain reports that it is no longer required, rather than suggesting a manual
resume. Resume/restart cannot make a terminal operation's delegate runnable,
orphan-pause recovery leaves it held, and generic `aq task recover` refuses it.
A live writer that tries to close such a delegate is told the operation ended
and not to retry the close, instead of a generic stale-close refusal. Its
pending recovery incident is superseded as retired rather than re-sent.

Every integration command wraps its internal error as
`{"success": false, "outcome": "blocked", "error": "<message>"}`
([`src/commands/integration_commands.py`](../../src/commands/integration_commands.py)),
so the text you see is the exact `ValueError` or `DevelopmentBusy` raised in
[`src/integration/development.py`](../../src/integration/development.py). Search
that file for the message: the surrounding code is the precise condition that
refused.

## Related pages

* [Integration](../concepts/integration.md) — what each state means.
* [Development integration](development-integration.md) — the commands and
  their options.
* [Switching integration modes](integration-migration.md) — mode changes and
  the blockers they hit.
* [Session troubleshooting](session-troubleshooting.md) — when the worker,
  rather than the delivery, is stuck.

## Source and tests

[`src/integration/development.py`](../../src/integration/development.py),
[`src/integration/branch_discard.py`](../../src/integration/branch_discard.py),
[`src/integration/delivery_branches.py`](../../src/integration/delivery_branches.py),
[`src/doctor/integration_checks.py`](../../src/doctor/integration_checks.py),
[`src/doctor/git_checks.py`](../../src/doctor/git_checks.py),
[`src/commands/claim_commands.py`](../../src/commands/claim_commands.py).

```bash
aq test tests/test_development_integration.py tests/test_doctor_integration_checks.py tests/test_branch_discard.py tests/test_archive.py
```
