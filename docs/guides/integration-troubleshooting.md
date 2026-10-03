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
aq doctor --check integration.delivery_path
aq doctor --check integration.operational
aq doctor --check integration.stranded_fences
aq doctor --check integration.stranded_delegates
aq doctor --check integration.stale_schedule
aq doctor --check integration.finished_branch_owners
aq doctor --check integration.reused_task_identity
aq doctor --check integration.branch_discards
aq doctor --check integration.unreviewed_prs
aq doctor --check integration.development_publisher_stalled
aq doctor --check integration.development_conflicts_unrepaired
aq doctor --check git.stale_branches
```

## Symptom index

| Symptom | Cause | Go to |
|---|---|---|
| Task is `COMPLETED`, work is not on `main` | Normal: delivery is batched | [Nothing is wrong yet](#nothing-is-wrong-yet) |
| A task closed `pass`, pushed, and is `BLOCKED` with "Could not authorize PR repository" | Its project has no working delivery path | [A passed task blocked at delivery](#a-passed-task-blocked-at-delivery) |
| `blockers: [{"code": "publication_pending"}]` | A push is unconfirmed | [Publication pending](#publication-pending) |
| Journal row `parked`, evidence `kind: merge_conflict` | A source would not merge | [A member conflicted](#a-member-conflicted) |
| `integration.development_conflicts_unrepaired` reports ERROR | A parked conflict's repair chain ended | [A member conflicted](#a-member-conflicted) |
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
| A train's `aq integration flush` answers `coalesced` every time and no sweep runs | Its outstanding request's batch ended without releasing it | [A train never sweeps](#a-train-never-sweeps) |
| A root batch stays `building` after `construct-and-test` ended `source_moved` | A member's head does not descend from its recorded base | [A batch stops at `source_moved`](#a-batch-stops-at-source_moved) |
| A root batch is green but `main` never moves; `promote-green-candidate` runs end on `wait` | Its stage-zero repair writer held the branch when CI went green | [A green batch never promotes](#a-green-batch-never-promotes) |
| A `dispatch-debug` or `promote-delivery` run ended `busy`, `stale` or `human_required` and the stage has no running writer | A refused dispatch; the service retries it | [Refused runs and hung sources](#refused-runs-and-hung-sources) |
| A child's delivery intent stays `prepared`; later deliveries end `target_moved: target has an unresolved promotion` | Its push was refused after the collector's fence changed | [Refused runs and hung sources](#refused-runs-and-hung-sources) |
| Log line `integration source=<name> exceeded its …s budget` | A remote call hung and was cancelled | [Refused runs and hung sources](#refused-runs-and-hung-sources) |
| A repair stage passes its deadline but no stage 1 (or 2) appears; supervisor message "is waiting for a writer" or "Repair dispatch … is retrying" | Its writer was never claimed, is live, or stopped without publishing; or dispatch met a mechanical `unknown` | [A repair stage passes its deadline without escalating](#a-repair-stage-passes-its-deadline-without-escalating) |
| A train root is `COMPLETED` with no PR; its checkpoint stays `working` | The root was never given, or never took, its pull request | [A completed root has no pull request](#a-completed-root-has-no-pull-request) |
| A child is `COMPLETED`, its parent stays `PAUSED`, and siblings sit `READY` but are never claimed | The parent never assembled the child: no approved evidence pins its head | [A completed child is never assembled](#a-completed-child-is-never-assembled) |
| `redrive-child` answers "the parent has no live collection operation"; a later child's conflict never gets a repair | `cancel-preserving` cancelled the parent's whole collection operation | [A parent's collection was cancelled](#a-parents-collection-was-cancelled) |
| An aggregate verifier's `pass` close is refused with `awaiting_trusted_verification`, or repeats on an unchanged head | The daemon has not recorded the trusted CI evidence for that parent generation and head | [An aggregate verifier keeps refusing its close](#an-aggregate-verifier-keeps-refusing-its-close) |
| `integration status` shows `draining: true` and the drain never finishes | Stale owners, leases or cleanup from an old train run | [A drain never completes](#a-drain-never-completes) |
| An open PR the train will never seat (untracked branch, child with no repository, parent with no verification), or one whose work already landed under other commits | Legacy delivery the train has no identity for | [A legacy PR stays open](#a-legacy-pr-stays-open) |
| Observe status lists `missing_receipt` with cause `no_parent_collection` | Children of parents that finished before the train | [Legacy children block observe readiness](#legacy-children-block-observe-readiness) |
| A delivered branch is kept because `integration owner … is reserved` | An ownership row a finished task never let go | [A finished task still owns its branch](#a-finished-task-still-owns-its-branch) |
| Stale `aq/…` branches pile up on the remote | Held, older than cleanup, or cleanup exhausted | [Delivered branches are still on the remote](#delivered-branches-are-still-on-the-remote) |
| `hierarchy.delivery_pending` when archiving | The work has not reached `main` | [Delivered branches are still on the remote](#delivered-branches-are-still-on-the-remote) |
| Claim after claim fails preparing the slot | Bounded slot-reset retries | [Slot reset keeps failing](#slot-reset-keeps-failing) |
| `development-repair-…` tasks appearing | Parked content needs a human-shaped fix | [Repair tasks](#repair-tasks) |
| Parked batches never progress; `daemon.log` grows fast | The publisher is stalled on one batch | [The development publisher has stopped making progress](#the-development-publisher-has-stopped-making-progress) |
| A repair closed `pass` but its branch is in no batch | The publisher is not collecting | [The development publisher has stopped making progress](#the-development-publisher-has-stopped-making-progress) |
| Supervisor message "Development publisher stalled on N candidate(s)"; doctor ERROR `candidate_stalled` | The same skip repeated `integration.publisher_stall_after` times | [The development publisher has stopped making progress](#the-development-publisher-has-stopped-making-progress) |

## A passed task blocked at delivery

The worker pushed `aq/<task>` and closed `pass`; git verification then blocked
the task (context `session_close_pipeline_stop`) with:

```text
Could not authorize PR repository: GitHub repository reference was invalid
```

The task's integration mode was `pull_request`, and its repository cannot host
a pull request: a bare repository on disk (`~/.agent-queue/local-remotes/<name>.git`)
or a host other than github.com. Since 2026-09-27 a repository on disk no longer
*inherits* `pull_request` from `integration.default_mode`; its tasks integrate
`direct` (`aq task show` reports `effective_integration_mode: direct` from the
`repository` policy), and the completion pipeline pushes the merged task branch
to the default branch as a fast-forward without touching the base checkout.

Two things are left for a human:

1. **Projects that still have no path.** A hosted remote other than
   github.com (it keeps the `pull_request` default so its work never lands
   unreviewed), an explicit `pull_request` on a repository on disk, a
   hierarchy/train project off github.com, a managed mode without an
   integration repository, or a repository on disk that is gone:

   ```bash
   aq doctor --check integration.delivery_path
   ```

   Enable the development publisher for the project
   (`aq integration develop <project> --validation ... --reason ...`), which
   delivers to any repository, or point it at a repository that exists.

2. **Tasks already blocked.** Deliver the pushed branch by hand — the local
   operator or the project's live supervisor:

   ```bash
   aq task deliver --task-id <task> --reason "passed; no PR possible" --dry-run
   aq task deliver --task-id <task> --reason "passed; no PR possible" \
       --expected-head <head_sha from the dry run>
   ```

   It fetches into a private repository under the daemon's data directory,
   fast-forwards or merges the branch into the default branch, pushes with a
   lease on the default branch as fetched, and completes the task (context
   `operator_delivery`, metadata `manual_delivery`) only if it is still
   `BLOCKED`. It refuses a task that is not `BLOCKED`, whose last close was not
   a pass or that was blocked for another reason (a timeout, an operator stop,
   spent retries), has open children, belongs to a development/hierarchy/train
   project, integrates by pull request on a repository that can host one
   (merge the PR instead), was never pushed, moved from `--expected-head`,
   carries daemon bookkeeping such as `.aq/claim.json`, or conflicts
   (`conflict_files` names the files; resolve on the branch, push, and deliver
   again).

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
* Otherwise AQ files one repair task — see [Repair tasks](#repair-tasks). If
  `main` moves again before that repair is published, the repair's own row
  parks and gets the next repair; the source is carried by that chain.

To see what is carrying a parked source, or that nothing is:

```bash
aq doctor --check integration.development_conflicts_unrepaired
```

It lists each completed source parked on a conflict whose repair chain has no
open repair (none filed, one ended FAILED or BLOCKED, or the generation budget
ran out), with the conflicting files and the chain. `aq integration sweep
<project> --recover-child <task>` also names the repair carrying the child when
the child stays unpublished. Do not file a hand-made rebase task for a source
the check does not list: the repair chain is already on it.

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
being published nor the SHA it was published from, and git cannot tell whether
the publication is in its history. The sweep stops rather than guessing.

Movement git *can* explain settles on its own. When the target moved before a
publication applied — during validation, or under the expected-old-SHA lease
of the push — the row becomes `cancelled` with `evidence.reason: base_moved`
and the observed SHA, and the sweep re-assembles once from a fresh fetch; a
second move waits for the next tick. A `publishing` row left by a crash whose
prepared head is not in the moved target becomes `cancelled` with
`evidence.reconciled: target_moved`. Neither parks the batch or files a repair,
and the moved target is never overwritten.

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

`aq task deliver` takes the local operator or the project's live named
supervisor, like the other integration controls.

`aq integration status` is readable by a worker session for its own project,
which is why a worker can diagnose but not act.

## A task inherited an older branch origin

An older install could reuse a deleted task ID even though its integration
origin, checkpoint or ownership row survived. The new task could then start
from the predecessor's base or fail at close with a released delivery fence.

```bash
aq doctor --check integration.reused_task_identity --json
```

This report lists unretired origins created before their current task, including
completed tasks, with the exact origin ID, branch, base, timestamps and checkpoint.
It runs in every integration mode. Retired origins and orphan origins without a
live task are not included. Timestamp anomalies warrant review; they are not
proof that branch contents can be discarded.

The check has no `--fix`. Releasing an owner leaves the old origin and
checkpoint in place. The guarded repair proves the predecessor's branch first:

```bash
aq integration rebind-reused-identity --task-id TASK_ID            # dry run
aq integration rebind-reused-identity --task-id TASK_ID --apply \
  --origin-id ORIGIN_ID --reason "inherited a deleted task's identity"
