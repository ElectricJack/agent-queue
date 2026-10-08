# Hierarchical integration trains: reconciler operation

<!-- aq:optional-mode -->
> Hierarchical and strict train modes are per-project choices. The reconciler
> owns delivery in every enabled mode. See [integration modes](../concepts/integration.md)
> for the different review and verification requirements.

The autonomous legacy engine and its rollout, adoption and rollback controls
were removed on 2026-10-04. New subjects start under the reconciler. Disabling
`integration.reconciler_active` pauses visits while preserving subject ownership.
It does not select another engine. Existing subjects keep the repository and
reviewed policy artifact they were created with.

Inspect recorded state before changing configuration:

```bash
aq integration status PROJECT_ID
aq task show TASK_ID
aq doctor --check integration.subjects_overdue
aq doctor --check integration.subjects_held
```

Status projects each live subject's phase, last journal record, policy pin and
next visit. Doctor reports overdue visits and human holds. Neither advances a
subject or writes Git. Fix a held subject's named gate through the gate workflow;
a reviewed gate release requires that subject's exact version:

```bash
aq integration release-held-gate SUBJECT_ID GATE_ID --apply --expected-version VERSION --reason REASON
```

Change future project inputs through the existing project command and take
`GENERATION` from a fresh status result. A stale preview returns stale; the CLI
never retries the mutation against a newer generation.

```text
aq project set PROJECT_ID integration-repository-id REPOSITORY_ID --expected-integration-generation GENERATION --reason REASON
aq project set PROJECT_ID integration-policy POLICY_JSON --expected-integration-generation GENERATION --reason REASON
```

The designated repository must belong to this project. Repository rebinding is
refused while live subjects, owners or unresolved publication intents retain it.
Policy routes must resolve to reviewed artifacts available to the project.
Configuration changes never replace an in-flight subject's frozen artifact.

The git-first train accepts an optional `train` block in the hierarchical
integration policy:

```json
{
  "train": {
    "cadence_seconds": 300,
    "settling_cap_seconds": 1800
  }
}
```

This fragment belongs alongside the required `parent`, `root`,
`branchless_parent` and `on_failed_child` fields. Both train timing values
must be positive integers. Omitting `train` uses the timing defaults at
runtime and keeps the field absent from serialized policy snapshots, so
pre-existing snapshots still compare equal.

A root seals after a quiet period of `cadence_seconds` since the latest
admission, or at `settling_cap_seconds` since the first admission, whichever
comes first. With the defaults, roots admitted at 0 s and 10 s share a batch
at 310 s; arrivals every 200 s share a batch at the 1800 s cap. This corrects
the fidelity spec's 300 s example to use the old settling rule, latest
admission plus cadence. The status state is `settling`, with the admission
times and `seal_at` in `detail`. Admission timing survives daemon restarts
but never substitutes for the current PR gate or Git delivery proof.
Epic collection and local development targets freeze immediately.

The `IntegrationTrain.visit(target, seal_now=True)` and
`DatabaseBatches.open_batch(..., seal_now=True)` hooks bypass the timing
window for one call. They preserve all admission and publication checks;
an empty or blocked call does not arm a bypass for future work. The supervisor
can use `aq integration seal-now --project PROJECT_ID --apply` to freeze the
currently eligible members immediately. It leaves building, checks, reviews
and publication to the train. If a batch already owns the target, the command
reports `existing_batch` with that batch's identity rather than `sealed`.

For a misbehaving batch, first use `aq integration pause-batch BATCH_ID --apply`.
It preserves the frozen inputs and blocks candidate and target publication.
`aq integration resume-batch BATCH_ID --apply` restores open intent.
Pausing a paused batch or resuming an open batch refuses with `refused`.
`aq integration status PROJECT_ID` shows batch intent and member disposition.

