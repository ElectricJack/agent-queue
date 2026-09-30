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