```

The dry run fetches the origin's repository. It answers `would_rebind` when the
origin's base, the predecessor's checkpoint and the ref's current tip are all on
the default branch; a ref that is gone is accepted too. Otherwise it answers
`unproven` and lists each cause:

| Cause | Meaning |
|---|---|
| `live_writer` | The task is claimed, has a live session or holds a workspace |
| `owner_held` | The ref's owner row is not `released`; settle it with `release-owner` or `release-stale-owners` |
| `dependent_history` | Child origins, episodes, receipts, batches, intents, repairs, dispositions or review evidence use the identity |
| `checkpoint_rewritten` / `checkpoint_mismatch` | The checkpoint cannot be attributed to the predecessor |
| `hierarchical_project` | In `hierarchy`/`train` mode the origin is live delivery identity; the control refuses |
| `not_on_default_branch` / `unavailable` | A predecessor commit is not on the default branch, or is not in the fetched store |

A commit that is not on the default branch is abandoned only on an explicit
decision, by naming it exactly with `--discard-tip SHA`. `--apply` needs every
origin id the dry run printed and a reason. It re-proves everything under the
project hierarchy lock, retires the origin (the row is kept), moves the
checkpoint into an `integration.task_identity_rebound` event and changes no
branch or owner row. The [diagnostic contract](../specs/design/integration-identity-diagnostics.md)
describes the proof and its limits.

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

`release-owner` takes no `--project-id`: the task (or the owner row's repository) names the
project, and the caller's token has to name the same one. A project's supervisor session may
therefore run this recovery for its own work and is refused for another project's — see
[aq-surface §7.3](../specs/design/aq-surface.md#73-elevated-scopes-and-the-commands-that-carry-no-project_id).

**Refusal reasons and how to resolve them.**

Every refusal is recorded in `integration_owner_recoveries` with the row's
`evidence` field populated — the `evidence.detail` string has the same wording
as the doctor check's report. Dry runs do not write an audit row.

| Refusal | Meaning | Fix |
|---|---|---|
| `writer_live` | The provider probe says the writer is still running, or a newer live session names the task. | Wait for the writer to exit, or confirm the task is truly cancelled/archived, then retry. A drain-acked pool worker holding a retired repair delegate, or still bound to a task whose close already committed, is stopped automatically (see the repair-delegate section and the paragraph below this table). |
| `checkout_in_use` | A live session holds the worktree the branch is checked out in. | Close that session or hand it off, then retry. |
| `origin_unreachable` | `git fetch` or the preservation push failed (network, auth, rate limit). | Fix connectivity or auth, then retry. |
| `stale_fence` | The row changed between the check and the release — a new writer attached, or the fence was bumped by another recovery attempt. | Re-run; the new writer will be the one the check evaluates. |
| `not_found` | The owner row id is wrong, or the row was already released by the time the command ran. | Look up the current row id: `aq doctor --check integration.stranded_fences`. |
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
`doctor` and the manual command remain available regardless.

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

## A train never sweeps

A train project has at most one sweep request in flight, in
`project_integration_schedules.outstanding_request_id`. Every later flush and
periodic tick coalesces into it, and only releasing its *promoted* batch frees it.
A batch that ended any other way used to keep it forever: an operator
`integration abort`, a development `cancel_preserving`, a seal that never
produced a batch, or a promoted batch whose lease `release-stale-owners` already
dropped. `aq integration flush` then answers `coalesced` indefinitely, and
`last_completed_sweep_at` stops moving.

The scheduler now frees such a request itself on the next tick or flush that
reaches the project, and both abort paths free it as they end the batch. To
see where a project stands:

```bash
aq doctor --check integration.stale_schedule        # every train project
aq integration clear-stale-request <project>        # one project, dry run
```

| Verdict | Meaning | Next step |
|---|---|---|
| `none` / `active` | No request, or the train still owns it (a live batch, or a promoted one holding the lease that release consumes). | Nothing. |
| `in_flight` | No batch yet; `integration.sweep_due` is waiting for a playbook to accept it. With repeated attempts and a `last_error`, doctor lists it. | Fix the route the error names; clearing it would not help. |
| `unsealed` | The event was accepted less than an hour ago, but no batch is sealed. | Wait, or clear it now with `--apply`. |
| `stale` | Nothing will ever end it. | The scheduler frees it; `aq doctor --check integration.stale_schedule --fix` or `--apply` frees it now. |
| `blocked` | Nothing will end it, but the batch still has unresolved write evidence (a running repair operation, a reserved ref mutation or attestation, a reserved or pushed resolution, an unsettled promotion intent), or another batch holds the project lease. | Settle what `blockers` names; never force it. |

Apply with the request id the dry run printed and a reason:

```bash
aq integration clear-stale-request <project> --apply \
  --request-id integration-sweep:<project>:<n> --reason '<why>'
