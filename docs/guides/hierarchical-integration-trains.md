# Hierarchical integration trains: operation and recovery

<!-- aq:optional-mode -->
> Hierarchical and strict train modes are per-project choices. The Git-first
> train owns enabled delivery. Start with the [train runbook](git-first-train-runbook.md)
> for activation, current status and batch controls.

The Subject publishers and runtimes were retired on 2026-10-12. Retained Subject
rows, gates and ownership records support historical recovery; they do not run
an alternative delivery engine. Set `integration.git_first: active` to construct
the train. The default `shadow` selects no writer, and changing
`integration.reconciler_active` does not pause the active train. Use the project's
train playbook activation or the batch controls to pause current delivery.

Inspect current state before changing configuration:

```bash
aq integration status PROJECT_ID
aq task show TASK_ID
aq doctor --check integration.delivery_engine
aq doctor --check integration.train_batches_stalled
```

Train status reports targets, batch intent, exact candidate checks, repair state
and blockers. Historical Subject holds remain binding where they apply to source
admission. Only for such a retained hold, resolve its named gate and use the
exact Subject version:

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
integration policy. Its `cadence_seconds` and `settling_cap_seconds` fields are
**deprecated**: they still parse, so stored `policy_snapshot` values validate,
but the fast flow ignores them. A lane dispatches as soon as it is idle and
batches everything eligible while it is busy; there is no timer, and sealing
now is the normal path rather than a bypass (spec 2026-10-12
fast-smooth-integration §6.2). The one
remaining reader is the `aq doctor` no-progress alert, which uses
`settling_cap_seconds` (default 1800) as its threshold. Omitting `train` keeps
the block absent from serialized snapshots, so pre-existing snapshots still
compare equal; do not add the deprecated fields to new policy.
Epic collection and local development targets freeze immediately.

The installable `fast-smooth-train` playbook supplies the current train policy.
Copy its shipped source into the project's playbooks, set its scope to
`project:PROJECT_ID`, and compile and activate it through the ordinary playbook
commands. Its `integration-policy.train` block chooses immediate admission or a
finite wait, in-place repair or a finite wait for red/conflicting candidates, and
the current required checks. Exact green proceeds through the existing publisher
fence; explicit holds and competing live writers remain binding. Unknown facts
are retried with a due time. This policy has no repair tasks or exhaustion gates.

Disabling that project activation stops new train batches and fenced writes,
including author-assisted integration, while workers can publish and close their
source branches. It preserves frozen sources and candidates. Re-enabling resumes
from Git. Editing required check names/version/producer affects the same pending
candidate; it does not abort and requeue that batch. The publisher rechecks the
current check policy inside its fence, and activation edits serialize with writes.

The conventional activation id is `fast-smooth-train`. For a renamed project
copy, set the project integration policy's `train.playbook_id` to its id. A missing,
unreadable or unhealthy bound artifact causes a finite retry, never legacy
publication. Existing projects without this activation or binding keep their
pre-install behavior until their operator installs it. The shipped template is
available on installation but is not automatically activated on existing projects.

Epic checks are advisory by default. An enabled, ready project playbook can opt
into a gate through `tables.parent_episode.required_checks`. Stored parent check
settings alone do not gate epic settlement; root and promotion gates still apply.

A red batch starts one task-free fixer session on its existing candidate branch.
The session uses a retained checkout outside task slots; subsequent red results
reuse both the session and checkout. It creates no repair task, claim or successor
repair. The project's reviewed routing policy selects the worker profile. Frozen
member file ownership and exact failing checks supply the failure groups in its
input. The startup brief is bounded; the complete frozen input and failure evidence
are in `.aq/tmp/batch-fixer-input.json` in its checkout. Large session prompts are
delivered through a local file pointer so they never exceed OS argument limits.
The fixer commits locally and runs `aq integration batch-fix --action push`;
this command derives its batch from the authenticated session and can publish
only that candidate branch. It refuses rewritten history, missing frozen sources,
changed session identity, withdrawn source authority and disabled integration.

