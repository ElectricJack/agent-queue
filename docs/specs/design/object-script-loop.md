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
views and their reservation in their task description. They publish immutable
candidate and capture bundles outside their worktree. The receipt names
durable URIs and SHA-256 digests; the object command validates the schema,
cross-references and completeness. The external artifact adapter remains
responsible for serving bytes by those URIs and verifying their digest during
materialization. Arbitrary local paths are not accepted as durable URIs.

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
`gpu_id`, the admitted rig's `hold_frames`, the view set with each view's
resolution, and the VT readiness counters that prove the frame had converged —
plus the admitted candidate and rig identities and the capture's readiness
state. The document is built from named fields only and hashed in AQ's single
canonical JSON form, so run ids, request ids, timestamps and absolute paths
cannot leak into it: one preset yields one digest, and any change to any of
those inputs changes it.

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
