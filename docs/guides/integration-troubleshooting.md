# Integration troubleshooting

The Git-first train owns enabled root, epic and development delivery. Inspect
current target visits, batch intent, exact candidate/check evidence and blockers:

```bash
aq integration status PROJECT_ID
aq task show TASK_ID
aq doctor --check integration.delivery_engine
aq doctor --check integration.train_batches_stalled
```

These are read-only projections. `integration.git_first: active` constructs the
train; the code default `shadow` starts no delivery engine. After project train
policy installation, a disabled activation preserves sources and candidates
while stopping admission and fenced writes. Check that binding before treating
a paused lane as a broken publisher. See the [train runbook](git-first-train-runbook.md).

Historical Subject rows retain their gates and ownership evidence. A restart
or configuration edit never grants authority to bypass a hold. Status can read
Git to verify delivery but never advances a target.

The retained Subject diagnostics still inspect unfinished rows in
`integration_subjects`:

```bash
aq doctor --check integration.subjects_overdue
aq doctor --check integration.subjects_held
```

`integration.subjects_overdue` reports rows whose `next_due_at` is more than
60 seconds overdue; `integration.subjects_held` reports rows with no
`next_due_at`. Inspect their `wait_reason` and `gate_id` for the recorded blocker.
These checks expose historical Subject state; use the train diagnostics above
for current delivery progress. For a stranded branch owner, preview the separate
ownership proof with `aq integration release-owner --task-id TASK_ID --dry-run`
as described below.

Inspect App credentials and branch protection with `aq integration app-verify
PROJECT_ID`. `app-setup` prints the fixes; applying GitHub settings is a separate
repository administrator action. See [App-mode setup](../config/app-mode-train.md).

## A passed task blocked at delivery

Passing close records execution, while delivery requires the exact revision to
be present on its target. Check `task show` for its checkpoint, current Git delivery
 evidence and blockers. Historical receipts alone do not prove current delivery.
Resolve missing source evidence or CI under the reviewed policy. For a missing
no-code receipt, a local operator or live elevated named supervisor can use
`aq integration record-noop`; ordinary workers cannot invoke that control.

`obsolete_child` means the child's diff only edits paths the parent has removed.
Review the reported source and parent heads and the parent commit removing those
paths. A local operator or project/global supervisor can run
`aq integration redrive-child CHILD_ID --dispose-obsolete --superseded-by SHA
--apply --head SOURCE --parent-head TARGET --reason "Why the work is obsolete"`.
Omit `--apply` to preview. The fenced ours merge preserves the parent's files,
records the reason and actor, and makes the child a real ancestor. Active writers,
batches, changed heads or missing supersession proof refuse it. No human gate is
required. `record-noop` continues to require a code-free completion.

A completed child missing from a parent's collection can be inspected through
`aq integration redrive-child CHILD_ID`. A completed root missing its PR can be
inspected through `aq integration redrive-root ROOT_ID`. Preview first; apply
uses the exact head and reason and repeats the proof. Changed remote ancestry,
source identity, live writers or human holds are blockers, never retry overrides.

For an older completed code leaf reporting `missing_git_provenance`, a local
operator or live project supervisor can preview source recovery:

```bash
aq integration redrive-child CHILD_ID --adopt-source
aq integration redrive-child CHILD_ID --adopt-source --apply --head FULL_PREVIEWED_OID --reason "Recover missing completion source"
```

This retains the existing passing generation after proving its published branch
matches its checkpoint and materialized parent origin. A conflicting recorded
source, missing passing completion or live writer refuses recovery. Adoption
lets the train evaluate the source; its usual review, check and containment
requirements still apply.

## Human holds and repair progress

A human-held parent Subject reports its gate id and current version. After
reviewing the gate answer, release that exact hold:

```bash
aq integration release-held-gate SUBJECT_ID GATE_ID --apply --expected-version VERSION --reason REASON
```