Each distinct red result notifies the same fixer once. The primary repair time
budget and at most three CI rounds bound automatic notifications; exhaustion or
an uncertain launch reports once to the supervisor and preserves the work. It
never launches a successor or wakes an agent on a timer. Trusted green checks
advance delivery even after exhaustion. Exhaustion releases the lane, retains the
frozen sources and candidate, and schedules a finite re-observation (default
300 seconds). Unchanged exhausted evidence does not re-admit work or notify an
agent. A changed saved head or exact current-policy green can resume delivery.
Capacity waits retry the same unlaunched identity without spending code rounds.
An interrupted launch still awaiting registration after 120 seconds enters the
same bounded retry path when no session was registered or its process is confirmed
stopped. Each retry gets its own grace period; a possibly live registration stays
retained for reconciliation.
Only an explicit operator decision creates an indefinite hold.
When every conclusive red group identifies a strict subset, the repair policy's
`split_attributed` decision (default true) removes that subset and its transitive
dependents. A reduced batch tests from the same delivery tip. Shared or uncertain
evidence prevents splitting. The original frozen batch and all fix commits stay
as a retained companion; cleanup cannot delete them. Each reduced membership is
attempted once. A split alone never starts a fixer.

After the remainder settles, a successor freezes the original sources and merges
the retained companion, including fixes saved since the split. A red successor
reuses the same fixer. The fixer saves and publishes its current work, then runs
`aq integration batch-fix --action rebind` to switch its clean checkout under both
branch fences. Publication derives the current binding from the session and
rechecks it under lock. Companion writes and delivery are serialized so an older
green result cannot lose a later fix. Full family delivery stops the session.
The project boundary's repair policy also supplies `primary_seconds`,
`primary_attempts` (capped at three), and `deferred_revisit_seconds`; legacy
`on_exhausted: human` does not create an automatic hold for task-free fixers.

Existing ordinary repair tasks remain readable and can finish through their old
close protocol, but the active allocator does not file new ordinary repairs.

Passing a legacy ordinary repair close reseals its current open batch and wakes
the delivery target. A later visit recovers a missed close handoff, including after
repair archival. Neither action changes frozen sources or bypasses candidate
checks. `integration.train_batches_stalled` and `stall.sweep` flag sealed batches
with no progress past `settling_cap_seconds` (an alert threshold only), allowing bounded visits and announced
retry deadlines. Paused batches and unfinished repairs retain their own
diagnostics. Inspect the named lane and checks before changing batch intent.

The daemon also opens one durable escalation per sealed, open batch after 60
minutes without progress. It notifies the project supervisor and queues a Discord
incident through the escalation outbox. Snapshot errors, running visits and retry
deadlines do not postpone this alert; a recent repair update does. Paused, aborted
and promoted batches are excluded. Cancel the incident after recovery; the alert
itself does not pause or resume delivery.

An empty loose object in a retained integration clone triggers automatic recovery
after a failed fetch. The daemon quarantines zero-byte objects under
`.git/aq-empty-object-backups/`, refetches the authorized origin without trusting
local tips, and requires `git fsck --full` to pass. A `pending` marker retains an
interrupted or failed recovery for the next visit. Local candidates and refs stay
in place: an object available only locally cannot be restored from origin, so its
snapshot remains unknown and requires operator recovery. Retained clones enable
Git fsync for committed data. See the
[recovery contract](../specs/design/integration-repository-recovery.md).

Speculative next-batch CI is disabled by default. Enable it last, per project,
only after operator validation of hosted root CI and independent runner capacity.
The policy's `train` block accepts:

```yaml
train:
  speculative_ci: true
  speculative_public_hosted: true
```

`speculative_public_hosted` records the operator's validation that this repository
is public and uses free hosted runners. A private repository instead needs
`speculative_batch_ci_p90_seconds` set to its measured batch CI p90, strictly below
60 seconds. Without either form of evidence, validation refuses with
`speculative_ci_compute_not_eligible`. Local or hybrid root CI refuses with
`speculative_ci_requires_hosted_root`. Keep Matter Engine disabled while it uses
local compute. No speculative setting is emitted at its default value.

While a root batch waits for CI, the train may build one disposable successor on
its exact candidate. The successor has no delivery target ownership until ordinary
admission freezes it after the predecessor lands. Its exact hosted run appears as
a shared `ci_run` wait. Reuse requires that its first parent is the published tip
and that the predecessor landed without split or repair. Otherwise the train
rebuilds normally. Speculative work runs separately; an unfinished build never
holds up the predecessor's publication. Restart can lose speculative scheduling
hints; Git parentage, durable predecessor state and current checks still govern reuse.

