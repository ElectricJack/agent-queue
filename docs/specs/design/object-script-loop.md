# Object-script loop contracts (AQ-2)

AQ coordinates bounded evaluation rounds for an external object generator and
scorer. Matter owns rendering and image metrics. A reviewed V2 playbook (AQ-3)
will call these commands; this change does not activate a playbook or run a
capture. The approved source is Matter review `rev-amber-zenith`, revision 2.

## Commands

`object_loop_start` binds one root epic and one `object_id` to immutable
reference, rig, scorer, render-profile and policy hashes. It requires a
finite first wave, a whole-attempt budget, scorer/retry reserve per wave,
a separate final-suite reserve,
and the exact approved brief review revision and document hash.
The three round caps — `max_rounds`, `max_repair_rounds` and
`max_plateau_rounds` — are start input and part of that fixed identity.
The command creates the finalization child and its event gate in the same
transaction as the loop row. The first candidate is admitted only by
`object_loop_reconcile` after that transaction commits. A repeated start with
the same fixed inputs returns the existing loop; conflicting inputs fail.

The start packet records `reference_kind`, either `calibrated` (the default)
or `self`. An explicit supervisor `self` packet discharges only the calibrated
reference precondition for a plumbing pilot. The kind is fixed for the attempt
and cannot change on replay. Candidate and finalization descriptions label
self-reference results as indicative, for plumbing only. All repair, plateau,
round, whole-attempt budget and final-suite reserve bounds continue to apply,
as does the experiment publication refusal.