`aq integration eject --batch BATCH_ID --task TASK_ID --reason "isolate member" --apply`
atomically aborts the old batch and freezes its remaining exact sources under
a new identity. A paused batch produces a paused replacement. The original
membership stays intact, and the removed task keeps its PR and approval,
returns to pending, and can enter a new batch at the next cadence. An ordinary
abort continues to withhold its frozen sources. Ejection refuses removal of an
undelivered prerequisite that the remaining members need.

The ejection instruction belongs to the batch and binds its project, frozen
member, operator and reason. It survives member archival and deletion. Task
metadata cannot release an aborted batch: the `integration_train_ejection`
prefix is reserved, and legacy markers are ignored. The additive migration
does not adopt those untrusted markers. Ejecting a task outside the frozen
membership returns `not_a_member`. A missing batch returns `unknown_batch` to
the local operator or global supervisor. A project supervisor gets the same
`unauthorized` refusal for foreign and missing IDs, which have no authorized
project binding. An empty batch ID returns `invalid_state`.

When a stacked source refresh replaces an open batch, the daemon writes the
same batch-owned release record with its service identity and the supersede
reason. The old batch aborts and its inputs return to pending for a new exact
candidate. An explicit pause or ordinary abort still holds its inputs; task
metadata cannot forge a supersede release.
If the refreshed member's task row is missing, appears in both active and
archived tasks, or belongs to another project, the old batch stays unchanged
and no inputs are released. The target reports `stack_member_missing`,
`stack_member_ambiguous` or `stack_member_foreign_project`, including the task
and batch IDs, and rechecks on later visits while other targets continue.
Restore an unambiguous task identity in the batch project to permit supersede,
or use the existing `abort-batch` control on the old batch; an ordinary abort
still withholds its frozen inputs.

These four supervisor controls default to preview; `--dry-run` explicitly
requests it. Applying an ejection requires a nonblank reason. Paused batches
can resume; aborted or promoted batches cannot.

The git-first path still rebuilds on target movement. It does not implement
`on_main_moved: wait` or a `max_wait_seconds` limit on candidate rebuilds;
these policy gaps are tracked separately from cadence and settling.

The project policy `hierarchical_integration_policy.prerequisite_branches` accepts
`stacked` or `wait-for-parent`. The `agent-queue` project defaults to `stacked`;
other projects default to `wait-for-parent`.

In stacked mode, a child with completed `blocks` prerequisites under the same
parent can start as soon as Git proves their checkpointed heads on their own
task branches. One prerequisite supplies the child's base directly; multiple
prerequisites are merged over the epic head. The origin records this stack
separately from its immutable filing base. Preparation preserves existing child
commits under the managed writer fence. Scheduler, pool demand, explain and
claim consume the same verified frontier rule.

A batch carries prerequisites before their dependents, or proves the absent
prerequisites already reached its target. It tests the exact combined candidate.
Before batching an idle completed dependent, changed prerequisite heads are
merged from its recorded checkpoint and recorded as a new completion. Its
remote branch must still equal that checkpoint, and the current completion
must name the same source. Post-close remote drift holds delivery with
`stack_source_changed`; an absent ref uses `stack_source_missing`, and a
missing matching completion uses `stack_source_unrecorded`. Each old
prerequisite head must be an ancestor of its replacement; rewritten history holds delivery with
`stack_prerequisite_superseded` so discarded prerequisite work cannot ride
along through the dependent. Conflicts
file an isolated repair task; the dependent stays withheld until the repair
resolves and current heads are included. Reopened, abandoned or unproven
prerequisites pause dependent delivery. A refreshed source gets a new batch;
the former frozen candidate cannot publish it. The train aborts that open batch
and releases its unchanged members to pending, so one live batch still owns the
target. Explicit batch pauses still hold.
Already-delivered dependents keep their existing completions. Batching checks
delivery before stack freshness: a later prerequisite change cannot block epic
collection or withhold work that depends on the delivered task. Stack holds
apply to undelivered sources. Workspace
preparation retains the recorded stack, its hold, pending repair and refreshed
head. A transient freshness hold clears without creating another completion.
After a dependent is reopened and closed again, refresh recognizes its matching
recorded source without replacing that completion.
Each train target refreshes only its own candidate window using the visit's
fetched Git view. Advisory stack freshness batches refs and caches them for
at most two seconds; preparation, claim and refresh use uncached ref checks.

