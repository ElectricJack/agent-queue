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
artifact retention, approved brief, calibrated reference manifests or an explicit
supervisor reference_kind=self start packet, finite job/GPU lease and budget
reserves before activation. Self-reference results are indicative and prove
plumbing only; all loop bounds and publication refusals still apply.
Every start requires incumbent_capture_sha256 from the retained baseline capture
receipt. For reference_kind=self, reference_sha256 must equal that capture hash;
for calibrated references it remains independent. incumbent_sha256 continues
to identify the candidate manifest.
This bundle never activates itself,
approves a review, chooses a provider or publishes a generator.

Every rule performs the same bounded sweep for matter-engine-cpp. Events are
wake hints only; object_loop_inputs reads persisted formula, task, score and
review data. The timer recovers missed events and restarts. At most 32 pending
or active objects are read; exceeding this bound fails visibly. Operate the
pilot with at most two object epics and one GPU lease. A loop is processed only
when its policy hash equals this running artifact's hash. An operator must
explicitly migrate a loop to a different reviewed policy; drift cannot adopt it.

One attempt, one editor build. Render the baseline capture and every later
capture of the attempt with `aq job submit --attempt-id ATTEMPT`, the same
attempt id the start packet carries. AQ copies that editor build once and
launches the pinned copy for every capture of the attempt, so rebuilding the
shared build cannot move the preset under a running attempt; a different
attempt id is how the build is allowed to move, and a start packet's
render_profile_sha256 is only valid for the attempt that pinned it.

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

Use the built-in bounded `filter` expression before each `foreach`; preserve
input order. Read once with `object_loop_inputs`, `project_id` =
`matter-engine-cpp` and `limit` = `32`, saving `inputs`. Both loops have
`max_iterations` = `32` and `failure_policy` = `halt`. The producer enforces
its bound before filtering, including foreign-policy objects.

* Filter `inputs.starts`, lexically binding `start`: `proposal_approved` must
  be truthy AND `policy_artifact` must equal the context `artifact_sha256`.
  Forward the whole typed `request` to `object_loop_start`, saving `started`;
  reconcile `started.object_id` with `object_loop_reconcile`. Missing or
  withdrawn approval leaves the bootstrap held. The bootstrap releases only
  after the finalizer hold commits.
* Filter `inputs.loops`, lexically binding `object`: `policy_artifact` must
  equal the context `artifact_sha256`. Apply the following ordered decision
  table to each eligible item. First match wins; continue to the next item
  after its action. All identities use `project_id` = `matter-engine-cpp`
  and the item's `object_id`.

| Priority | Predicate | Action |
|---|---|---|
| 1 | `state.status` `eq` `stopped` | Reconcile settlement and finalizer release. |
| 2 | `elapsed_seconds` `gte` `86400` | Reconcile with `expected_version` from `version` and stop reason `object wall deadline reached`. |
| 3 | `scorer_failed` truthy | Record a defect stop for `state.score_task_id` at `version`, with no receipts and reason `scorer exhausted; retained incumbent; quality unavailable`; reconcile. |
| 4 | `state.checkpoint` truthy | Read and handle the exact checkpoint below. |
| 5 | `score` truthy | Record and handle the typed score below. |
| Otherwise | No matching action | Reconcile only; unsettled work and retries remain held. |

Checkpoint: call `object_checkpoint_read`, saving `checkpoint`. If the item's
`review_state` `in` `["rejected", "withdrawn", "changes_requested"]`, stop at
its `version` with `checkpoint not accepted; evidence retained`. Otherwise,
only when `checkpoint.approved` and the item's `next_variants` are truthy,
reconcile that packet at the freshly read `checkpoint.version`. A rejection
of that continuation reconciles a stop at that same version with
`checkpoint continuation refused by limits`. Otherwise leave the checkpoint
held for the supervisor's variation suite or new evidence revision. Neither
review events nor a wait outcome grant approval or resolve this review gate.

Score: the read bridge accepts only the completed current scorer's latest
note with exact prefix `object-score:1` plus newline and a complete JSON
ObjectScoreRecordArgs. Forward `score` to `object_score_record`; reconcile
on success. On rejection, only if `score.action` `eq` `continue`, retry that
same packet once with action `stop`, empty `next_variants`, no review fields,
and reason `continuation refused by score or budget contract; retained verified result`.
This validates the evidence again; all other rejections fail the sweep.

The commands own receipt validation, ranking, tie retention, repair/plateau
caps and atomic reservations. Forward the approved `limits` unchanged: the
$100 operating cap belongs in the immutable start packet's `limits.usd`,
including retries, scoring and final-suite reserves. Do not replace it with a
per-round allowance or enlarge it during continuation. Forward `max_rounds`
unchanged (including a three-round pilot); do not derive rounds from calls.
No script, model call, provider substitution or extra retry task is introduced.

