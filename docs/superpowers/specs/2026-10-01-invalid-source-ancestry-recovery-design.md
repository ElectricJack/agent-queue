# Invalid train source ancestry and stranded construction

Status: implementation, task `bright-nexus-97`, 2026-10-01. Follows
[continuous delivery](2026-09-30-continuous-delivery-design.md) and
[later repair stages](2026-10-01-later-repair-stage-constraints-design.md).

## Incident

Batch `integration-batch-e694a6059b8a865bc8ace9704f3b2263` (sweep 93) froze two
members on recorded base `f736edeb`: `fresh-flare-12` at `82ae322c` and
`keen-beacon-16` at `43aa77ed`. The worker for `fresh-flare-12` stacked its branch
on `5b83a173`, a line that does not contain `f736edeb` (merge-base `82b19024`).
The reviewed tree was exact; the recorded base was not an ancestor of the head.

1. **Admission.** The task-authorization snapshot
   (`ReviewEvidenceProducer._snapshot_pull_request`, driven by
   `GitHubReviewPoller`) proved the exact remote head and tree, and the ancestry of
   repaired CI sources, but never that the recorded source base is an ancestor of
   the reviewed head. It recorded approved evidence and the train sealed the source.
2. **Construction.** `CandidateService.build` reserved revision 0 (`building`),
   activated repair stage 0 (`RepairService.start`), then
   `_member_identity_matches` refused member 0 and returned `source_moved` without
   recording anything.
3. **Policy.** The reviewed `root-train` graph routes `source_moved` and
   `base_moved` from `construct-and-test--build` to its `failed` terminal. Nothing
   re-emits `integration.sealed`, so the batch stayed `building`, stage 0 stayed
   `active` with no `repair_task_id`, `stale_schedule` classified the request
   `active`, and `IntegrationScheduler._maintain_batch_lease_on` renewed the lease
   indefinitely. At the stage-0 deadline the ladder would have dispatched a debug
   writer that cannot change a frozen manifest.

The supervisor recovered with `integration cancel-preserving` on the batch
operation and `reopen-with-feedback` on `fresh-flare-12`; catch-up 94 sealed a new
batch with the valid source. Nothing was written to the database by hand.

## Decision

The policy graph is unchanged: `source_moved` and `base_moved` remain typed
non-success outcomes. The mechanism guarantees that neither leaves a live batch
without a runnable continuation, and the earliest Git-aware step refuses the
identity.

### 1. Admission refuses invalid ancestry

Both review paths that write approved evidence for a train root
(`_snapshot_pull_request` for GitHub and task-authorization verdicts, and
`snapshot` for a reviewer task on a train root) prove
`merge-base --is-ancestor <recorded base> <reviewed head>` in the retained
repository after the exact remote head is proven. A false or unknown answer
raises `SourceAncestryInvalid` (a `HierarchyError`, code `invalid_ancestry`)
carrying the exact identity, the merge-base and the reason. No approval is
written. A rejection is never refused.

### 2. One exact, auditable withdrawal

`ReviewEvidenceProducer.record_ancestry_rejection_on` appends rejected review
evidence for the exact identity (task, repository, base, head, generation):
reviewer identity `integration:source-ancestry`, decision path
`source_ancestry_invalid`, the proof, and actionable feedback. Its id is
deterministic, so replay is idempotent, and its `created_at` is after the
identity's latest evidence, so `latest_exact_reviews_on` and `_authorization_on`
exclude the identity from every later seal and authorization. A repaired head is
a new identity and is admitted normally. Nothing fabricates an approval; dependents
are not relabelled.

### 3. Actionable repair through the command layer

`CommandHandler._cmd_repair_integration_source_ancestry` is a daemon-only adapter
for a server-observed `SourceAncestryObservation` (never a tool input). Under the
source's advisory lock and the project lock it re-reads the exact current source
identity (a changed one is `stale`), records the rejection, and then:

- with `root.repair.source_ci` (the operator's continuous-delivery authorization
  for routine source repair) and a `source_base_not_ancestor` proof it reopens the
  source through `_cmd_reopen_with_feedback`: same task, same branch, same
  recorded origin. The feedback names the base, head, merge-base, branch and
  evidence id and asks the worker to make the recorded base an ancestor without
  discarding reviewed history (merge it; never rebase or force-push), re-run
  checks, publish and close. The identity is re-read just before the reopen;