The `seal_now=True` hooks remain accepted by train visits and batch selection.
Idle lanes already dispatch immediately; these hooks preserve all admission and
publication checks and do not arm future work. The supervisor
can use `aq integration seal-now --project PROJECT_ID --apply` to freeze the
currently eligible members immediately. It leaves building, checks, reviews
and publication to the train. If a batch already owns the target, the command
reports `existing_batch` with that batch's identity rather than `sealed`.

Selection still refreshes stacked members and conflicting epics before it freezes
a batch. A visit cancelled by its deadline or by shutdown while such a refresh is
running waits for the refresh to finish recording any pushed source before the
visit is released, so a fenced push never lands without its completion record.

For a misbehaving batch, first use `aq integration pause-batch BATCH_ID --apply`.
It preserves the frozen inputs and blocks candidate and target publication.
`aq integration resume-batch BATCH_ID --apply` restores open intent.
Pausing a paused batch or resuming an open batch refuses with `refused`.
`aq integration status PROJECT_ID` shows batch intent and member disposition.

`aq integration eject --batch BATCH_ID --task TASK_ID --reason "isolate member" --apply`
atomically aborts the old batch and freezes its remaining exact sources under
a new identity. A paused batch produces a paused replacement. The original
membership stays intact, and the removed task keeps its PR and approval,
returns to pending, and can enter a new batch on a later eligible visit. An ordinary
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

The Git-first path rebuilds on target movement. Historical `on_main_moved`
settings do not change that behavior. The train playbook supplies finite policy
waits; these are distinct from the legacy candidate-rebuild settings.

The project policy `hierarchical_integration_policy.prerequisite_branches` accepts
`stacked` or `wait-for-parent`. Every project defaults to `stacked`; an explicitly
authored `wait-for-parent` choice remains in force.

In stacked mode, completed passing `blocks` prerequisites in the same repository
can supply sources from siblings, another epic or a root task. They do not wait
for parent assembly or default-branch delivery. Git's retained completion names
the immutable final source; deleting or moving its task branch does not change
that generation. Preparation includes the current parent/default base and every
required source, ordered by dependency topology then stable task ID. Contained
heads are deduplicated, fast-forwards are allowed, and divergent inputs use real
merge commits. Every required source must be an ancestor of the prepared head.
The origin records this stack separately from its immutable filing base.
Preparation preserves existing child commits under the managed writer fence.
Scheduler, pool demand, explain and
claim consume the same verified frontier rule. Cross-repository dependencies
continue to use their artifact contract and cannot contribute a Git stack.

Explicit holds, required human decisions and current reviewer rejections travel
through prerequisite sources and their owning epics. An open epic or a missing
epic review row alone adds no gate. Reopening or replacing a prerequisite
invalidates the consumed generation. A running worker receives `stack_stale_input`
on heartbeat and on a passing close until it merges the current required sources.
Its checkout and authored commits are preserved; ancestry of those sources
satisfies the stack without a separate review or proof submission.

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

With an explicit `wait-for-parent` choice, a completed prerequisite in another
epic, or a root prerequisite, must reach the project's default branch before its
dependent becomes claimable. The configured delivery observer proves the exact
completion source. `aq task explain` names an unproven prerequisite and its epic
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

