---
id: object-loop
kind: pipeline
scope: project:matter-engine-cpp
enabled: false
triggers:
  - formula.cooked
  - task.completed
  - task.failed
  - review.decided
  - timer.5m
---
# Bounded object experiments

This opt-in policy implements approved proposal rev-amber-zenith revision 2.
Import/review stores this artifact; activation requires a separate recorded
operator decision naming its exact hash. Require the Matter adapter, durable
artifact retention, approved brief, calibrated references, finite job/GPU lease
and budget reserves before activation. This bundle never activates itself,
approves a review, chooses a provider or publishes a generator.

Every rule performs the same bounded sweep for matter-engine-cpp. Events are
wake hints only; object_loop_inputs reads persisted formula, task, score and
review data. The timer recovers missed events and restarts. At most 32 pending
or active objects are read; exceeding this bound fails visibly. Operate the
pilot with at most two object epics and one GPU lease. A loop is processed only
when its policy hash equals this running artifact's hash. An operator must
explicitly migrate a loop to a different reviewed policy; drift cannot adopt it.

## Rule: on-formula

On formula.cooked, perform the sweep below.

## Rule: on-completion

On task.completed, perform the sweep below.

## Rule: on-failure

On task.failed, perform the sweep below.

## Rule: on-review

On review.decided, perform the sweep below. The event's verdict never grants approval.

## Rule: recover

On timer.5m, perform the sweep below, including after restart.

## Sweep

1. Call object_loop_inputs for matter-engine-cpp with limit 32. For each
   pending start, require its exact proposal revision/hash still approved and
   its policy_artifact equal to the running artifact hash. Call object_loop_start
   with the typed request, then object_loop_reconcile with the returned object
   identity. The gated bootstrap leaf is released only after the finalizer hold
   commits. A missing or withdrawn approval leaves the start held.
2. For each registered loop bound to this policy, a stopped loop calls
   object_loop_reconcile to finish settlement and gate release. An active loop
   older than 86400 seconds calls object_loop_reconcile with its current version
   and stop_reason `object wall deadline reached`.
3. An exhausted FAILED or BLOCKED scorer calls object_score_record with the
   exact current version/scorer, no receipts, action stop and stop_reason
   `scorer exhausted; retained incumbent; quality unavailable`. Reconcile next.
   Unsettled work, including provider outages and retries remaining, only
   reconciles. No model calls, provider substitutions or retry tasks are made
   by this policy; each task's retries consume its original reservation.
4. At a checkpoint call object_checkpoint_read. Rejected, withdrawn or
   changes_requested reviews stop with `checkpoint not accepted; evidence retained`,
   without resolving the review gate or adopting a revision as approved. An
   exact approved checkpoint with a finite predeclared next_variants packet
   calls object_loop_reconcile with that packet and the freshly read version.
   A refused continuation records `checkpoint continuation refused by limits`.
   Otherwise leave the checkpoint held for an operator variation-suite cook or
   a new evidence revision. Human review consumes no worker seat.
5. For a completed scorer, read its latest task note beginning the exact prefix
   `object-score:1` followed by a newline and a JSON ObjectScoreRecordArgs object.
   The whole packet is typed and scope-checked; no free-text extraction is used.
   Call object_score_record with those fields. It validates all receipts, ranks
   eligible captures, keeps the incumbent on a tie, counts invalid-capture repair
   rounds and reserves a finite next wave atomically. If a continue request is
   refused (including plateau, repair, round-cap or aggregate budget limits),
   retry the same score once with action stop and stop_reason
   `continuation refused by score or budget contract; retained verified result`.
   This fallback still validates the evidence, so invalid scores fail closed.
   All other refusals fail the run. Reconcile a successful score or stop.
6. With no score/checkpoint action, call object_loop_reconcile and finish this
   item. Waiting outcomes create no duplicate tasks. The next event/timer reads
   the state again. Command runtime errors retry at most twice with one-second
   backoff; rejected operations are never blindly retried.

## Bindings and command inputs

The sweep binds `inputs`, `start`, `started`, `object` and `checkpoint`.
Command names are `object_loop_inputs`, `object_loop_start`,
`object_loop_reconcile`, `object_checkpoint_read` and `object_score_record`.
The read uses `project_id` and `limit`. Start forwards `epic_task_id`,
`object_id`, `attempt_id`, `incumbent_sha256`, `incumbent_artifact`,
`reference_sha256`, `rig_sha256`, `scorer_sha256`, `render_profile_sha256`,
`policy_sha256`, `brief_review_id`, `brief_review_revision`,
`brief_review_sha256`, `mandatory_views`, `limits`, `final_reserve`,
`score_reservation`, `noise_band`, `max_rounds`, `max_repair_rounds`,
`max_plateau_rounds` and `variants`. Score forwards `expected_version`, `score_task_id`,
`receipts`, `spent`, `action`, `next_variants`, `stop_reason`, `review_id`,
`review_revision` and `review_sha256` plus project/object identity.
All commands route `completed`, `rejected` and `runtime_error` explicitly;
foreach collection failures end `failed`.

## Formula and worker handoff

Cook the packaged object formula once at the root with immutable inputs and the
approved brief. Its bootstrap has an event gate installed inside graph creation.
Candidate workers use immutable input artifacts and write only generators,
materials and presets in their own workspace. They publish evaluation bundles
to the artifact store, not the product branch. Scoring workers independently
validate complete mandatory views and record the strict score packet with
`aq task set HELD_TASK --note PACKET` before closing. Include next_variants in a
checkpoint packet only for an approved continuation; every variant reservation
includes its allowed retries. Missing or unknown costs charge the full reserve.

For variation, create a suite container directly under the epic, then cook
variation with that suite as --parent. The transaction checks the exact current
approved candidate, marks every task artifact-only, attaches the checkpoint gate
and refuses duplicate suite cooking. It creates ten-seed and boundary/reload
leaves, never a fourth hierarchy level or ancestor dependency. The existing
final-suite reserve covers these checks. Install the suite before recording stop.
The finalizer waits for candidate/scorer/variation settlement and verifies retained
artifacts, review state, failures and the stop reason. Task completion never
authorizes product integration: a separate code task imports only an accepted
generator through the project's integration owner.

## Failure handling, uniformly

Each sweep and foreach body terminates completed or failed. There is no
unbounded loop, AI step or human-decision command. One item's failure ends the sweep visibly; the next event or timer retries
from persisted state.
Failed children retain their status. Finalization can report defects after a
rejection, but neither it nor this policy can force a passing epic.
