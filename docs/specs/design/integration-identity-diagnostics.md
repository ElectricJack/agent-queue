# Integration identity diagnostics

`integration.reused_task_identity` is a report-only doctor check for tasks
created before task naming reserved identifiers retained by integration history.
A deleted task can leave an origin and checkpoint that a later task with the
same ID accidentally inherits.

The check reports every unretired branch origin whose `created_at` is strictly
earlier than the matching live task's `created_at`. It includes completed tasks
and runs regardless of integration mode. Retired origins and origins without a
live task are outside this check. Equal timestamps are not findings.

Each finding includes the task ID, project, status, creation time and current
repository/branch; the origin ID, repository, branch, base, materialization state
and creation time; and any checkpoint's repository, branch and SHA. The result
contains the total origin and task counts and at most 50 findings, ordered by
project, task and origin ID. Findings produce `warn`; an unavailable database
produces `info`, and no findings produces `ok`.

The timestamp comparison is evidence of a suspected identity collision, not
authorization to rebind. Imports or clock corrections can also affect timestamps.
Owner-row creation time is not evidence: an ownership transfer preserves it even
when the owner ID changes. The check performs no Git operations and has no fix.

Rebinding remains an operator decision. Releasing a fence alone does not replace
an origin or checkpoint. A repair must prove the exact predecessor branch was
delivered or explicitly discarded, exclude live writers and dependent integration
history, preserve the old origin for audit, and account for an existing ref before
reserving the current task's fresh base. This check changes neither deletion
semantics nor claim admission.

## Rebinding a reused identity

`aq integration rebind-reused-identity --task-id TASK`
(`integration_rebind_reused_identity`, `src/integration/identity_rebind.py`)
is the guarded repair. It is an operator integration control: the loopback
operator or a live named supervisor of the task's project may run it. It is a
dry run unless `--apply` is given.

**Scope.** The *inherited origins* are the ones the doctor check reports for
the task: unretired, and created strictly before the task. With none, the
answer is `nothing_to_rebind`. The *predecessor checkpoint* is the task's
`task_integration_checkpoints` row. It counts only if it was last written
before the task existed and binds the same repository and branch as the
inherited origin.

**Refusals.** Each of these is listed with a `cause` and blocks `--apply`:

- `live_writer`: the task is ASSIGNED or IN_PROGRESS, has a live session, or
  holds a workspace lock.
- `owner_held`: the origin's ref has an `integration_branch_owners` row that
  is not `released`. `aq integration release-owner` or `release-stale-owners`
  settle that first.
- `dependent_history`: integration history hangs off the identity. That means
  a live child origin naming the task as parent, a parent episode, a delivery
  receipt, a batch membership, a promotion or root intent, a repair
  operation, a child disposition, or review evidence. A checkpoint that names
  an episode, verification or completed operation also counts.
- `checkpoint_rewritten`: the checkpoint changed after the task was created,
  so its content cannot be attributed to the predecessor.
- `checkpoint_mismatch`: the checkpoint binds a different repository or branch
  from the origin.
- `hierarchical_project`: the task's project, or the project that owns an
  inherited origin's repository (a deleted task of another project can leave
  the origin), is in `hierarchy` or `train` mode. There an unretired origin is the task's live delivery identity: train
  candidacy reads its `base_sha`, and claim admission needs a materialized
  one. Retiring it would change delivery, not tidy an audit record. The
  control therefore never reserves a fresh origin. In every other mode no
  origin is needed. A task that later needs one gets it from the ordinary
  reservation paths, which materialize a ref only when it is absent or
  already at the pinned base.
- `branch_unrecorded`: a legacy origin with a NULL `branch_name`, whose exact
  ref cannot be recovered.

**Proof.** After one fetch of the origin's repository (the development
store), the control checks each predecessor SHA against the default-branch
tip. The SHAs are the origin's `base_sha`, the predecessor checkpoint's
`checkpoint_sha` and `verified_sha`, and the current tip of the origin's ref.
Each one is `on_default_branch` (an ancestor of the tip), `not_on_default_branch`
or `unavailable` (the commit is not in the fetched store). An absent ref is
recorded as `absent`. `release-stale-owners` holds the same standard: a
deleted owner's ref that is gone proves nothing is left to deliver from it.
Every SHA that is not on the default branch is unproven. The operator settles
it only by naming that exact SHA with `--discard-tip SHA`, which records an
explicit discard. A named SHA that is not one of the unproven ones is
`invalid`. No SHA is ever accepted without proof or an exact discard.

The proof has a limit, which the control states rather than hides. A commit
the predecessor pushed beyond its recorded checkpoint, and that later left
the ref (the branch was deleted or force-pushed), is not observable on
origin. Neither the control nor the operator can prove or discard it.

**Apply.** `--apply` needs `--reason` and, via `--origin-id`, every inherited
origin the dry run reported, as a compare-and-swap on the set. The apply runs
in one transaction under the project hierarchy lock. It re-reads the task,
origins, checkpoint and owner rows and re-checks every refusal. Anything that
moved since the proof answers `changed`. The apply then:

1. sets `retired_at` on each inherited origin. The row itself is kept, and
   the immutability trigger permits only this column and the discard
   bookkeeping to change;
2. deletes the predecessor checkpoint, compare-and-swap on its `version`,
   after copying it verbatim into the audit event;
3. writes one `integration.task_identity_rebound` event on the task. It
   carries the task, origin, checkpoint and owner snapshots, every proof, the
   discarded SHAs, the default-branch tip, the reason and the principal.

It changes no Git ref, no owner row, no `task.deleted` event and no
`integration_owner_recoveries` row. That is the deletion evidence, and it
stays. A discarded tip stays wherever it is on origin. After a rebind the
doctor check no longer reports the task, and a later hierarchy close no
longer mistakes the task for a producer of the predecessor's branch.

Outcomes: `would_rebind`, `rebound` and `nothing_to_rebind` succeed;
`unproven` (any refusal or unproven SHA, each also listed in `unproven`),
`blocked` (origin could not be inspected, or the development publisher holds
the repository), `changed`, `invalid` and `not_found` fail.
