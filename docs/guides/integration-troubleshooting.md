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
aq doctor --check integration.branch_discards
aq doctor --check integration.unreviewed_prs
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
| Claim after claim fails preparing the slot | Bounded slot-reset retries | [Slot reset keeps failing](#slot-reset-keeps-failing) |
| `development-repair-…` tasks appearing | Parked content needs a human-shaped fix | [Repair tasks](#repair-tasks) |

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
each sweep, once the session provider confirms the process is really gone.

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
retires its detached, unfinished repair and verifier tasks. They remain
`PAUSED`, with no automatic resume time and an `integration_retirement`
record naming the terminal operation. `aq task explain --task-id <id>` reports
that the delegate is no longer required. This preserves the original task and
stage evidence without claiming that an unused delegate passed. Active worker,
claim, workspace and branch-owner attachments prevent retirement. Active
operations and operations waiting for a human decision are unchanged.

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
[`src/doctor/integration_checks.py`](../../src/doctor/integration_checks.py),
[`src/commands/claim_commands.py`](../../src/commands/claim_commands.py).

```bash
aq test tests/test_development_integration.py tests/test_doctor_integration_checks.py tests/test_branch_discard.py
```
