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
             "checks": [{"command": "…", "exit_code": 1, "output": "…"}],
             "conclusion": "failed"}
"reason": "selected validation failed"
```

The batch was assembled and preserved as a ref, and nothing was published. The
`output` field holds the last 8,000 characters of that command's combined
output — read it before anything else; it is the actual failure.

Exit code 124 means the command hit `timeout_seconds` (default 300) and its
process group was killed. Raise `timeout_seconds` (maximum 3600) with another
`aq integration develop … --reason …`, or make the check smaller.

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

Branch cleanup keeps any branch an `integration_branch_owners` row still names
unless that row is `released`. In the hierarchy modes a row is released when its
task is deleted or archived; nothing in development mode releases one, and
development claims create none. So a project that left `hierarchy`/`train` kept
one row per task it had claimed — `reserved`, or `attached` when a stopped
writer's slot was later reused — and every one of them pins that task's branch
on the remote after it is delivered.

```bash
aq doctor --check integration.finished_branch_owners          # what is held, and why
aq doctor --check integration.finished_branch_owners --fix    # release what is safe
```

A row is released only when all of these hold:

- its owner is a task (`worker`/`repair`; a `collector` row belongs to an
  operation and is never touched) that is `COMPLETED`/`FAILED`, archived, or
  gone from both task tables;
- no hierarchy/train project integrates the repository, as its mode or its
  desired mode — there a finished child's branch is still its parent's to
  transfer;
- no live session names the task, no workspace is locked by it, no candidate ref
  mutation is in flight on the branch and no running integration operation owns
  the task;
- for an `attached`/`handoff_pending` row, its writer session is stopped in both
  `state` and `desired_state` and the session provider confirms, by a fresh
  probe, that the process is gone — the proof `preserve_stopped_owners` takes.
  Run the check through the daemon (the plain `aq doctor` does); without the
  provider such a row is kept, naming why.

Each row is re-proved under the project's hierarchy lock and its own row lock
before the write, so anything that changed after the scan keeps the row. The fix
changes the ownership row only — no checkout, workspace lock, session or task —
gives a released writer's row a fresh fence, and records one
`integration.branch_owner_released` event per row. It is safe to repeat. Once it
has run, re-run whatever branch cleanup was keeping the branches.

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
