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