In wait-for-parent mode, Git must prove the sibling's current work reached the
shared parent first. Preparation merges that exact parent head into the child.
Both modes preserve the filing origin and exclude unrelated default-branch
commits. Unknown or stale evidence leaves the child unclaimable.

A completed prerequisite in another epic, or a root prerequisite, must reach
the project's default branch before its dependent becomes claimable. Git proves
the exact completion source by ancestry, an `AQ-Source` trailer or whole-source
patch equivalence. `aq task explain` names an unproven prerequisite and its epic
with `prerequisite_not_on_default_branch`. Pool demand and claims use the same
proof.

An explicit `no_change` proof also releases a prerequisite: its passing close
must record an empty commit list, its retained completion head must equal its
recorded base, and Git must contain that base on the default branch. Completions
that do not meet these conditions use the exact whole-source delivery proof;
an empty commit list cannot override containment of the retained source. Missing
provenance keeps the dependent withheld. The root train uses the same proof and
does not batch a completion that added no changes.

The project integration policy defaults to
`cross_epic_prerequisites: default_branch`; an explicit `completed` value restores
the legacy rule that completion alone releases cross-epic dependencies.

Before starting a cross-epic dependent, its epic must contain the proven sources.
A refresh brings them from the default branch through a frozen train batch.
Publication needs the epic's lease,
checks on the exact candidate and attestation. Checks or an occupied lease keep
the refresh pending; a content conflict files ordinary repair work on the epic
branch and withholds children whose prerequisites are still outside that epic.
If the repair publishes a merge containing every required source, those children
can start while the refresh candidate still awaits checks and attestation.
Preparation preserves the child's existing
commits and records its actual refreshed parent head and default head in the
filing origin's `base_refresh` annotation. The original `base_sha` stays fixed.
Preparation requires the epic to contain each proven cross-epic source; unrelated
default-branch commits arriving during CI do not force another refresh. It refreshes
before constructing a sibling prerequisite stack, so both inputs reach the child.
Status includes each epic's `ahead` and `behind` commit counts against the default
branch, or an unknown distance when Git cannot observe it.

Supervisors can preview and start a refresh to admit dependents whose epic lacks
a proven source:

```bash
aq integration refresh-epic --task EPIC_ID
aq integration refresh-epic --task EPIC_ID --apply
```

An apply reports `pending` while the train checks or repairs its candidate.
Subsequent train visits finish publication; repeating apply also advances the
same frozen refresh. Apply and child preparation request bounded, serialized train
visits and respect repository rate-limit pauses. An open epic-refresh batch withholds
cross-epic dependents until it settles, even if its sources are already contained.
An ordinary sibling collection, including a failed or human-blocked batch, does
not withhold a dependent whose exact prerequisite sources are proven on the default
branch and contained in its epic. Missing epic containment withholds the child even
when no batch is open. Scheduler, pool demand, explain and claim share these rules.
Explain reports `frontier_epic_refresh_pending` for the epic gate and names any
blocking refresh batch id; default-branch proof retains its separate exclusion.
Repairs for failed or missing CI start from the tested refresh candidate; conflict
repairs start from the partial candidate containing the successful merges.

GitHub runs no `pull_request` workflows for a PR that conflicts with its base, so
a conflicting root PR would wait for checks forever. When the PR read reports it
conflicting, the root's blocker is `pr_conflicting`, with the PR URL and the
default-branch SHA; a still-computing mergeability is a short `awaiting_pr_checks`
retry. For an epic root the train's next visit starts the same attested refresh as
`refresh-epic --apply`, once per (epic head, default head) pair, and the blocker's
`refresh` names its batch. The refreshed head is held for its own review and its
own exact-head PR checks. For a leaf root nothing is started: merge the default
branch into the task branch, push it and close the task again. Push-event runs
never count as PR checks.