```

A request that changed since the dry run is refused (`changed`). Freeing the
request never touches Git or the batch row. It deletes the ended batch's own
fenced lease, turns a catch-up recorded against the old request into the next
request, and records an `integration.schedule_request_released` event.

The latched `next_due_at` in the past is not part of the fault. A periodic
sweep also waits for the approval-armed settling window, so with no new
approval `next_due_at` stays at the missed boundary, and the sweep runs as soon
as an approval arms the window. `aq integration flush` bypasses that window.

## A batch stops at `source_moved`

A train member merges `recorded base..reviewed head` onto the candidate, so its
recorded origin base must be an ancestor of its reviewed head. A worker that
stacks its branch on another line, or resets it, can drop that base while the
tree stays correct. On 2026-10-01 `fresh-flare-12` (base `f736edeb`, head
`82ae322c`, merge-base `82b19024`) was admitted and sealed. Construction answered
`source_moved` after stage 0 had started, the `root-train` run ended `failed`,
and the batch sat `building` with no delegate while the scheduler renewed its
lease. The operator recovered with `integration cancel-preserving` and
`reopen-with-feedback`.

The train now does this itself:

- **Admission refuses the identity.** No approval is recorded for a head that
  does not descend from its recorded base (`invalid_ancestry`), whether it comes
  from GitHub, task authorization or a reviewer task.
- **Construction withdraws it.** Before applying any member, the build proves
  every member's ancestry and tree. A Git-proven failure records rejected review
  evidence (`reviewer_identity: integration:source-ancestry`, `decision_path:
  source_ancestry_invalid`, with base, head, merge-base and feedback). It then
  cancels the operation and its stages, ends the batch `aborted` with members,
  revisions, refs and evidence intact, and frees its lease and request. The
  sweep's own trigger becomes the catch-up, so the valid members reseal at once
  on observed main. An `integration.batch_source_withdrawn` event lists what was
  withdrawn. Nothing is withdrawn while a writer, ref mutation, resolution,
  attestation or promotion intent is unresolved; the build retries instead.
- **The source is repaired on its own branch.** On its next pass the review
  poller finds the withdrawn identity still `COMPLETED`. Under
  `root.repair.source_ci` it reopens the task with feedback: merge the recorded
  base (never rebase or force-push), re-run checks, publish and close. The new
  head is a new identity and is admitted normally. The source is reopened at
  most once per identity. Without that authorization, for a tree mismatch, or
  when the same head comes back, the supervisor gets one message naming the
  identity, the reason and the `aq task reopen-with-feedback` command.
- **Construction is never left without a next step.** `base_moved`, and a
  `source_moved` that does not withdraw, schedule `integration.sealed` again after
  60 s while the batch is still `sealed` or `building`, keeping at most one
  undelivered retry per batch revision. A stage-0 deadline that finds the batch still `building` with no
  writer re-drives construction once and escalates only if it is still unfinished
  ten minutes after that re-drive was delivered (or enqueued, if nothing took it).

The batch's `human_abort_reason`, the rejected evidence row and the
`integration.batch_source_withdrawn` event all name the exact identity and its
merge-base. Design:
[invalid source ancestry recovery](../superpowers/specs/2026-10-01-invalid-source-ancestry-recovery-design.md).

## A green batch never promotes

Under the continuous repair policy a batch's stage-zero repair writer is
dispatched as soon as its candidate is built, so it is usually still
attached to the integration branch when CI turns that candidate green.
Promotion needs the batch collector's fence, so the first
`integration.candidate_green` run ends on `wait`. Its `error` names the
holder, for example `integration branch is held by repair repair-…-0
(attached, fence 2, attached to session …)`. That is expected.

When the writer closes on the unchanged candidate, the branch goes back to
the collector. This happens through `continue-closed-root-repair`, or within
a few seconds through the integration service's green-continuation pass, and
a fresh continuation promotes the batch. A restart, a duplicated event or a
long-expired stage deadline converges on the same single promotion. Nothing
re-runs CI, rebuilds the candidate or files a new repair task. The daemon
log line `green root batch <id> promotion continuation <outcome>: <reason>`
is written when a batch's state changes:

| Outcome | Meaning | Next step |
|---|---|---|
| `blocked` | The snapshot is not promotable. The reason names the writer still attached, a short or foreign lease, or an unpublished candidate. | Wait for that holder. An attached writer is never taken over. |
| `handed_off` / `continued` | A continuation was enqueued. | Nothing. |
| `pending` / `backoff` | A continuation was just delivered; retries double from 60 s up to one hour and never stop while the authority is unchanged. | Nothing. From the sixth continuation the line is logged as a warning: read the promotion refusal it keeps hitting. |

A live supervisor can re-drive the batch. `integration_promote_main` and a
manual `aq playbook run` of the root train execute as the supervisor session.
They reload every authority and finish a closed writer's handoff themselves,
and they never accept CI evidence from the caller:

```bash
aq playbook run --playbook-id agent-queue-root-train \
  --event '{"type": "integration.candidate_green", "event_id": "<unique id>", \
            "project_id": "<project>", "operation_id": "<operation>", \
            "batch_id": "<batch>", "revision": <n>, "head_sha": "<tested sha>"}'