In `wait-for-parent` mode, before starting a cross-epic dependent, its epic must
contain the proven sources. A refresh brings them from the default branch through
a frozen train batch.
Publication needs the epic's lease,
checks on the exact candidate and attestation. Checks or an occupied lease keep
the refresh pending; a content conflict starts the same task-free fixer on the
refresh candidate and withholds children whose prerequisites remain outside the
epic. The train publishes the repaired candidate only after its exact checks and
attestation pass; the fixer cannot bypass that gate by pushing the epic itself.
Preparation preserves the child's existing
commits and records its actual refreshed parent head and default head in the
filing origin's `base_refresh` annotation. The original `base_sha` stays fixed.
Preparation requires the epic to contain each proven cross-epic source; unrelated
default-branch commits arriving during CI do not force another refresh. The
default stacked mode instead merges those exact sources directly into the child
over its current epic base, without requiring an epic refresh.
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
For stale children, `aq doctor --check stall.sweep` reports `epic_refresh_needed`
and the operator stall script prints `EPIC-REFRESH-NEEDED`, with the epic's refresh
command and any blocking batch ids. A conflicting apply files an ordinary train
repair on the epic branch. Repeating apply reuses that batch and repair; the child
continues waiting for the refresh's checks, attestation and publication.
Repairs for failed or missing CI start from the tested refresh candidate; conflict
repairs start from the partial candidate containing the successful merges.
Refresh failures are compared with the default SHA frozen into the batch. A
failure already present on the epic is still repairable when that default SHA
passes the check. Status reports `epic_refresh_checks_failed`, the exact baseline
and a supervisor action. Exhausted repairs send one incident asking the
supervisor to repair the refresh candidate or abort the batch with
`aq integration abort-batch BATCH_ID --apply --reason REASON`. Red candidate
checks continue to block publication after exhaustion.

GitHub runs no `pull_request` workflows for a PR that conflicts with its base, so
a conflicting root PR would wait for checks forever. When the PR read reports it
conflicting, the root's blocker is `pr_conflicting`, with the PR URL and the
default-branch SHA; a still-computing mergeability is a short `awaiting_pr_checks`
retry. For leaves and epics the train first tries a mechanical merge using the
regenerating drivers. A clean result is pushed as a bot merge, retaining the
original authored head's PR checks and approvals for admission. Batch-tip CI
still gates delivery. A real source conflict reopens the original task on its
own branch with the conflicting paths and default tip; its worker merges,
runs targeted tests only and closes. The train never reopens the same
(head, default tip) pair twice, including after a daemon restart, and preserves
the task's original age. The hot-file fix (Phase A5) must ship before or with
this policy. Explicit `refresh-epic --apply` remains available. Push-event runs
never count as PR checks.

Status retires an old `pr_conflicting` diagnostic when a historical code receipt
matches the completed task's current source and root target. This includes a
cherry-picked delivery whose carrying commit differs from the PR head. Receipts
for another repository, target, head or an older completion leave the diagnostic
visible. This read-only reconciliation does not change train admission or Git
delivery truth.

Root admission first probes the fetched target and source with local `merge-tree`.
A source conflict reports `pr_conflicting` without a GitHub read; conflicts confined
to files marked `merge=aq-generated` follow the build's regeneration policy instead.
An unavailable probe reports unknown. Conflicting refusals refund the selection
work quantum, leaving time to admit clean sources in the same visit.

For an existing historical root subject, `engine-transfer` offers a forward-only
audited ownership transfer to `reconciler`. It does not install a publisher.
Development subject transfer was retired with the dormant development runtime. Preview first, then supply every exact subject version, the reason
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

Time spent evaluating a root refused for red PR checks or exhausted automatic
recovery does not spend the candidate-selection quantum. Later independent roots
can reach admission during the same visit; green and other candidate work still
spends the quantum. The outer visit deadline remains in effect. Red roots keep
their named blockers, and their dependents still require delivery or an admitted
paired repair before they can enter a batch.

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

- `aq integration seal-now --project PROJECT_ID --apply` requests an immediate visit to freeze eligible root inputs when the target is idle.
- `aq integration pause-batch BATCH_ID --apply` pauses publication for an unpromoted Git-first batch.
- `aq integration resume-batch BATCH_ID --apply` resumes a paused Git-first batch.
- `aq integration abort-batch BATCH_ID --apply --reason REASON` aborts an unpromoted Git-first batch, including one whose target ref is confirmed missing. Preview reports `target_missing: true` and a null target SHA in that case; unavailable Git evidence still refuses abort. The train automatically aborts an open epic refresh when its target disappears. Abort tells its batch fixer to stop and preserves the checkout and history.
- `aq integration settle-batch BATCH_ID` previews settlement when every frozen source is already on its target. A legacy epic-sync instead requires every frozen source on the designated default branch; preview reports that default `target_ref` and its SHA, and replay can resume cleanup of an already promoted sync. An epic refresh settles once its epic has the frozen default head as an ancestor, including a merge its repair published to the epic; until then `abort-batch` withdraws it. Apply with `--apply --target-sha TARGET_SHA --reason REASON`. It records delivery without advancing the target; normal guarded branch cleanup still runs, and active human holds and incomplete or unavailable source proof refuse settlement. See the [required-job and settlement specification](../specs/design/required-job-train-settlement.md).
- `aq integration refresh-epic --task EPIC_ID --apply` starts or advances the attested refresh; pending checks or repairs continue through the train.
- `aq integration reverify-epic EPIC_ID` previews aggregate verification for a completed epic. Apply with `--apply --head HEAD_SHA --reason REASON` to request one bounded train visit, including when the old owner is released. The result names the actual visit state and blockers; `visited` does not mean checks or review passed. Read the [control specification](../specs/design/epic-aggregate-reverification.md) for the operator handoff.
- `aq integration record-root-noop ROOT_TASK_ID` previews a no-artifact completion for an unheld no-code root; apply requires `--apply --head HEAD_SHA --reason REASON`.