For an existing historical subject, `engine-transfer` and
`development-engine-transfer` offer a forward-only audited transfer to
`reconciler`. Preview first, then supply every exact subject version, the reason
and reviewed cutover evidence. There is no transfer back to the retired engine.

GitHub App readiness remains available through `aq integration app-verify PROJECT_ID`. See [App-mode setup](../config/app-mode-train.md) for
credentials and repository protection. Applying project policy does not change
GitHub configuration. Root admission and publication are train decisions;
supervisor controls adjust intent and membership without bypassing admission
or publication gates.

When GitHub rate-limits a visit, the train pauses every target of that
repository instead of failing each one on every tick. The pause lasts 60 s and
doubles with each consecutive limit up to 15 minutes, or runs until GitHub's
own retry time when that is later. In `aq integration status`, the paused
targets show `detail.reason: rate_limited` and `detail.retry_at`. When the pause
ends, a single target probes GitHub first and the rest resume once it gets
through. The daemon log carries one warning per pause; the traceback appears
only at DEBUG.

Train admission allows at most four concurrent visits per repository, including
explicit refresh requests. Additional targets remain deferred without creating
waiting tasks. Each tick admits the least recently started idle targets; unvisited
targets go ahead of previously visited targets. Other repositories have their
own allowance. A blocked target retries after 10 seconds, doubling for the same
batch, target OID and refusal up to 60 seconds. A changed refusal or target resets
the delay. `IntegrationTrain.wake` clears it. The delay is a scheduling hint:
each retry takes fresh Git observations and repeats the normal admission and
publication checks. New work on an existing blocked target may wait up to the
retry cap; a new target receives a turn within one frontier sweep once running
visits finish. Existing repository rate-limit and promotion pauses still apply.

`aq integration status PROJECT_ID` exposes `timing.selection` for each visited
target, including a visit still in flight. `stages` reports calls, item counts
and seconds for pending ID reads, routing, root proof, stack refresh, member
input reads, target proof, epic readiness, PR admission and freezing. `counts`
reports candidate scans, visit-local window reuse, repository/routed IDs, shared
fetch hits, and completion, object and delivery cache hits/misses. Missing keys
mean zero observed operations. Compute a cache hit rate as hits divided by hits
plus misses for that cache; shared fetch hits divided by hits plus fetch starts
measures fetch reuse. The completion and delivery keys retain the pinned metadata
OID, exact completion identity and exact target OID. Missing or invalid provenance
still withholds admission.

Substage durations include I/O waits, and nested substages overlap. They cannot
be summed with the exclusive `timing.stages_seconds`, or used to distinguish
subprocess work from resource contention without a separate profile. The reported
197.46-second `select_batch` observation alone does not identify a culprit.
Selection now routes before root proof and reuses its member window within a
single visit. Root proof still removes delivered work before the member limit;
historical delivery and out-of-window dependency checks remain active.

Two bounded, isolated benchmarks make the measurements reproducible:

```bash
python scripts/benchmark-train-selection.py --targets 55 --members 4
python scripts/benchmark-git-truth.py --members 137 --passes 1
```

The first uses synthetic database rows and proof latency with the actual
selection and admission mechanisms. The previous scan layout performs 110
scans and 24,200 root-proof items; the new layout performs 55 scans and 220
items. On the development run, wall times were 0.361 and 0.070 seconds. Its
repository peak was four visits; new runnable work received a turn after 13
additional rounds, while the message delivery engine completed 14 inbox passes.
These are workload-model results, not a speedup claim for the historical daemon
visit. The second uses disposable real Git objects: 137 cold delivered proofs
used 961 Git commands in 2.864 seconds; the warm pass used zero commands with
137 delivery-cache hits in 0.004 seconds. Wall times vary with machine load.