- it reopens an identity at most once. If the same head comes back (the
  feedback's evidence id is already in the task's `reopen_feedback` context), or
  the proof is a tree mismatch no merge can fix, or the project lacks the
  authorization, it sends one stable-id message to `supervisor-<project>` with
  the exact identity, the reason and the `aq task reopen-with-feedback` command.

`GitHubReviewPoller` calls the adapter when a snapshot raises
`SourceAncestryInvalid`, and on every pass for a completed root whose current
exact identity's latest evidence is an ancestry rejection. The second path makes
a construction-time withdrawal (below) and a crash between withdrawal and reopen
self-healing.

### 4. Construction withdraws, never strands

After fetching exact inputs (including observed main, the construction base)
and before construction applies a member, `build` proves every member's identity. Two failures are Git-proven and permanent for the
frozen manifest: `source_base_not_ancestor` and `reviewed_tree_mismatch`. If any
member has one, `_withdraw_invalid_sources` runs one transaction under the project
lock that:

- revalidates candidate authority (revision, lease fence, operation, collector);
- refuses (see below) while a writer is attached or live, or a ref mutation,
  resolution, attestation or promotion intent is unresolved;
- records the ancestry rejection for each invalid member;
- cancels the operation and its unfinished stages, settles reserved delegates
  (`release_delegates_on`) and releases detached reserved branch owners, exactly as
  `cancel_preserving` does;
- ends the batch `aborted` with the machine reason, keeping members, revisions,
  refs and evidence intact (the frozen manifest is never edited);
- records the batch's own trigger as the schedule's catch-up when none is pending;
- logs `integration.batch_source_withdrawn`.

`release_ended_batch_request` then frees the lease and turns the catch-up into
the next request, so the valid members are resealed at once on observed main. The
build returns the existing `source_moved` outcome with the first invalid
`member_ordinal`.

When a blocker refuses withdrawal (a delegate that is assigned, in progress or
attached counts as a writer), or `_construct` reports `source_moved` for a
missing object or an inconclusive probe, nothing is ended; the build schedules a
continuation (§5). A `repairing` batch is driven by its repair ladder instead.

### 5. Durable construction continuation

Whenever `build` or `rebuild` ends `base_moved`, or `source_moved` without a
withdrawal, `_schedule_construction_retry` enqueues `integration.sealed` for the
batch's operation route, available after `CONSTRUCTION_RETRY_SECONDS` (60 s).
At most one undelivered retry exists per batch revision; a new one is written
only after the previous was accepted, at most one per 60 s window. It is written only while the
batch is still `sealed` or `building` at that revision and its operation is
active. A replayed build is idempotent, so the retry resumes from durable state.

### 6. The stage-0 deadline re-drives unfinished construction once

A batch whose stage 0 reaches its deadline while the batch is still `building`,
with no writer ever assigned, has nothing to repair. `RepairService.expire`
enqueues one `integration.sealed` re-drive per operation, stage and revision and
waits. Once `CONSTRUCTION_REDRIVE_GRACE_SECONDS` (600 s) has passed since that
re-drive was delivered, or since it was enqueued if no route ever accepted it,
the existing ladder escalates as before. Attempts, deadlines and budgets are
untouched. This recovers batches already stranded by the old code.

## Not changed

- No migration, contract field or reviewed playbook bundle changes.
- Members, revisions and evidence of an aborted batch remain as audit history.
- Eject stays an operator control; withdrawal ends the batch instead of editing it.
- `wait` and `runtime_error` outcomes keep their current behaviour.

## Verification

- Real Git graph: member 0 stacked on a side line that drops the recorded base,
  exact tree; member 1 valid. Build returns `source_moved`, the batch is aborted,
  the stage and operation cancelled, the lease freed, a catch-up request enqueued,
  and the rejection excludes member 0. The next seal holds only member 1 and
  builds. The withdrawn source is reopened with feedback, and after merging its
  recorded base it is admitted and seals again.
- The reviewed `root-train` artifact still routes `source_moved`/`base_moved` to
  `failed`; the durable state above is the continuation.
- The stranded shape from the incident (revision 0 `constructing`, stage 0 active,
  no delegate, failed run): the deadline re-drives construction and the build
  withdraws.
- `base_moved` keeps exactly one pending retry; ended batches schedule none.
- Admission: the authorization, GitHub and reviewer-task approvals refuse the
  identity; the poller reopens under `source_ci` and messages the supervisor
  otherwise; replay is idempotent, and a close that brings the same head back is
  routed to the supervisor instead of a second reopen.
- A live writer defers withdrawal and keeps the retry; an undelivered re-drive
  still escalates after its grace.