After a hand landing, an ordinary open epic batch settles automatically when
the configured default branch proves every frozen input, including when the
epic ref has been deleted. Its membership and history remain available; no ref
is recreated or published. Partial or unknown proof and human holds preserve
the batch. Paused, epic sync/refresh and promotion batches keep their protocols.

Completed root epics receive ordinary aggregate train visits automatically,
including after child origins retire. They do not depend on a parent integration
run. A missing root origin stays visible as a readiness blocker. Implementation
settlement alone does not display delivery as complete: the epic waits for
aggregate verification or reports unavailable delivery evidence. Both
`integration.completed_undelivered` and `stall.sweep` report completed root
branches outside the default branch even when a PR, batch or disposition
accounts for the pending work. The diagnostic clears when the branch tip is
contained; an unreadable ancestry result remains unknown.

Reverification apply also retires the completed epic's own detached `worker`
reservation under its branch fence. The reservation must have no session,
workspace or unexpired lease. Preview leaves it intact; live writers, other
owners, attached rows and open batches still block the request.

A target visit first settles an ordinary batch whose complete frozen inputs are
already delivered. Current bound playbook checks overlay a pending candidate as
described above. On the retained stored-policy path, removing a required check causes
the next root or epic target visit to supersede an open, unpromoted batch that froze that check. Its
sources can seal under the updated policy; the original snapshot and sources
remain in the audit history. Special epic sync and refresh batches retain their
purpose in an atomic replacement. The visit names `batch_required_checks_removed`
and reports whether it released the batch. Explicit pauses and human holds stay
in force: resume a paused batch, or release the recorded human decision, before
replacement can proceed. Missing Git proof retains the batch. Promotion requests
keep their separately pinned check sets.