Current batch repairs use one task-free fixer session and retained checkout.
Inspect the batch's fixer state, failure evidence, current head and finite revisit
rather than looking for a new repair task. Distinct red results reuse the same
session; exhaustion reports once to the supervisor and preserves work. A fixer
publishes through `aq integration batch-fix --action push` and changes its clean
binding only with `--action rebind`. Exact green can still deliver after exhaustion.
Existing ordinary repair tasks retain their frozen input, ancestry checks and
close handoff. The retired `recover-preserved-repair`, `rebind-repair` and
`rebind-detached-repair` controls are unavailable; use the retained
[repair recovery procedures](hierarchical-integration-trains.md#repair-and-collection-recovery)
for the named current blocker.

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
aq integration release-owner --task-id repair-repair-batch-…-1 --apply
# => same shape, dry_run: false, one row now in integration_owner_recoveries
```

You can also target a specific owner row directly instead of a task:

```bash
aq integration release-owner --owner-row-id o-a3f7… --apply
```

`release-owner` previews by default; `--apply` explicitly preserves work and releases
the stopped attachment. `--dry-run` and `--apply` are mutually exclusive.
Preservation first proves the local task ref, recorded task origin, claim time
and matching slot sentinel. An unrelated detached slot is skipped; a missing
local ref appears as `skip_preserve` with reason `missing_local_ref`. Unpublished
work without provenance returns `unproven_task_work`, leaving it in place.
Detached checkouts skipped for a missing task ref or mismatched sentinel remain
intact and their released slots are disabled, including clean checkouts, until
their unknown commits can be reviewed. A slot whose sentinel names another task is
never a push source, even when its detached HEAD holds commits origin lacks.

`--no-push` releases without publishing anything, combined with either the preview or
`--apply`. Nothing is proved, snapshotted or pushed, so unpublished work does not
refuse the release with `unproven_task_work`. Each unpublished source appears in
`evidence.unpublished` and as a `skip_preserve` step with reason `no_push`; it stays in
its checkout or local branch ref. The row's own slot is retained and disabled, and the
release comment names what was left behind. Use it when the work belongs elsewhere or
will be preserved by hand:

```bash
aq integration release-owner --task-id TASK_ID --no-push            # preview
aq integration release-owner --task-id TASK_ID --no-push --apply
```
It takes no `--project-id`: the task (or the owner row's repository) names the
project, and the caller's token has to name the same one. A project's supervisor session may
therefore run this recovery for its own work and is refused for another project's — see
[aq-surface §7.3](../specs/design/aq-surface.md#73-elevated-scopes-and-the-commands-that-carry-no-project_id).

If a task branch was contaminated by an earlier recovery, the local operator or
live project supervisor can restore its recorded origin after releasing its
writer. Preview supplies the origin id, current remote SHA and released fence:

```bash
aq integration restore-task-origin --task-id TASK_ID --json
aq integration restore-task-origin --task-id TASK_ID --apply \
  --origin-id ORIGIN_ID --expected-remote-oid BEFORE_SHA \
  --expected-fence-token FENCE --reason "Remove unrelated slot history"
```

Apply retains the displaced head at the preview's `backup_ref`, then uses an
exact remote lease to publish only the recorded origin (the preview's `after_sha`). A changed origin,
fence, remote head or active writer refuses the operation. The audit events
`integration.task_origin_restore_started` and `integration.task_origin_restored`
record the principal and before/after SHAs. This control never resets a checkout.
It refuses checked-out branches and other local work; an existing local ref
matching the displaced head is repaired with an exact compare-and-swap.

**Refusal reasons and how to resolve them.**

Every refusal is recorded in `integration_owner_recoveries` with the row's
`evidence` field populated — the `evidence.detail` string has the same wording
as the recovery result. Dry runs do not write an audit row.

| Refusal | Meaning | Fix |
|---|---|---|
| `writer_live` | The provider probe says the writer is still running, or a newer live session names the task. | Wait for the writer to exit, or confirm the task is truly cancelled/archived, then retry. A drain-acked pool worker holding a retired repair delegate, or still bound to a task whose close already committed, is stopped automatically (see the repair-delegate section and the paragraph below this table). |
| `checkout_in_use` | A live session holds the worktree the branch is checked out in. | Close that session or hand it off, then retry. |
| `unproven_task_work` | Unpublished work lacks the recorded origin, claim time, matching slot sentinel or task-ref ancestry. | Inspect the preview evidence and preserve the work under its actual task. Do not substitute the slot HEAD for a missing task ref. To release without publishing, rerun with `--no-push`. |
| `origin_unreachable` | `git fetch` or the preservation push failed (network, auth, rate limit). | Fix connectivity or auth, then retry. |
| `stale_fence` | The row changed between the check and the release — a new writer attached, or the fence was bumped by another recovery attempt. | Re-run; the new writer will be the one the check evaluates. |
| `not_found` | The owner row id is wrong, or the row was already released by the time the command ran. | Preview the current task with `aq integration release-owner --task-id TASK_ID --dry-run`. |
| `not_recoverable_state` | The row's `owner_role` is `collector`, or its `handoff_state` is `released`. Only `worker`/`repair` rows in `attached` or `handoff_pending` are recoverable. | Do nothing — the row is in a correct state. |

**A live worker blocked on its own failed preparation.** A retry automatically
rechecks a self-owned pending handoff using the exact unactivated pool claim and
clean, published checkout proof. If it remains fenced, the operator or project
supervisor can preview and restore that reservation without killing the session:

```bash
aq integration release-owner --task-id TASK_ID --self-owned --dry-run
aq integration release-owner --task-id TASK_ID --self-owned --apply
```

This requires a retained failure record for the same session instance, claim epoch
and workspace generation. It retains the claim and workspace locks for retry, even
if repeated failures requested drain and disabled the workspace. It refuses active
claims, incomplete child cleanup, changed bindings, dirty or unpublished work.
An apply writes `integration.preparation_handoff_restored` and a task comment.
Without `--self-owned`, the ordinary stopped-writer proof still applies.

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

**Historical stopped task holders.** If binding audit reports “no unique current
session proves the retained holder”, or a READY task still names a dead agent
and restart reports “saved continuation branch is unavailable”, a local operator
or live global supervisor can preview the exact historical binding:

```bash
aq project recover-retained-holder --project-id <project> --task-id <task> \
  --session-id <historical-session> --workspace-id <workspace> \
  --reason "Historical failed-publication holder"
```

Inspect the returned snapshot, Git identities, commit inventory and `proof`.
Repeat with `--apply --expected-proof <proof>` and the same identities and reason.
Changed evidence requires a fresh preview. The workspace argument is optional;
the command also discovers task-locked, continuation-stage and historical-session
workspaces. Missing historical identity, live/unknown provider or process state,
foreign bindings, active jobs and modern Git authority/delegation refuse repair.
Those owners retain their existing recovery protocol.

Apply preserves HEAD, index and unstaged/untracked files under immutable local
refs before atomically releasing exact legacy owner fences, terminal job pins,
task waits and continuation bindings, and the stale assignment. The audit event
`project.retained_holder_recovered` records the original rows and checkpoint refs.
Task status, retries and recovery holds stay unchanged. Source workspaces remain
disabled with an `operator:` reason, with their files and claim markers intact.
Inspect/salvage the recorded refs before re-enabling a source or restarting work;
this command does not publish commits or restore checkpoints automatically.

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

## A promotion flow is refused or misconfigured

`aq project set PROJECT_ID promotion-flow FLOW.yaml
--expected-integration-generation N` activates a flow only when every layer
passes: the schema and chain rules, the trust manifest's attestations and check
sets, and the targets themselves. Before it writes, it creates any missing target
with a create-only push from its source's tip. A refusal names the code and the
JSON pointer into the file, and never stores the flow. The one thing a refusal
can leave behind is a target branch it already created at its source's tip;
the result lists it under `created_targets`, and a retry reuses it. Check a file
first with `aq promote validate --project PROJECT_ID --file FLOW.yaml`.

A step with an open promotion keeps its `id`, `source`, `target`, `type`,
`gate`, `versioning` and `notes` until that promotion closes
(`promotion_flow_in_use`). Its `after` hooks, `gate.request_ttl` and new steps
appended at the end can still change. A flow that names a new target while live
integration work holds it is refused with `repository_busy`. An open train batch also
holds the repository: changing the integration repository or deleting the
project is refused until it finishes.

The daemon re-validates every stored flow when it starts. When a stored flow no
longer validates, for example after a default-branch rename or a trust-manifest
change, `aq integration status` marks its targets `misconfigured`. Schema and
chain problems are re-checked on every status read, so renaming the default
back clears them at once; a trust-manifest problem clears when the flow is
activated again or the daemon restarts. The train keeps promoting to the
default branch, and nothing is rewritten:

```bash
aq doctor --check integration.promotion_flow
```

## A promotion intent stays sealed

The train admits a promotion only while its daemon-owned task is `IN_PROGRESS`
and unrouted. If something else rewrites that row, every train visit holds the
intent with `promotion_intent_invalid`, and the daemon log names the defect
once per hold. For example, a restart might reset the task to `READY` and the
router might hand it a worker profile. The doctor check lists every open intent
in that state together with its repair command. The repair writes a JSON backup
of the task row before it restores the row:

```bash
aq doctor --check integration.promotion_stranded
aq promote repair REQUEST_ID --project PROJECT_ID
```

## Source and related pages

[Reconciler operation](hierarchical-integration-trains.md) describes current
project configuration, exact-head recoveries and receipt requirements.
[Worker pools](worker-pools.md) describes claims and session handoff.
The shared stopped-writer implementation is `src/integration/owner_recovery.py`;
its durable audit table is retained for historical and current recoveries.