`incumbent_capture_sha256` is required for both kinds and identifies the retained
baseline capture receipt (`job_retain`'s artifact of kind `capture_receipt`).
It is fixed for the attempt and remains distinct from `incumbent_sha256`, which
identifies the candidate manifest. For `self`, packet validation requires
`reference_sha256 == incumbent_capture_sha256` before any loop or child is
created. For `calibrated`, the reference hash remains independent of this
baseline capture hash. Legacy loops lacking that capture identity can still
reconcile, but a new start cannot silently bind them to a claimed baseline.

`object_loop_reconcile` is safe to call from task, review or timer events and
after restart. It takes the loop row lock and re-reads task settlement. Its
stored intent uses `object:<id>:attempt:<id>:round:<n>:variant:<id>:candidate`
keys. A child created just before a crash is adopted by that key, parent and
creator identity. Completed, exhausted FAILED and BLOCKED candidates all
settle fan-in; an incomplete wave never creates a scorer. The scorer is a
sibling, with its own stable key. No blocking dependency on a failed candidate
is required. Reconciliation after a recorded stop releases the finalization
gate; it never approves an asset or erases failed children.

`object_score_record` requires the completed scorer, the exact loop version,
all completed candidate receipts, and settled wave membership. It rejects
foreign task/variant/round/base/reference identities, missing mandatory views,
non-finite metrics, missing candidate or capture artifact pointers, and
inconsistent quality gates. Invalid captures retain null quality and cannot
win. It ranks eligible candidates by mean loss, then worst-view loss, and
keeps the incumbent unless the winner beats the repeat-noise band. The
external scorer supplies per-view measurements; this command never computes
an image metric. Missing receipts or cost coverage marked unknown charges the
full wave reservation even when a lower measured aggregate is supplied. A
continue decision reserves every sibling and retry allowance together in the
same loop-row update as the score decision. The round, repair and plateau caps
are enforced. The round ceiling is `object_loop_start`'s `max_rounds`, one to
eight and eight by default, so a pilot cap of three rounds is three rounds of
its wave size rather than a `calls` budget that happens to fit one variant per
wave. It is fixed input: a repeat start with a different `max_rounds` is
refused as different fixed inputs, and a row admitted before the field existed
keeps the eight-round ceiling it was admitted with. A refused continuation is
a cap, not a dead loop — the loop still records a stop with its reason.

An exhausted FAILED or BLOCKED scorer may instead record a defect stop with
the exact loop version, `action=stop`, an explicit nonblank reason and no
receipts, next variants or checkpoint review. The wave must already be settled;
a FAILED scorer with retries remaining cannot stop the loop. This path charges
the full wave reservation, records the scorer failure, preserves the incumbent
and its metrics, and admits no more candidates. It requires no asset approval
and never records successful scoring or resolves an approval gate. Replaying
the same decision is idempotent. Terminal reconciliation rechecks candidate and
scorer settlement before releasing only the loop's terminal gate, and does not
advance the loop version on a replay, including after restart. Reopened work
keeps that gate held until it settles again. Failed children retain their status.

`object_checkpoint_read` checks the recorded review ID, exact current
revision and document hash, approved state, decision timestamp, project and
candidate hash. Review events only wake reconciliation; they are never proof
of approval. A checkpoint continuation must name the expected loop version
and a finite next wave. A terminal stop instead requires the current loop version
and an explicit reason, independently of brief or candidate approval. It retains
the checkpoint and verified incumbent without changing any review decision,
clears pending creation intent, and never creates another wave or scorer. An
exact retry of the stop version and reason is idempotent; conflicting stops and
continuations after a stop are refused. The finalization gate remains held until
all existing object candidate/scorer children settle. Reconciliation after restart
retries gate release without changing the recorded stop or loop version.
Reconciliation files no continuation task while the checkpoint is unresolved or stale. A stop can
release the finalizer for a defect report without approving product code.
The brief gate and the approved checkpoint gate are attached to their
respective candidate tasks in the creation transaction; the exact revision is
rechecked when a score is recorded.

## Evidence and publication

Candidate workers receive exact hashes, a base artifact pointer, mandatory
views, their reservation and a `round_handoff` block in their task
description. They publish immutable candidate and capture bundles outside their
worktree. The receipt names durable URIs and SHA-256 digests; the object
command validates the schema, cross-references and completeness. The external
artifact adapter remains responsible for serving bytes by those URIs and
verifying their digest during materialization. Arbitrary local paths are not
accepted as durable URIs.

### The round handoff

A candidate worker can only build on the rounds before it if it is told what
they changed. Every recorded score appends its wave's variants to the loop
row's `round_history`, each entry carrying the round and variant, the task,
the candidate identity and its retained bundle artifact, the branch and commit
that produced it — read from the task's own close record, because the worktree
slot a round used is released when it settles — the validity, the hypothesis,
the patch scope, the predicted and observed effect and the mean loss. Round 0
has no such history; that is what the incumbent bundle artifact is for.

The next candidate packet carries that history as `round_handoff`, together
with the incumbent bundle and capture identity, the frozen comparison inputs
(`reference_kind`, `reference_sha256`, `rig_sha256`, `scorer_sha256`,
`render_profile_sha256`, `policy_sha256`, `mandatory_views`) and explicit
instructions: materialize the incumbent bundle and verify its digest, read the
earlier rounds' retained artifacts and branches in scope, capture only under
the attempt's frozen inputs, and submit every capture of the attempt under the
same `--attempt-id`. Scorer packets carry the same block, because a score is
only meaningful when candidate and reference were compared under one preset.
The history is bounded by the round ceiling and the three-variant wave.

### The editor pin

One attempt, one editor build. `matter_render` launches the editor named by
`resources.jobs.matter_editor`, which is a shared build that can be rebuilt
while an attempt is running — and then the attempt's pinned render profile
names bytes no later capture will produce, and every later capture's score is
refused as foreign. So `job_submit --attempt-id ATTEMPT` copies that build once
into `{data_dir}/editor-pins/binaries/<sha256>/<name>`, records the attempt's
pin, and every later submission of the same attempt launches that copy. The
copy is written through a temporary file and renamed, and verified against the
source digest, so a rebuild *during* the copy is refused rather than pinned
half-old, half-new. A new attempt id re-pins; a recorded pin whose binary went
missing or stopped hashing true is refused as `jobs.editor_pin_lost` instead of
being silently replaced, because a swapped build would swap the preset under a
running attempt. Submission without an attempt id still launches a
content-addressed copy, so one job's editor cannot move between submission and
execution, but it has no cross-job continuity. Pins older than the retention
window that no surviving attempt record names are swept with the other job
retention. The attempt id is the start packet's `attempt_id`, so the baseline
capture and every round's captures share one profile.

### Producing the two identities a receipt needs

Two producers close the gap between a finished render and a startable loop.
Both are commands, both are scoped to the caller's own job, and both are
idempotent.

`job_retain <job_id>` is the retention step. It copies the capture members a
receipt names — each view's image and the capture receipt — out of the transient
`runs/<job>/capture` tree into `{data_dir}/artifacts/objects`, a
content-addressed store whose objects are fsynced, never overwritten and never
reached through a symlink. Each retained member is re-hashed and refused if
those bytes no longer match the digest the completion receipt recorded. The
identity planes and the adapter's own transcript stay behind `aq job logs`
unless `--include-channels` asks for them, because a receipt names evidence and
not logs.

It also mints the *candidate artifact*. `candidate_sha256` is the digest of the
canonical candidate manifest with its own declaration removed, so retaining
those exact bytes produces an artifact whose digest **is** the declared
identity — which is what `ScoreReceipt` requires, since it refuses a receipt
whose candidate digest is missing from its artifacts. The bundle lives in the
author's workspace, so a released workspace is reported as such instead of
substituting a near-miss identity.

`artifact_verify <uri>` is the other half: it resolves an `artifact://sha256/`
URI and re-hashes the bytes behind it. A URI is a claim; something has to check
it. `s3://` and `https://` receipts belong to the external artifact adapter and
are refused here rather than guessed at.

### The render profile

`render_profile_sha256` is the digest of a canonical render-profile document:
the preset that produced the pixels — `editor_sha256`, `adapter_sha256`,
`gpu_id`, the admitted rig's `hold_frames` and `rig_sha256`, and the view set
with each view's resolution and format. The document is built from named
fields only and hashed in AQ's single canonical JSON form, so run ids, request
ids, timestamps and absolute paths cannot leak into it: one preset yields one
digest, and any change to any of those inputs changes it.

The object under test is deliberately *not* in it. The profile is the preset, so
two candidates rendered under the same editor, adapter, GPU lease, rig and view
set share one digest — which is the only way a candidate that is not the
incumbent can be scored at all: folding `candidate_sha256` into the digest gave
every non-incumbent a profile no start packet had pinned, and its receipt was
refused as foreign. The rig digest covers the view set and the lights the rig
fixes, so moving either still changes the profile.

What the *capture reported* is recorded beside the profile as `observed` and is
never hashed: the per-view VT readiness counters and the capture's readiness
state describe what this object produced, and a heavier object legitimately
queues more VT work. Convergence is still proved, by the retained capture
receipt and by the scorer's own `ready`/`decoded` flags.

`hold_frames` is the rig's *admitted budget*, not the frames a run happened to
settle on. A real four-view capture reports 95, 96, 95 and 106 stable frames for
one render, so hashing the observed count would give every run its own profile
identity and refuse every variation. The budget is admitted with the rest of
the render preset and recorded on the job contract.

The profile is computed where the result is assembled and covered by
`result_hash`, so `job_result` returns it and a start packet quotes a value AQ
computed. A result with no retained capture records no profile, and one whose
receipt cannot yield a profile records why: a passed render must not become a
failed job over a reporting field, and the gap must stay visible.

Candidate, scoring and finalization tasks carry the `object_experiment` metadata marker at
creation. The development publisher excludes marked tasks, and hierarchical
collection and promotion refuse them. A separate ordinary code task imports
only an accepted generator into the product repository. Task completion alone
does not grant that import or publication.

The loop row stores bounded identity, budget, wave, checkpoint and intent data
as PostgreSQL JSONB. Images and raw logs stay in the artifact store. Its task
references are soft so normal archive cleanup does not erase the loop record.

## Reviewed policy and formulas (AQ-3)

### Audience and decisions

Each attempt has two documents for Jack: a brief before admission and one
final result after settlement. The brief names the object in ordinary words,
the maximum number of rounds and the total budget. The result pairs before
and after images for every view, explains whether the object improved and by
how much (or why a measured comparison is unavailable), and says what would
help the next run. Neither document exposes attempt/round IDs or terms such
as receipts, incumbent or plateau. The finalization task must submit this
result as an `other` review; retries reuse that review rather than adding one.

Scoring, probes, capture checks, variations and continuation checkpoints are
internal. The loop decides within its fixed limits; any review authored by an
internal `object_experiment` task, including a descendant probe, has decider
`supervisor`, independently of the project's review delegation setting. Such
tasks have no implicit human-review deliverable. Explicit review deliverables
still apply and route to the supervisor. Only the finalization task routes a
result to `user`. Checkpoint packets must name a supervisor review. The brief
remains the exact, approved document required for admission.
Experiment review audiences cannot be changed by generic review delegation.
The schema migration also reroutes existing reviews with these experiment
markers, preserving their documents and decisions.

Supervisor reviews stay out of the human Reviews inbox, Discord review
notifications, dashboard toasts and activity gates, and the digest's requests
for human decisions. Review lifecycle events carry their decider for this
filtering. Result screenshots labeled Before/After are paired by view in two
columns with plain captions, without candidate IDs or hashes. A blocked loop
retains its detailed evidence for the project supervisor, who handles recovery.
Jack receives a single plain-English sentence only if he needs to do something;
ordinary capture failures or exhausted limits require no human decision.

The opt-in `object-loop` V2 bundle is shipped inactive. Its command grants do
not include review decisions, provider routing, source publication or activation.
The approved proposal is `rev-amber-zenith`, revision 2; object admission also
requires an approved, hash-bound `other` brief review in the object's project.
Importing/reviewing the bundle stores bytes; activation is a separate operator
decision naming the artifact digest and operational prerequisites.

`object` and `variation` are real vault formulas, seeded write-if-absent from
the package. Object cooking creates a root and a gated bootstrap leaf. The
gate is installed in the graph transaction, and is released only after AQ-2
has committed its finalization hold. Thus even a missed formula event cannot
settle the root before the first wave. Variation cooking uses an existing
suite directly under the object root, checks the exact approved checkpoint,
and gates and marks every leaf in the graph transaction. The hierarchy is
root → suite → seed/preset check. Exactly one suite per object consumes the
final-suite reserve; ten distinct seeds and finite presets are bounded on cook. All experiment tasks are excluded from
source publication; finalization waits for variations as well as candidates.

`object_loop_inputs` is a bounded read-only bridge from formula provenance,
loop rows, task settlement and review state to V2 inputs. Task/review events
are wake hints; a timer reads the same persisted inputs after lost events or
restart. Scorers hand off a strict JSON `ObjectScoreRecordArgs` packet in a
task note beginning `object-score:1\n`, before closing their own task. Only
the current completed scorer's latest packet is eligible. The bridge parses
the entire JSON document (never prose extraction), verifies scope/identity,
and leaves measurement validation and version fencing to `object_score_record`.
Workers receive no loop-mutator or sibling-task authority.

The artifact chooses continuation, checkpoint, defect and deadline stops.
Rejected continuation is retried once as a score-bearing stop, preserving a
valid winner when a cap binds. No repeated provider calls occur in the policy;
task retries must fit the reserved amount. Non-settled work remains held during
an outage. Review rejection, withdrawal or requested changes stop admission
without granting approval; a revised candidate requires new scored evidence.
An exact approved checkpoint can adopt its predeclared finite continuation.
The 24-hour object deadline stops admission; settlement still precedes release
of the finalization gate. The artifact cannot close tasks or erase failures.