## Controls quick reference

- `aq integration seal-now --project PROJECT_ID --apply` freezes eligible root inputs immediately, bypassing cadence once.
- `aq integration pause-batch BATCH_ID --apply` pauses publication for an unpromoted Git-first batch.
- `aq integration resume-batch BATCH_ID --apply` resumes a paused Git-first batch.
- `aq integration abort-batch BATCH_ID --apply --reason REASON` aborts an unpromoted Git-first batch.
- `aq integration refresh-epic --task EPIC_ID --apply` starts or advances the attested refresh; pending checks or repairs continue through the train.
- `aq integration record-root-noop ROOT_TASK_ID` previews a no-artifact completion for an unheld no-code root; apply requires `--apply --head HEAD_SHA --reason REASON`.

The controls keyed by a task, operation, batch or reservation — `redrive-root`,
`redrive-child`, `reopen-collection`, `reserve-owner`, `release-owner` — take no `project_id`,
so authorization resolves their target project server-side. `record-noop` is an
exception: a local operator records no-code receipts (or an authorized playbook),
and *every* session is refused it, including the project's supervisor. See
[the command scope model](../specs/design/aq-surface.md#73-elevated-scopes-and-the-commands-that-carry-no-project_id).

Current recoveries reuse the existing episode, fence, receipts and stage budget.
Always preview, resolve the reported blocker, and apply the exact reviewed head
and other fences. Never edit integration tables to bypass a gate or CI proof.

```text
aq integration redrive-root TASK_ID
aq integration redrive-child CHILD_TASK_ID
aq integration reopen-collection PARENT_TASK_ID
aq integration rebind-repair --task-id REPAIR_TASK --dry-run
aq integration rebind-detached-repair OPERATION_ID
aq integration recover-preserved-repair OPERATION_ID --intent INTENT_ID --candidate SHA
aq integration resolve-candidate-member --help
aq integration recover-candidate-member RESERVATION_ID
aq integration close-delivered-pr PROJECT_ID PR_NUMBER
```

## Repair and collection recovery

A parent conflict successor retains the current intent as its trigger and
publishes with its own fence. If an older attachment names a displaced intent,
preview `rebind-repair` with the repair task id before applying its current head.

Use the full `head_sha` reported by the dry run. The command refuses a stopped
writer, expired authority, changed candidate or moved remote target. Applying
records the reservation under the current intent; the attached repair session
then pushes with its current fence and closes. If the writer has stopped,
recover its attachment through the existing repair lifecycle first.

When a parent repair stage exhausts at the open conflict's old tip, its debug
successor keeps that conflict intent as its trigger. Its writer resolves and
publishes through `integration-resolve-conflict` and `push-conflict-resolution`
under its own fence, with no rebind. Any other debug stage carries the trigger
`stage-exhausted:<operation>:<ordinal>`.

A parent debug stage can be frozen on a commit the parent branch never
received: its retained handoff bound a stopped writer's local head, so every
delegate fails admission with `slot_reset_failed` ("repair branch no longer
descends from its frozen starting commit") and its `stage-exhausted` trigger
cannot resolve the open conflict. The same operators can rebind that detached
stage to the conflict at the published head:

```bash
aq integration rebind-detached-repair OPERATION_ID
aq integration rebind-detached-repair OPERATION_ID --apply \
    --stage STAGE --remote-head PUBLISHED_SHA --reason "..."
```

Pass the `stage` and `remote_head_sha` the dry run reported. The command
refuses unless the writer is detached and unclaimed, the stage is within its
unchanged deadline and attempts, the published head is the conflict's
expected target, and the frozen head is an unpublished descendant of it. Every
receipt must also sit in the published history. Apply changes only the stage's
starting commit, trigger and subject, and records the previous values in the
dossier's `detached_rebinds`. The branch, fence, budget, intent and gates are
left as they were. The same delegate is then readied, and it resolves the
conflict through the normal fenced publication path. Design:
[detached repair rebind](../superpowers/specs/2026-10-01-detached-repair-rebind-design.md).

If a debug stage has already expired without progress, but its stopped writer
completed a resolution that `release-owner` preserved, preview that exact commit:

```bash
aq integration recover-preserved-repair OPERATION_ID --intent INTENT_ID --candidate SHA
```

Run the returned `apply_command` after inspecting its source, target, parents,
tree, released fence and remaining attempts. Apply requires `--stage`,
`--released-fence` and `--reason`; it repeats every proof and publishes with an
expected-old compare-and-swap under a fresh collector fence. It consumes only
the audited two-parent merge. The expired deadline and consumed attempts remain
unchanged, the former delegate stays blocked, and normal parent verification is
still required. An exhausted attempt budget or human gate returns a specific
blocker. An interrupted push with an unchanged target is ambiguous and is never
blindly retried. See [preserved repair recovery](../superpowers/specs/2026-10-01-preserved-repair-recovery-design.md).

### No-code child receipts

A reviewer filed under an active parent is itself a child in the collection
episode. Its `pass --work-outcome no-op` close records the review verdict, but
the parent still needs a disposition receipt for the reviewer's own branch.
When parent readiness reports `receipt_missing` for that child, a local
operator records the exact no-code disposition:

```bash
aq --json task show CHILD_TASK_ID | jq -r '.data.integration_delivery.checkpoint_sha'
aq integration record-noop CHILD_TASK_ID --expected-head-sha CHECKPOINT_SHA
```

The command requires the current passing `no-op` completion, and for a reviewer
it requires an approved review evidence row. It checks that the child head is
still its reserved base, resolves that commit's tree from Git, and writes a
receipt for the current parent episode. Repeating the command returns the same
receipt; a new no-op completion gets a new receipt revision. A playbook may
invoke the contracted `integration_record_noop` command when its policy grants
that exact capability. No session can invoke it: worker and supervisor
principals are both refused, so a no-code child always needs a local operator
(and a supervisor asked to record one reports the child id and its checkpoint
head instead).

### A parent whose last child was deleted

Nothing to run: the delete and the parent runtime finish it. Deleting a
container's last child takes back the parent's claim if a stopped worker still
held one, and leaves the parent PAUSED with no agent and its collection episode
intact. The parent runtime then projects readiness on the aggregate that is
actually left — with no child receipt there is nothing collected on top of it,
so the head to verify is the parent's own pre-collection commit, published on an
`aq/parent` ref — files its writer, and completes the parent on trusted green CI
([work-graph §13a](../specs/design/work-graph.md)). A parent already in that
shape when the fix deploys is repaired by the container sweep on the next tick
or on daemon start; `reopen-collection` and `recover-parent-head` are no longer
needed and both refuse an IN_PROGRESS parent anyway.

### Stopped pool-writer handoff recovery

Preview `aq integration release-owner --task-id TASK_ID --dry-run`. The existing
owner recovery probes the provider, verifies the current fence, preserves
unpublished work and releases only a proved stopped attachment. It refuses a
live writer, reused checkout or changed fence. Repair handoff and retry remain
Subject decisions under the frozen policy and unchanged stage budget.

`aq task restart TASK_ID` is the supported restart for a stopped repair
delegate: it redispatches the delegate's current stage through the same fenced
handoff, so the delegate returns to the pool claim frontier holding its exact
reserved repair fence and resumes from any preserved repair commits. It is
refused — the task left exactly as it was — while the branch fence is still
held by the stopped writer; free that with `release-owner` first.

To disable future admission, use the project configuration CAS:

```bash
aq project set PROJECT_ID integration-mode disabled --expected-integration-generation GENERATION --reason REASON
```

Existing Subjects retain their identity, pins and scheduling. Pausing all visits
uses `integration.reconciler_active: false`; re-enabling resumes those same
Subjects. There is no engine rollback or automatic drain that restores old policy.

## No-code root completions

A materialized root branch still needs completion provenance when its work
produced no code. A local operator or live supervisor of the owning project can
complete an unheld root, or repair a COMPLETED root whose administrative status
change omitted its completion record:

```bash
aq integration record-root-noop ROOT_TASK_ID
aq integration record-root-noop ROOT_TASK_ID --apply --head PREVIEW_HEAD_SHA --reason "No code produced"
```

The preview proves that the published head descends from the recorded source
origin and has the same tree. Apply checks the exact head again, retains an
immutable `artifact:false` Git completion and records a passing `no-op`
completion with COMPLETED status. It preserves the branch and origin. Existing
passing no-op completions retain their generation; repeated apply is idempotent.
Claims, live writers, branch ownership, containers, required deliverables and
open batches must be resolved first. Changed or unavailable Git evidence refuses.
Use normal `task close` for held work; `task set-status ... COMPLETED` refuses a
new completion of a branched train root.

A source with unknown provenance stays withheld together with its dependent
work. Its task blocker remains visible while unrelated sources can seal and an
already sealed healthy batch can pass checks and publish on the same target.

## Source CI: red versus infrastructure

The root PR admission gate automatically reopens a completed leaf when a
required check genuinely fails on its exact head. Reopen feedback includes
the failing jobs, links and reported test names. The same head can reopen once;
`root.repair.primary_attempts` bounds changed-head attempts (default three).
An unchanged failing head, exhausted recovery or a container root instead
notifies the supervisor with a named blocker. Existing source-CI repairs keep
their paired admission path. See [root PR check recovery](../specs/design/root-pr-check-recovery.md).

Source CI observation of a train root's exact PR head files a repair only when
at least one required check genuinely failed (`failure`, `timed_out`,
`action_required`). A run whose non-success required checks are *all* cancelled
is infrastructure — typically a GitHub Actions outage, annotated "The job was
not acquired by Runner of type" — and it is never handed to an agent: the only
code action available would be merging the same head again and hitting the same
outage, which is how a single outage becomes a chain of repairs of repairs.

Such an observation waits, and asks GitHub to re-run the checks for that exact
head: one re-request per cancelled check suite id GitHub reported for it
(`POST /repos/{owner}/{repo}/check-suites/{check_suite_id}/rerequest`,
[bounded](https://docs.github.com/en/rest/checks/suites#rerequest-a-check-suite)
by `root.repair.source_ci_infra_backoff_seconds` to
`..._backoff_max_seconds`). GitHub documents writes to checks as GitHub-App-only,
so an existing-login credential issues no request at all and the observation only
waits; widening a credential is a human decision, and a human can also re-run the
workflow by hand. After `root.repair.source_ci_infra_attempts` consecutive
infrastructure-only observations it stops asking and names
`source_ci_infrastructure` on the source-CI row instead of looping. Any
observation that is not infrastructure-only resets the counter, so a head that
recovers costs nothing later. A cancelled check is never counted as success, and
a red run stays red however many of its other checks were cancelled.

The root-batch path already treated cancellation-only as infrastructure and
waited (`observe-ci` outcome `infra`); this is the same rule applied to the
source-CI observation path so the two agree.

## Delivery evidence

`task show` and the dashboard report delivery from receipts and observed Git
history. A worker close, queued request or historical journal entry alone does
not prove that the task's exact revision reached the default branch. Pending
root requests are revisited from durable state after restart; event delivery is
a wake-up hint, not the authority to seal or publish.

Parent readiness still requires the existing child dispositions and trusted
verification receipts. Reconciler decisions use those facts through the shared
ports. Historical development operation rows remain audit and recovery inputs;
they do not restart an autonomous publisher.
