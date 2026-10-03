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
same loop-row update as the score decision. Eight rounds, repair and plateau
caps are enforced.

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