The controls keyed by a task, operation, batch or reservation — `redrive-root`,
`redrive-child`, `record-noop`, `reopen-collection`, `reserve-owner`, `release-owner` —
take no `project_id`, so authorization resolves their target project server-side.
A live elevated named project supervisor or global supervisor may record no-code
receipts, alongside a local operator or an authorized playbook. Workers and
supervisors outside the child's project are refused. See
[the command scope model](../specs/design/aq-surface.md#73-elevated-scopes-and-the-commands-that-carry-no-project_id).

Current recoveries reuse the existing episode, fence, receipts and stage budget.
Always preview, resolve the reported blocker, and apply the exact reviewed head
and other fences. Never edit integration tables to bypass a gate or CI proof.

```text
aq integration redrive-root TASK_ID
aq integration redrive-child CHILD_TASK_ID
aq integration reopen-collection PARENT_TASK_ID
aq integration reevaluate-repair OPERATION_ID
aq integration release-owner --task-id REPAIR_TASK --dry-run
aq integration close-delivered-pr PROJECT_ID PR_NUMBER
```

## Repair and collection recovery

`aq task reparent --task-id CHILD_ID --root` can move an unheld no-commit leaf
out of an epic. An unused reservation is replaced from the default branch.
A materialized branch must still equal its recorded base; its branch and base
remain unchanged while delivery now targets the default branch. Moving to
another parent requires that parent's checkpoint to match the materialized
base. The transaction advances affected parent generations and invalidates
their verification. Live writers, workspace locks, attached branch owners,
delivery receipts, completed work and sealed ancestors refuse the move.

Branch ownership cannot attach to the shared repository base checkout. Recovery
ignores a stale base-workspace attachment when the task branch is elsewhere,
without snapshotting, detaching, unlocking or disabling the operator checkout.
A task branch actually checked out in the base still requires operator attention.

The train owns current integration work. Inspect `aq integration status PROJECT_ID`
and the blocker returned by its batch or repair operation. A stopped attachment uses
`release-owner --task-id TASK_ID --dry-run`; apply only the exact recovery that the
preview proves. An audited no-op repair can be previewed with
`aq integration reevaluate-repair OPERATION_ID`. Its apply requires the reported
head, generation, stage, fence and snapshot digest plus a reason, and repeats the
trusted exact-head green-CI proof.

The former candidate-member and parent repair rebind/preserved-publication commands
were retired with the legacy publishers. Their historical dossiers and evidence remain
available to recovery and archival checks; they do not authorize a legacy publisher.
See the [consumer retirement audit](../reports/2026-10-12-legacy-consumer-retirement.md)
for the retained contracts and table exclusions.

### No-code child receipts

A reviewer filed under an active parent is itself a child in the collection
episode. Its `pass --work-outcome no-op` close records the review verdict, but
the parent still needs a disposition receipt for the reviewer's own branch.
When parent readiness reports `receipt_missing` for that child, a local operator
or live elevated named supervisor records the exact no-code disposition:

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
that exact capability. Supervisors retain these evidence guards; ordinary worker
sessions cannot invoke it. A child with real code requires an explicit obsolete
disposition rather than a no-code receipt.

For `obsolete_child`, the train and `redrive-child` supply the one-command
`aq integration redrive-child CHILD --dispose-obsolete --superseded-by SHA
--apply --head SOURCE --parent-head TARGET --reason REASON`.
Review the exact source and parent heads first. The superseding commit must be
on the parent and remove the last of the child's changed paths. Preview without
`--apply` is read-only. Apply performs a fenced ours merge: the parent keeps its
files and gains real child ancestry. The merge commit records the reason, actor
and superseding commit; completion history remains intact. Local operators and
live project/global supervisors can apply it without a human gate. Head movement,
live writers, active batches and unverified provenance refuse the write.

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

Failed pool preparation can attach a branch owner before checking out the task branch.
If the exact claim is still `preparing`, the checkout is clean and detached at the freshly
published default tip, and the task branch is fully published, failed preparation releases
the claim to READY. `aq task stop TASK_ID` uses the same proof for a retained preparation
and also returns it to READY; `aq task restart TASK_ID` can then retry it. This preserves
the task's existing resume point and never discards dirty or unpublished work.

The orphan sweep automatically preserves and releases a confirmed stopped pool
writer's retained active claim to READY before the `session_not_live` fallback.
It leaves refused proofs and operator holds in place for recovery and respects
`integration.owner_recovery_sweep: false`.

Preview `aq integration release-owner --task-id TASK_ID` (or add `--dry-run`). The existing
owner recovery probes the provider, verifies the current fence, preserves
unpublished work and releases only a proved stopped attachment. It refuses a
live writer, reused checkout or changed fence. Repair handoff and retry remain
Subject decisions under the frozen policy and unchanged stage budget.

A project supervisor can use this control for a task left
`BLOCKED/session_close_hard_failure` with `integration_handoff_unproven`. Review
the dry-run result, then add `--apply` to release the stopped attachment.
If its session is still live, stop that exact session through the normal session
control and wait for confirmed termination; a stopped database status alone is
insufficient. Release preserves the task's failure status. After reviewing the
failure, use `aq task restart TASK_ID` for an intentional retry. `aq task recover`
does not automatically retry a hard failure.

An IN_PROGRESS task whose stop returns `Integration branch handoff is unproven` uses this
same recovery control. The refusal names its exact writer and the commands to run: kill
that session, wait for termination to be confirmed, preview and apply `release-owner`,
then retry task stop if it remains IN_PROGRESS and restart it. A recovery incident is not
required. The release audits and preserves work before freeing the branch and claim.

Failed-close cleanup keeps HEAD at the task tip until handoff verifies and
detaches it. A live pool worker uses its claim and checkout proof, while a
task-lifecycle worker also needs a provider stop proof. This protocol is the
same for every harness and model.

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

Historical Subject rows retain their identity and pins; no Subject runtime is
started by the current daemon. `integration.reconciler_active: false` does not pause
the train. Use the bound project train activation to stop train work, or pause the
specific batch. Neither operation revokes a live writer or restores old policy.

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

New source-CI recovery retains the original branch and PR and reopens a leaf task
once. The budget persists across repaired heads and checkpoint generations.
A further genuine failure, including failure on a historical repair delegate,
notifies the supervisor once; it never creates another task, repair PR or chain.
Existing historical delegates retain their ownership and delivery protections.
Containers notify the supervisor to coordinate repair on the original branch.
After the original source lands, the supervisor closes its obsolete repair PRs.

Source CI observation of a train root's exact PR head reopens the original task only when
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

The delivery watchdog keeps one wait projection per repository, task completion
generation and target. Repeated and overlapping scans reuse that row, including
its original completion age and takeover history. A previously settled wait keeps
its outcome when the same generation is observed again. Each task observation
and each settlement has its own transaction: a failed task rolls back, is logged
with its project and task id, and appears in the scan's `errors` list. Other tasks
continue and the project cursor advances; the failed task is retried on a later
pass.

The train automatically re-runs a required Actions job once if its check stays
`in_progress` for five minutes after its workflow or all job steps completed.
The check remains pending until fresh authenticated evidence concludes it.
Integration status includes durable `check_reconciliation` evidence with the
exact SHA, job, workflow attempt, actor and request outcome. A reserved request
after interruption, or an unavailable request, consumes the same one-shot bound.

The owning supervisor can preview and apply the same bounded recovery:

```bash
aq integration rerequest-check BATCH_ID --check 'Tests (postgres-integration)'
aq integration rerequest-check BATCH_ID --check 'Tests (postgres-integration)' --apply --candidate-sha PREVIEW_SHA --reason 'Reconcile completed workflow with unfinished check'
```

This uses GitHub's [job rerun endpoint](https://docs.github.com/en/rest/actions/workflow-runs#re-run-a-job-from-a-workflow-run),
which requires Actions write permission. A failed or ambiguous request is retained
for supervisor attention; it never makes the check green or files a code repair.

`task show` and the dashboard report delivery from receipts and observed Git
history. A worker close, queued request or historical journal entry alone does
not prove that the task's exact revision reached the default branch. Pending
root requests are revisited from durable state after restart; event delivery is
a wake-up hint, not the authority to seal or publish.

Parent readiness still requires the existing child dispositions and trusted
verification receipts. Reconciler decisions use those facts through the shared
ports. Historical development operation rows remain audit and recovery inputs;
they do not restart an autonomous publisher.

`aq doctor --check integration.delivery_engine` identifies projects whose selected
delivery engine is unavailable or whose designated integration repository is
missing or belongs to another project. `stall.sweep` reports the same failures
for active projects, and task explanations say "no delivery engine for this
project" when a prerequisite cannot be delivered. Restore the selected runtime
and repository binding with the operator configuration commands, or explicitly
disable integration. Enabling a mode without an engine is refused. An active
train supports `development` as well as `train` and `hierarchy`; `observe` and
`disabled` intentionally perform no delivery.

Enabling `train` or `hierarchy` also backfills branch origins for existing open
tasks and completed tasks that carry legacy delivery artifacts. Published task
branches are adopted at their exact remote tips, with their fork bases recorded
separately; absent branches receive reservations for ordinary materialization.
Existing origins, checkpoints and retired completion history are preserved.
An unknown remote state or conflicting branch ownership refuses the edit and
rolls back its database changes.

Re-enabling the train playbook also recovers detached READY roots filed while
it was off: branch reconciliation backfills missing origins before draining
reservations. The same bounded pass catches system filings that have no origin;
the delivery watchdog reserves an origin when it files a takeover while the
train is enabled. Failures are retried without blocking later roots, and the
pass rechecks train activation and writer ownership under the project lock.

A reservation is cut by the branch materialization drain
(`src/integration/branch_materialization.py`), ten origins per pass, so a
large backfill reaches its last origins minutes later; until then those tasks
are honestly `origin_not_materialized` and `reserve-owner` refuses them because
the origin is not yet materialized. Backfill keeps a pre-existing managed owner row and leases
it to the task with the finite ref-lease TTL, which a long backlog can outlive.
The drain then re-leases that row before cutting the ref, but only under all of
these, checked under the project hierarchy lock and the ref lease lock:

* the origin is live, reserved, unmaterialized, and names the task's exact
  branch in the project's designated integration repository;
* the owner row is a managed `worker` lease held by that same task, expired,
  and attached to no session or workspace;
* the task has no assigned agent, no attached session and no locked workspace.

Re-leasing bumps the fence (`BranchLock.acquire_on`), so any older grant is
fenced out, and records an `integration.origin_lease_reacquired` event with the
previous and new fence. The cut itself is unchanged: an absent ref is created
at the pinned base, an exact one is accepted, a ref published at any other
commit is refused (`delivery_target_fixed`) and kept, and an unreadable remote
stays unknown. A foreign or unexpired holder, a live writer, or legacy
ownership is never re-leased. Task status, holds, routes and budgets are left
as they are; only a DEFINED, unblocked task becomes READY. No operator step is
needed. The drain retries the origin on its next pass.

For projects enabled before this backfill existed, run `aq doctor --check
integration.branch_origins`, then add `--fix` to repair through a generation-fenced
project mode edit. App verification, functional preflight and cutover all report
`branch_origins_missing` until the affected tasks have delivery identities.
Adoption alone does not prove delivery: completed work still needs its exact
completion provenance and the normal batch admission checks.

COMPLETED therefore does not mean delivered. `aq doctor --check
integration.completed_undelivered` lists every COMPLETED task, archived ones
included, whose branch tip the default branch cannot reach, with the rule that
accounts for it (in a live batch, a pending retirement, a recorded re-landing,
a root PR still inside its delivery window) or the remedy when nothing does. The
`stall.sweep` patrol and `aq task explain` carry the same findings. A train root
whose PR is green yet not delivering answers `aq integration authorize-root`
with the condition it fails or the train blocker it waits on, never a bare
`not_eligible`. Re-landed content and archive dispositions are recorded as in
[branch retirement](../specs/design/branch-retirement.md#re-landed-content).

## Author-assisted parent merge on close

A passing worker close in an active train project can retain the author through
its merge into a non-default parent branch. The daemon freezes the clean,
exactly published source commit, creates a separate warm candidate checkout and
performs a real two-parent merge. The task branch remains the completion source.
Conflicts and failed candidate checks return the candidate path to the same
claim holder; commit fixes there, then run:

```bash
aq integration continue-close --operation-id <id>
```

Each changed candidate is regenerated and checked against the current parent
check requirements. Managed jobs use the shared cache and resource limits. A
pending exact job/check returns a verified wait; follow that receipt instead of
polling. Only the publisher can update the parent, with a short expected-old
lease, renewed claim and explicit-hold checks, and remote readback. Parent
advances merge the retained candidate onto the new parent, preserving author
fixes, with at most three automatic rebuilds.

`hierarchical_integration_policy.close_merge` accepts `author_assist` (default
`true`) and `author_budget_seconds` (default `1200`). Defaults are omitted from
policy dumps. This reader uses the same live project mode and parent check
projection as the git-first train; retired playbook-route pins do not gate a
close. A disabled train completes source work normally without a close proposal
or parent lease. `author_assist: false`, unavailable candidate validation, or
exhausted automatic retries hand the exact retained candidate to the existing
publisher. Root/default-branch publication remains publisher-owned.

An author budget is an integration-origin verified wait. Expiry sends one
incident to the project supervisor; it does not pause, wake, or release the
author. Changed source generations cancel obsolete budget observations while
retaining their workspaces. An uncertain push is resolved from current Git
ancestry, never from the operation's stored `finished` state alone.

Storage disposition: the approved fast-smooth-integration section 5 adds
`integration_close_attempts` (migration `a00000000117`). It stores version-fenced
proposal identity, retained candidate and validation evidence, and links the
integration budget wait. It is not a delivery receipt or a second scheduler.
The source completion continues through the existing accepted-close recovery
protocol. This additive table and all close helpers remain charged to the
integration ownership inventory; historical and final size/table caps are not
raised by this feature.