Every command uses `max_attempts` = `2`, `backoff_seconds` = `1`, and retries
only `runtime_error`. Explicit outcomes are `completed`, `rejected` and
`runtime_error`; only the two continuation fallbacks above handle rejection.
All other command errors and collection failures terminate `failed`.
After each sweep terminate `completed`. The five events and recovery timer
re-read durable state; no suspended run replaces their recovery behavior.

## Bindings and command inputs

`inputs` exposes `starts` and `loops`. Filter bindings are lexical; foreach
binds the selected row as `start` or `object`. `started` exposes `object_id`.
`object` exposes `object_id`, `policy_artifact`, `state.status`,
`elapsed_seconds`, `scorer_failed`, `state.score_task_id`, `state.checkpoint`,
`version`, `review_state`, `next_variants` and `score`. `checkpoint` exposes
`approved` and `version`. `start` exposes `proposal_approved`, `policy_artifact`
and `request`. These typed reads, not event payloads or prose extraction,
determine every command input.

Forward these exact start paths unchanged:

* `project_id` ← `request.project_id`.
* `epic_task_id` ← `request.epic_task_id`.
* `object_id` ← `request.object_id`.
* `attempt_id` ← `request.attempt_id`.
* `incumbent_sha256` ← `request.incumbent_sha256`.
* `incumbent_artifact` ← `request.incumbent_artifact`.
* `incumbent_capture_sha256` ← `request.incumbent_capture_sha256`.
* `reference_kind` ← `request.reference_kind`.
* `reference_sha256` ← `request.reference_sha256`.
* `rig_sha256` ← `request.rig_sha256`.
* `scorer_sha256` ← `request.scorer_sha256`.
* `render_profile_sha256` ← `request.render_profile_sha256`.
* `policy_sha256` ← `request.policy_sha256`.
* `brief_review_id` ← `request.brief_review_id`.
* `brief_review_revision` ← `request.brief_review_revision`.
* `brief_review_sha256` ← `request.brief_review_sha256`.
* `mandatory_views` ← `request.mandatory_views`.
* `limits` ← `request.limits`.
* `final_reserve` ← `request.final_reserve`.
* `score_reservation` ← `request.score_reservation`.
* `noise_band` ← `request.noise_band`.
* `max_rounds` ← `request.max_rounds`.
* `max_repair_rounds` ← `request.max_repair_rounds`.
* `max_plateau_rounds` ← `request.max_plateau_rounds`.
* `variants` ← `request.variants`.

Forward these exact score paths unchanged (except the explicit stop fallback):

* `object_id` ← `score.object_id`.
* `project_id` ← `score.project_id`.
* `expected_version` ← `score.expected_version`.
* `score_task_id` ← `score.score_task_id`.
* `receipts` ← `score.receipts`.
* `spent` ← `score.spent`.
* `action` ← `score.action`.
* `next_variants` ← `score.next_variants`.
* `stop_reason` ← `score.stop_reason`.
* `review_id` ← `score.review_id`.
* `review_revision` ← `score.review_revision`.
* `review_sha256` ← `score.review_sha256`.

## Formula and worker handoff

Jack sees two documents per attempt. Before cooking, submit one brief in plain
English: the object being built, the maximum number of rounds and the budget.
At the end, the held finalizer submits one result with before/after images per
view, whether the object improved and by how much in ordinary words, and what
would help the next run. Use the object's ordinary name. Do not put attempt IDs,
round IDs, receipts, incumbent or plateau in either document.

Every per-round score, probe, capture, variation and continuation decision is
internal. The loop decides continue/stop within the approved bounds; a
checkpoint review must have decider supervisor. Marked experiment tasks and
their probe children have no implicit human-review deliverable. Any explicit
internal review routes to the supervisor and stays out of Jack's Reviews,
Discord notifications and digest requests. Only the finalizer's result routes
to user, and repeat submission reuses its document. Do not submit a separate
human score, probe, capture or plateau review.

Blocked work sends detailed evidence to the project supervisor through task
notes and the normal recovery incident. The supervisor handles recovery. Send
Jack one plain-English sentence only when he must act, naming what he needs to
do; ordinary capture failures and limit stops need no human decision.

Cook the packaged object formula once at the root with immutable inputs and the
approved brief. Its bootstrap has an event gate installed inside graph creation.
Candidate workers use immutable input artifacts and write only generators,
materials and presets in their own workspace. They publish evaluation bundles
to the artifact store, not the product branch. Every candidate and scoring
packet carries a round_handoff block: the incumbent bundle artifact, each
earlier round's branch and commit with the bundle it retained, and the frozen
comparison inputs. Reading those retained artifacts and those earlier round
branches is in scope for a round worker; a worker that starts from the
reference instead of from the incumbent cannot be scored against it, because
the attempt's render profile and rig are fixed. Every capture of the attempt is
submitted under the same attempt id, so the pinned editor build is the one the
profile names. Scoring workers independently validate complete mandatory views,
score every wave member against that same frozen profile so candidate and
reference are comparable, and record the strict score packet with
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