```

Worker sessions still get `unauthorized`.

## Refused runs and hung sources

A playbook rule runs once per event. When its command answers with a typed
refusal (`busy`, `stale`, `wait`, `human_required`) the run ends failed and
nothing in the playbook looks again. The integration service re-finds that work
from durable rows on every pass instead, so a lost or refused event does not
leave a subject waiting:

- **Repair dispatch.** Every active repair stage, under any `on_exhausted`
  policy, whose writer was never launched (no delegate, or a delegate still
  `PAUSED` because the branch never passed to it) is dispatched again through
  `integration_repair_dispatch`. The first sight dispatches at once; each later
  attempt waits twice as long, from 30 s up to 10 minutes. The log line
  `integration repair dispatch operation=<id> stage=<n> outcome=<outcome>` is
  written when a stage's outcome changes. Operations a human must decide
  (`human_required`), supervisor-recovery stages and delegates an operator
  paused are never selected.
- **Green promotion.** See [A green batch never promotes](#a-green-batch-never-promotes);
  continuations never stop, at most an hour apart.
- **Parent delivery intents.** A child delivery whose exact commit is already on
  the parent branch is finalized. A `prepared` intent whose push did not apply
  is pushed again with its own frozen identity under the parent's *current*
  reserved collector fence (the same expected-old push the original run would
  have made), after a 60 s grace and with the same backoff. A branch held by a
  repair or verifier writer is a wait. Under the current collector fence, a
  diverged prepared intent is superseded only after read-back proves its commit
  is absent from the target. Collection then queues a fresh attempt, retaining
  the old intent, recovery ref and receipt identity. An intent whose commit was
  never built (`reserved`) gets a durable `delivery.ready` continuation so the
  parent playbook can rebuild it and handle any conflict. Pending continuations
  deduplicate across restarts; delivered but refused continuations retry with
  exponential backoff. Ended operations and operator pauses remain held.

Every source of the pass and every item of a page the service iterates itself
runs under a budget: `integration.service_source_timeout_seconds` (300 s),
`integration.service_item_timeout_seconds` (60 s) and per-source
`integration.service_source_timeouts`, keyed by the name in the log line. A
call past its budget is cancelled, `integration source=<name> exceeded its …s
budget` is logged, and the other sources run; the work stays retryable on the
next pass. A call that ignores cancellation is left to finish on its own and its
source is skipped (`skipped until its timed-out call finishes unwinding`) until
it has, so one hung GitHub or Git call no longer stops CI observation, repair
deadlines, intents, cleanup and drains. A source that times out on every pass
because it is slow rather than hung needs a larger per-source budget.

## A repair stage passes its deadline without escalating

A repair stage's clock only escalates (opens the next stage, or blocks for a
human under a finite policy) when its writer had a conclusive CI attempt or
moved the subject head. Otherwise the continuing ladder reads the delegate's
state at the deadline and records each decision in the stage dossier's
`deadline_deferrals` (the last ten, plus `deadline_deferral_count`):

| `reason` | Meaning | What happens |
|---|---|---|
| `writer_unclaimed` | The delegate is `PAUSED`/`READY` and nobody claimed it: capacity, not failure. | Deadline moves by one primary budget; one supervisor message per stage. Check pool capacity for the stage's class. |
| `writer_live` | Its session is still live. | Revisited every 5 minutes; the writer keeps its fence. |
| `operator_hold` | The delegate carries a `manual_pause` hold. | Revisited; never refiled or dispatched around the hold. |
| `writer_refiled` | It stopped without publishing anything; the owner recovery proved the stop. | The same ordinal and delegate get a fresh clock (`writer_refiles` in the dossier) and are dispatched again. |
| `stale_fence`, `stop_proof_unavailable`, `checkout_in_use`, `origin_unreachable`, `stale_claim` | The stop could not be proven, or the fence is someone else's. | Revisited every 5 minutes; one supervisor message per stage and reason. Fix the named cause (for example [a branch held by a writer that is gone](#a-branch-is-held-by-a-writer-that-is-gone)). |

A stopped writer with unpublished commits is not refiled: the stage rolls over
to its successor, which resumes the preserved tip. A stage is refiled at most
once, and at most three writers start on one unchanged subject head; after that
the ladder's no-progress guard ends the budget once with a
`Repair … stopped without progress` message.

### A parent conflict resolved, and what the ladder does next

A parent conflict repair ends with one recorded resolution on its promotion
intent: a frozen head, the authoring fence and the observed push. That head is
the parent's new aggregate subject, so what remains is verification of it, not
another repair writer. A stage whose subject is that recorded resolution head
therefore ends `passed` (`resolution_verification` in its dossier names the
intent and head) and hands the parent back to its collector; the ordinary
readiness projection, verifier wake and exact-head CI then run on the new head.
A stage that changes nothing on such a subject no longer produces a
`Repair … stopped without progress` incident, and a successor stage is only
allocated when a conclusive failure is recorded at that head — which the
successor's dossier then carries.

If a resolution was recorded but the aggregate head still does not include it
(the stage closed without a commit proof, or its extension edge is missing), the
operation stays blocked for a human: reconcile it through
`aq integration recover-parent-head OPERATION_ID --head SHA`, never by
dispatching another repair stage.

`integration_repair_dispatch` answers `unknown` (with `reason` and
`reason_code`) for a state it did not expect — a missing or mismatched delegate,
an id collision, a missing or non-predecessor owner, an incoherent handoff.
Nothing is consumed and the continuation passes retry it; reviewed playbooks
see it as `busy`, and the supervisor hears once per operation and reason.
Only an operator hold on the delegate, or preserved progress that no longer
proves its lineage, stays `human_required`.

## A completed root has no pull request

A train seals a root only when it is `COMPLETED`, has a pull request, and has
an approved GitHub review of its exact head (`eligible_root_page_on`). Nothing
else seats it, so a root with no PR never delivers, and `integration flush`
cannot help: it only links a PR that already exists.

Two paths open the PR. An epic's opens when its aggregate verification
completes (`ParentCompletion.complete_parent`). A childless root, which
`task create` files onto its own `aq/epic/...` branch in a train project,
closes through the leaf checkpoint: the close advances `checkpoint_sha` to
the pushed head and opens the PR. Its checkpoint `state` stays `working` —
that is the leaf's finished shape, not a stall — and its `reserved` worker
owner row is the normal post-close state until the train delivers it. Before
2026-09-25 the leaf close opened no PR (noble-harbor-74).

Both are best-effort, so the daemon retries: `RootPullRequestReconciler`
(`src/integration/root_pull_requests.py`) opens the PR of any COMPLETED train
root whose checkpoint names a finished head, backing off per root. It skips
unverified epics, roots already delivered, and heads already on the default
branch. To see where one root stands:

```bash
aq integration redrive-root <task>        # dry run
```

| Outcome | Meaning | Next step |
|---|---|---|
| `would_open` | Complete, head published, no PR. | `--apply --head <head_sha> --reason '<why>'`. |
| `nothing_to_redrive` | Already has a PR, delivered, or its head is already on the default branch (a fix-forward merged by hand). | Nothing; `reason` says which. |
| `blocked` | An epic whose aggregate verification has not completed, a leaf whose close recorded no head, an unpublished branch, or a remote branch that moved from the recorded head. | Settle what `reason` names; never force it. |
| `not_eligible` | Not a train root, not `COMPLETED`, or no integration checkpoint (the legacy completion pipeline owns that PR). | Nothing to redrive here. |

Applying needs the head the dry run printed; a head that changed since is
refused (`changed`). It opens the PR for that head, stores it on the task and
records an `integration.root_redriven` event.

## A completed child is never assembled

A parent collecting its children (`PAUSED`, checkpoint `awaiting_children`)
assembles a COMPLETED child only once an approved review-evidence row pins the
child's exact checkpoint head (`CollectionService.queue_next`), and
`delivery_promote` re-checks that row before it pushes. Until the child is
assembled no receipt reaches the parent, so every sibling whose `needs` names
it stays out of the claim frontier: the pool counts `ready=0` while the
siblings show `READY`. The child's own checkpoint `state` stays `working`; that
is the finished-leaf shape, not the stall.

Before 2026-09-25 nothing wrote that evidence. Automatic per-task reviews were
retired on 2026-09-09, and GitHub review ingestion covers roots only, so every
child of a train epic stalled after its close (vivid-ridge; sharp-impact). The
collector now proves each completed child without a verdict from Git -- the
remote branch tip is the recorded head, which descends from the child's
origin base -- and records `leaf` completion evidence for that head and tree
(`reviewer_identity` `completion:<task>`). It leaves a head a reviewer
rejected, and a child with an open reviewer task, alone, and backs off a child
whose proof fails.

`aq doctor --check integration.stuck_children` lists children still waiting
five minutes after their close, with the verdict on their head. To see where
one stands:

```bash
aq integration redrive-child <task>        # dry run
```

| Outcome | Meaning | Next step |
|---|---|---|
| `would_advance` | Complete, head published and proven, not yet assembled. | `--apply --head <head_sha> --reason '<why>'`. |
| `nothing_to_redrive` | Already delivered into the parent, or a promotion of this head is being written or repaired. | Nothing; `reason` says which. |
| `blocked` | A reviewer rejected the head or is still open, the remote branch moved or is unpublished, the checkpoint is still the origin base (a no-code child), or the parent is not collecting. | Settle what `reason` names; a no-code child takes `aq integration record-noop`. Never force it. |
| `not_eligible` | Not a hierarchy/train child, not `COMPLETED`, a nested parent, or no checkpoint. A root takes `redrive-root`. | Nothing to redrive here. |

Applying needs the head the dry run printed; a head that changed since is
refused (`changed`). It records approved evidence for that head (the operator
as reviewer, the reason in the evidence), queues the parent's collection, and
logs an `integration.child_redriven` event. Promotion, the receipt and the
parent's readiness then follow the normal path.

## An aggregate verifier keeps refusing its close

```text
close refused: Parent integration completion was refused: awaiting_trusted_verification
(verification_not_recorded). Trusted integration check evidence is not recorded for parent
<parent> generation <n> at <head>; required producer <producer> version <v> covering <checks>.
```

A parent's branchless verifier (`verify-<operation-id>[-g<n>]`) proves the
collected aggregate itself, then completes the parent through
[`integration_complete_parent`](../reference/playbook-commands/integration_complete_parent.md).
That completion needs one more thing the verifier cannot produce: the trusted
check evidence for that exact generation and head. Only the daemon's parent CI
producer (it publishes the frozen `aq/parent/…` snapshot and observes the
required checks) and the parent-integration playbook (`integration.ci_completed`
→ `integration_parent_verify`) may record it. So while the evidence is still
missing the verifier keeps its claim, its fence and its `IN_PROGRESS` task, and
the refusal says a re-run of the local suite cannot change it — re-running the
whole focused/schema sweep per attempt is pure waste on an unchanged aggregate.

Before 2026-10-03 this wait answered as `stale_verification`, an undifferentiated
"something moved" that invited exactly that re-run; calm-grove-25 generation 5
spent two 450-test sweeps on one unchanged head before the retry budget ran out
and a fresh `-g6` verifier was filed.

| What you see | Meaning | Next step |
|---|---|---|
| `awaiting_trusted_verification (verification_not_recorded)`, no recorded evidence | Nothing has verified this generation and head yet. Normal while CI is running. | Wait. `aq integration status --project <project>` shows the parent's blockers; `aq integration flush <project>` re-drives the sweep. |
| The same, with green evidence already recorded for that head | CI went green and the parent-integration playbook has not accepted it yet. | Read the playbook's last `verify-parent` rule; its own outcome says why `integration_parent_verify` did not record the verification. |
| The same, with a recorded failing conclusion | The aggregate did not pass its required checks; the repair ladder owns the next step. | `aq integration status --project <project>` for the active stage. A new aggregate needs a new CI run, not a re-run of the local suite. |
| `… (verified_other_head)` / `… (verified_other_generation)` | A verification exists for a different subject. | The collected aggregate moved; the current one needs its own CI run. |
| `… (verification_record_missing)` | The checkpoint names a verification that does not resolve to this operation. | A durable gap, not a pending run: read `aq integration status` for that parent. |
| `stale_verification` | The collected aggregate advanced past the head the close quoted. | A genuinely superseded subject; the verifier re-reads readiness. |
| Refusal 2+ on the same subject, `needs_attention=awaiting_trusted_verification:…` | The wait is stalled, not pending: the verifier re-attempted the close with unchanged evidence. The task is flagged and no new attempt is warranted. | Settle whichever owner the refusal names, then report with `aq message send --to user:dashboard`. |

Every refusal records its subject/evidence state on the verifier task under
`integration_trusted_evidence_wait`, so a replay on unchanged state is
deduplicated into one escalation instead of another invitation to re-run.

## A parent's collection was cancelled

```text
redrive-child: blocked — the parent has no live collection operation for its current episode;
a cancelled one is reopened with `aq integration reopen-collection <parent>`
```

A collecting parent has exactly one repair operation per episode, and it is
also the parent's *collection* operation: the collector's fence names it, every
child's receipt is bound to it, and its repair stages resolve the children's
conflicts. `aq integration cancel-preserving <operation>` on that operation —
typically meant to retire one expired repair stage — stops the whole
collection. The parent stays `PAUSED` with an `awaiting_children` checkpoint in
the same episode, the delivered receipts stay bound to it, the collector's
fence is released and the stage delegates are settled and archived. Nothing
continues it: the collector and `redrive-child` need a live operation,
`reserve_episode_on` returns the cancelled one, `aq integration resume` needs a
`human_required` operation and the delegate in `tasks`, and the next child's
conflict intent never gets a repair stage (calm-grove-25 and azure-vault-92,
2026-10-02).

Reopen it in place, so every receipt stays valid as recorded:

```bash
aq integration reopen-collection <parent>        # dry run
aq integration reopen-collection <parent> --apply --head <head_sha> --reason '<why>'
```

The dry run proves the parent's remote tip is the recorded collection head
(the current conflict's old tip, else the last receipt's head) and that every
receipt's head is still on the branch. It reports the operation, episode,
owner, receipts, the settled or archived delegates, any open human gates and
the one current conflict with the stage it would open.

| Outcome | Meaning | Next step |
|---|---|---|
| `would_reopen` | The operation is `cancelled` and nothing can still write. | `--apply --head <head_sha> --reason '<why>'`. |
| `nothing_to_reopen` | The collection operation is already live. | `redrive-root` / `redrive-child` for the stall you see. |
| `ambiguous` | A promotion, resolution, ref mutation or attestation of this operation or branch has an unknown outcome. | Reconcile that write first; it is never replayed. |
| `blocked` | A branch owner that is attached or held by another writer, an unsettled or attached stage delegate, a verifier, a `manual_pause`, a draining project, more than one conflict, a conflict for a child that moved on, or a remote tip that is not the recorded head. | Settle what `reason` names (`aq doctor --check integration.stranded_delegates --fix` settles delegates). Never force it. |
| `not_eligible` | Not a collecting parent, or the operation is `human_required` (`aq integration resume`) or `completed`. | Nothing to reopen here. |

Applying needs the head the dry run printed and a reason; state that changed
since the dry run is refused (`changed`). In one transaction it reclaims the
collector reservation for the same operation at the next fence token
(nothing holding an older token can write), returns the operation to `active`
or `escalated`, and — when one conflict waits — opens a fresh repair stage
bound to that conflict with its own deadline and budget. The daemon then files
a new delegate `repair-<operation>-<stage>`; archived delegates are never
restored and the cancelled stages stay cancelled. A parent the exhausted repair
terminally `BLOCKED` returns to `PAUSED`. Reopened with no conflict waiting,
the collection's next conflict opens a fresh stage past the cancelled one on
its own. If the dispatch fails, the daemon's continuation sweep retries it.
Gates and holds are untouched. Each
apply logs an `integration.collection_reopened` event with the operator, the
reason, the previous owner and fence, the receipts and the new stage.

To retire only an expired repair while children are still landing, do not
cancel the operation; let its deadline escalate or run `aq integration
resume <operation>` once it asks for a human.

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

Status already accepts a child whose latest completion git finds on the
default branch (the shared delivery evaluator, fetched outside the status
snapshot); no delivery row is consulted. A child closed before completions
retained their exact source in git is unknown to that evaluator, so it is
flagged until this control proves it. Run the control for the rest, as a
local operator or the project's supervisor:

```bash
aq integration adopt-legacy-deliveries --project-id <project> --dry-run
aq integration adopt-legacy-deliveries --project-id <project>
```

It fetches the designated repository once. It adopts a child when git proves
its latest completion on the default branch (`development_delivery`), when the
child's branch tip is (`branch_tip`), or when merging the branch tip, the
latest completion commit or a source a retired development journal row names
(kept as a `development.legacy_provenance` event) into the default branch
changes nothing, because the work landed under other commits
(`content_equivalent`). A retired row only locates a source; its state, and an
assembly's published commit, prove nothing.
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

## A legacy PR stays open

The train seats only completed roots with an integration checkpoint, so some
legacy PRs have no route at all: an operator branch with no task, a child
filed before its project was bound (`repository_not_designated`), or a parent
root with no aggregate verification. `materialize-root`, `redrive-root`,
`redrive-child` and `bind-legacy-repositories` refuse them correctly; never
forge a checkpoint, receipt, verification or approval to get past them.

Deliver the work through a fresh *carrier*: an ordinary root task whose branch
merges each legacy head exactly (`git merge --no-ff <sha>`; never rebase,
squash, cherry-pick or amend). It takes the normal path — source CI, authorized
admission (a type other than feature/bugfix needs its id in
`root.authorized_task_ids`), batch conflict repair, candidate CI, promotion.
GitHub marks a PR merged once its exact head is reachable from the default
branch, so every carried PR closes by itself when main fast-forwards. Then
prove the legacy tasks with
[`bind-legacy-repositories`](#legacy-children-block-observe-readiness) and
`adopt-legacy-deliveries`.

A PR whose work landed under other commits stays open. Close it only on Git
proof:

```bash
aq integration close-delivered-pr <project> <number>        # dry run
aq integration close-delivered-pr <project> <number> --apply --head <head_sha> --reason '<why>'
```

| Outcome | Meaning | Next step |
|---|---|---|
| `would_close` | `proof.kind` is `ancestor` (the head is on the default branch), `patch_equivalent` (a linear series whose every commit has a patch-identical commit on the default branch, listed in `equivalent_commits`) or `content_equivalent` (merging the head changes nothing). | Apply with the reported `head_sha`. |
| `undelivered` | No proof reaches it. `undelivered` lists the commits and the conflict or the files merging would still change. | Deliver the work (a carrier root); never close it by hand. |
| `nothing_to_close` | The PR is already closed or merged. | Nothing. |
| `changed` | The PR head moved, the remote branch is not the PR head, or `--head` is not the reported head. | Run the dry run again. |
| `not_eligible` | The PR does not target the default branch, its head is not a branch of the designated repository, or the project has none. | Nothing this control can do. |

Applying re-proves under the retained repository's lock, posts one marked
proof comment (`aq-delivered-pr:<number>:<head>`), re-reads the head, closes
the PR and records `integration.pr_closed_delivered` with the proof, the
operator and the reason. It never changes a task, completion, receipt or
branch; tasks whose `pr_url` names the PR are listed in `task_ids`. Design:
[legacy open PR recovery](../superpowers/specs/2026-10-01-legacy-open-pr-recovery-design.md).

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
* **A candidate's skip stalled.** A completed task the sweep skips (an
  undelivered dependency, a missing source ref, a completion without retained
  git provenance — `missing_provenance`, fixed with `aq integration
  migrate-provenance <project> --apply` — a cycle, a parked source with no live
  repair) is counted per identical evaluation. At
  `integration.publisher_stall_after` (default 5) the attempt ends as
  `candidate_stalled`: doctor reports ERROR and `supervisor-<project>` gets one
  message with the task, repository, target and source OIDs, completion,
  reason and a recovery command. Below the bound the check warns from the third
  evaluation (`candidate_skipped`). A skip waiting on a live repair does not
  count. The stalled attempt is not reported again, even after a restart; the
  sweep still checks it and clears the record the moment git shows the work
  delivered. Fix the named cause, then `aq integration sweep <project>
  --recover-child <task>` starts a fresh attempt; so does a new completion, a
  moved source or a changed target. The skip record, in the task's
  `development_publisher_skip` metadata, is observation only — never delivery
  proof.

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
(`aq/development/<project>/<head>`), a branch per repair and, before
publication stopped assembling parents, parent assemblies
(`aq/development/parent/…`). Once a batch is confirmed
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
run or has a live session, COMPLETED development work git does not prove on
the target (not delivered yet, or unknown: a completion without retained git
provenance, another project's repository, a git failure), an unsettled batch
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
  stays parked for a human rather than starting an unbounded chain, and the
  batch records the `repair_generation_exhausted` diagnostic.

A merge-conflict repair is asked to merge each parked source revision by its
exact SHA, so the source stays an ancestor of the repair and its delivery
lands the source itself. A repair that cherry-picked or rewrote the parked
changes is still accepted. Its source manifest identifies the exact revisions
it is responsible for. A passing close
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

The same refusal covers the writer of an expired or failed stage once its
`active`/`escalated` operation has moved on to a later stage. Either way the
close returns `result: verification_failed` with a `retired` record (operation,
stage, disposition, reason) and `next_step: aq session drain-ack`. A pool
worker that runs `aq session drain-ack` while still holding such a delegate is
stopped by the session reconciler on its next tick. No supervisor
`aq session kill` is needed (`session.retired_delegate_stopped`, plus a task
comment from `session-reconciler`). The stop happens only when the agent's own
drain-ack is recorded and the retirement is proven from durable state. A pending,
active or awaiting-completion stage keeps the worker waiting for its close. So
does any stage of a `human_required` operation, because `aq integration resume`
may revive it with the same writer. A terminal stage that is still the
operation's current stage also keeps it waiting, as does the verifier, parent or
candidate-resolution seat of a running operation. An `accepted` candidate repair also keeps it
waiting, because its close still completes it truthfully. The stop is the same
`_terminate_pool_session` teardown a kill reaches. The task is not closed or
marked passed. An attached branch owner keeps the stopped session's claim,
checkout and binding, so owner recovery can snapshot unpushed work to
`aq/preserved/<owner-row-id>` before it releases the branch to the successor
stage. If that push cannot happen (`origin_unreachable`), nothing is released.

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
[`src/commands/claim_commands.py`](../../src/commands/claim_commands.py),
[`src/integration/delivery_path.py`](../../src/integration/delivery_path.py),
[`src/integration/manual_delivery.py`](../../src/integration/manual_delivery.py),
[`src/integration/pr_delivery.py`](../../src/integration/pr_delivery.py),
[`src/integration/service.py`](../../src/integration/service.py),
[`src/integration/green_continuation.py`](../../src/integration/green_continuation.py),
[`src/integration/parent_completion.py`](../../src/integration/parent_completion.py),
[`src/integration/parent_intents.py`](../../src/integration/parent_intents.py).

```bash
aq test tests/test_development_integration.py tests/test_doctor_integration_checks.py tests/test_branch_discard.py tests/test_archive.py
aq test tests/test_integration_service.py
aq test tests/test_integration_repair_rollover.py tests/test_integration_promotion.py -k "parent_intent or busy_successor"
aq test tests/test_delivery_manual.py tests/test_integration_mode.py tests/test_merge_slot.py
aq test tests/test_integration_pr_delivery.py
```
