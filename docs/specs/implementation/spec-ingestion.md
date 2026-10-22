---
spec_kind: implementation
status: draft
---

# Approved document ingestion

Successful approval of kind `spec` or `plan` dispatches `spec.approved` with
an immutable `decision_id` and `packet_sha256`. The absolute vault path is a
mirror link, not approval authority. Other documents and rejected or stale
decisions do not ingest. The default pipeline calls `ensure_spec_ingest`,
deduplicates on `spec-ingest:decision:<id>`, and runs the role at deep-high.
Replayed approvals return the original transactional task receipt after the
task completes, is closed as obsolete, or is archived. Settling a task does
not authorize replacement ingestion. The graph's batch receipt likewise
prevents replay from recreating existing or archived work. A new approved
decision is distinct; a path or textual graph reference alone cannot establish
its ingestion authority. See [work-graph §15](../design/work-graph.md#15-immutable-approval-ingestion-2026-10-11).

The role launches on the enabled global `spec-ingest` agent definition, retaining
its saved name, id and execution overrides. Generic pool definitions cannot take
this assignment, the ingestion agent cannot take ordinary implementation tasks,
and busy or disabled role definitions do not create duplicates.
The shipped profile grants `knowledge_show` and `record_search` for prime's
project knowledge context and scoped discovery, plus `record_capabilities` for
inspecting available record operations. Existing installs receive these grants through the
operator's `aq agent profile-reseed --profile-id spec-ingest --grants-only`;
the shipped file remains write-if-absent.

Submitted `spec_kind: design | implementation` belongs to each immutable
review revision and survives vault regeneration. Missing values remain unset
for legacy documents; the ingest agent reads the content before classifying.
Routing consumes type/class hints, never profiles.

The design behind this contract is the approved vault spec
`2026-10-13-spec-ingestion-with-task-structure-review-before-release.md`
(`rev-bright-meadow` revision 2); its implementation spec is
`2026-10-14-spec-ingestion-held-batches-and-fidelity-review-before-relea.md`
(`rev-agile-beacon` revision 1). Section numbers below cite the design.

## What ingestion creates (design §3)

**Design documents** produce one epic containing one deep-high leaf that writes
a code-grounded implementation spec and submits it to Jack's review with
`spec_kind: implementation`. The server authors this two-node shape; the
leaf's brief carries the design's goals, decisions and non-goals. Its batch is
created `released` with the server receipt `design_authoring_shape`
(release actor `service:design_authoring_shape`). No fidelity reviewer runs,
and design approval creates no implementation work.

**Implementation documents** produce delivery epics and self-contained leaves.
A delivery epic contains only the finite work that produces and verifies its
landable artifact: implementation, tests, necessary documentation and packaging.
Each leaf declares `section_anchor`, `primary_outcome`, `acceptance`,
`constraints`, `owned_modules` and a rough `size` (a large leaf explains why).
Every node maps to the approved document identity, revision, body hash and
section; coverage runs both ways, so every acceptance requirement maps to a
leaf, proven existing work, or a scope question, and every leaf traces to a
source statement.

Structural `parent-child` edges express membership only. Execution edges
(`blocks`, `conditional-blocks`, `waits-for`) connect concrete leaves, never a
container at either end. Each dependency states what result the dependent
consumes; shared files alone are an advisory finding, not a chain.

**Operational roots** are the one exception to "every root is an epic".
Deploying to an operator environment, a production cutover, post-landing
observation and elapsed-time soak periods are separate root tasks in the same
batch, never children of a delivery epic, which would then wait on them
forever. A proposal marks such a task `placement: operational_root` with
`delivery_targets`, the code leaves it needs landed. The server records
`delivery_condition = {"kind": "delivery-of", "targets": [...]}` in task
metadata and adds one `delivery-of` edge per target
([work-graph §3.1](../design/work-graph.md#31-blocking-edge-types)). The edge
stays unmet until a durable landing receipt exists; unknown or failed delivery
keeps the root blocked. It never infers landing from epic completion or a
sleep, and a code leaf cannot depend on an operation that needs that leaf to
land first (`delivery_condition_cycle`).

Ingest authority only creates work. A change set carrying edits, edge removals
or comments on existing tasks is refused; such updates go to the supervisor as
an ordinary, human-approved change set.

## The held batch (design §4)

`task_batch_propose` from a live spec-ingest role assignment, with source
`spec:decision:<id>` for the held task's server-stamped decision and immutable
approved packet, materializes the graph **held** in one transaction. The
proposal row is the batch (`proposal_id = batch_id`); `ingest_batches`,
`ingest_batch_members` and `ingest_batch_receipts` record its state, member map,
revision, digest, design lineage and receipts. Every epic, leaf, operational
root, routing gate, provenance row and edge is a real, visible task row from
that transaction, each carrying `tasks.held_batch_id`. The call returns
`status: held`, `batch_id`, `members`, `structure_revision`, `structure_digest`
and `lineage` (the originating design review, or `design lineage: none` for a
legacy document). The ingestion role records that receipt and closes; it never
commits. Only an authenticated live spec-ingest role assignment may produce such
a proposal: the server stamps the authority, and caller-chosen `source` text
cannot bypass ordinary proposal approval. These proposals emit no
`proposal.ready`, so the human proposal gate does not run; the machine release
below is the only step that exposes the graph.

A hold is independent of status, routing, pauses and ordinary gates.
`not_ingest_held()` (`src/database/queries/ingest_hold.py`) is the single
predicate, applied to the claim frontier, pool demand, explicit claims, claim
activation after preparation, push scheduling, launch, restart, container
settlement, the integration train, the delivery observer and the blocked-state
projection. A held task cannot launch, reserve a workspace or start publication;
a held container never settles; resolving a routing gate or resuming a task does
not release it. `aq task explain` names the reason ("member of ingestion batch
<id> awaiting fidelity review"). Moving a held member never sheds its hold, and
leaves added during review join the batch held.

Validation failure before creation still leaves nothing. A reviewer failure
leaves a durable held batch. `task_batch_commit` on a held batch answers
`fidelity_review_required`, `task_batch_discard` answers
`batch_retire_required`, and a commit on a released batch replays its receipt.

## Automatic checks, then one reviewer (design §5)

`task_batch_validate` (`aq batch validate`) runs `src/task_graph/batch_checks.py`
over the batch and the live graph around it. Propose, every held edit and
release run the same checks. It makes no LLM call and reads no meaning from
words.

| Hard (refuses) | Retryable hard | Advisory (for the reviewer) |
|---|---|---|
| `authority`, `membership`, `parent_not_unique`, `cross_project`, `depth_exceeded`, `cycle`, `container_endpoint`, `external_target_invalid`, `dependency_on_ancestor`, `post_landing_inside_delivery_epic`, `delivery_condition_cycle`, `section_anchor_missing`, `primary_outcome_missing`, `acceptance_missing`, `profile_pinned` | `stale_revision`, `active_claim`, `integration_owned` | `large_leaf`, `mixed_outcomes`, `broad_command`, `duplicate_title`, `redundant_edge`, `unexplained_serialization`, `concentrated_ownership` |

`batch.held` drives the default pipeline's `fidelity-review-on-held` rule,
which calls `ensure_fidelity_review`. That files one root task for the
`fidelity-reviewer` profile (deep-high), deduplicated on
`fidelity-review:batch:<batch_id>`, outside the held graph with informational
`related` links to it. The task retries at most twice, and the batch is
dispatched at most three times; when that budget is spent
(`fidelity_review_exhausted`) the batch stays held and one ask is raised.
Batch-review authority reaches only the live assigned reviewer; reassignment
revokes the previous holder's authority in the same transaction.

The reviewer judges fidelity first and structure second. It edits only that
batch's unclaimed members through the held branch of `task_batch_update`
(`aq batch update`), which requires `expected_revision` and a list of
`operations`: `rewrite`, `split`, `combine`, `reparent`, `rewire`, `add_leaf`,
`move_to_root` (with `delivery_targets`) and `retire`. Each edit is validated
against the combined graph, bumps the structure revision and recomputes the
digest. Titles are excluded from the digest, so a retitle keeps a receipt
valid. The reviewer cannot edit external prerequisites, widen its own scope or
change the approved document.

`task_batch_review_record` (`aq batch review-record`) writes the receipt, fenced
by `expected_revision` and `expected_digest`. It holds one verdict per mapped
section (`faithful`, `fixed` or `scope_question`), a reverse-coverage verdict,
a walkthrough verdict and the structural findings. A passing receipt moves the
batch to `reviewing` and queues `batch.receipt_recorded`. A `scope_question`
moves it to `needs_scope`, queues `batch.needs_scope` and raises exactly one ask
through `src/asks` (`ask_kind: batch_scope`, key
`batch-scope:<batch_id>:<revision>`). An answer is evidence for another edit and
attestation round: it returns the batch to held for a fresh review and never
releases it.

## Atomic release (design §6)

`batch.receipt_recorded` drives `release-on-receipt`, which calls
`task_batch_release` (`aq batch release`) with the event's `batch_id`,
`expected_revision` and `expected_digest`. Only ingestion policy (a playbook
or service principal), the project supervisor or the operator may release;
the reviewer never can. In one transaction the server re-reads the batch, the
approved source, the live graph and the current passing receipt, re-runs the
hard checks, then clears every member's `held_batch_id`, marks the proposal
`committed`, stores the release actor, revision, digest and membership receipt,
and emits `batch.released`. Mandatory routing and dependency promotion then
determine the initial READY frontier. `already_released` replays the stored
receipt. `needs_scope` ends the rule completed without release; every other
refusal (`source_diverged`, `batch_not_held`, `membership`, a stale fence or
hard finding) leaves the batch held. A diverged or unapproved source cannot
release.

`batch_reconcile` runs on the daemon cycle under a service principal. It
re-dispatches a held batch's missing reviewer and re-emits
`batch.receipt_recorded` for an unreleased passing receipt. Recovery never
releases by itself.

`task_batch_retire` (`aq batch retire`, ingestion policy, supervisor or
operator) finalizes an unreleased batch whose members were already retired
through the supported obsolete lifecycle. It proves that retirement, marks the
batch `retired` and the proposal `discarded`, and never retires live work or
clears a hold. `aq batch show --project <id> <batch_id>` (REST
`GET /api/projects/{project_id}/batches/{batch_id}`) reads the batch, its
members, checks and receipts.

## Editing after release (design §7)

Release freezes the reviewed snapshot for audit, not the live structure. The
reviewer's batch authority ends at release. The project supervisor repairs
in-scope structure through ordinary commands, each with a reason:

- **Queued, unclaimed, no materialized branch:** `aq task reparent --task-id
  <id> --root` (or `--parent-id`) moves it in one transaction, including in
  hierarchical projects; a materialized origin under a different checkpoint
  is refused with `delivery_target_fixed`.
- **No longer required or duplicate:** `aq task archive --disposition obsolete`
  with `--replacement <leaf>`, `--satisfied-by <evidence>` or a `--rewire` edge
  plan. A retired task shows retired, never successful implementation.
- **Already landed:** delivery proof stays attached to its original source;
  remaining work is a new explicit leaf.

Not yet built: the supported split of a started mixed task into retained code
and a root operational task, and a `needs_quiescence` answer for running or
integration-owned work. Until they ship, such a repair waits for the work to
stop and goes to the supervisor. New scope from a newly approved spec enters a
new held batch with its own review.

Ordinary proposals keep the staged flow: `task_batch_propose` persists a
`ready` proposal, `proposal.ready` requests approval, and the human gate's
resolution is what `task_batch_commit` accepts as authority.

## Acceptance

Review approval and dedup through the recorded default pipeline; revision
classification round trips and vault recovery; a design proposal through a live
role principal is released with `design_authoring_shape`; an implementation
proposal produces visible, held, unclaimable tasks with no commit call, and
replay returns the same receipt; held tasks add no pool demand and survive
restart, routing-gate resolution and resume; the reviewer's receipt releases the
batch atomically, and a scope question raises one ask that never releases;
operational roots stay blocked until their `delivery-of` targets land; ordinary
proposals are still staged and gated; fault injection and a second database
connection prove rollback and absence of partial graph visibility. Ratchets:
`tests/test_ingest_hold_admission.py` and the batch, fidelity-review and
pipeline suites named in the implementation spec.

## Rollout

Revision `a00000000081` added nullable revision classification. Revision
`a00000000100` adds the three batch tables, `tasks.held_batch_id` and the
`held` proposal status; its downgrade refuses while any batch is `held`,
`reviewing` or `needs_scope`. Shipped profiles are write-if-absent, and every
step below is operator work:

1. `aq db upgrade` to `a00000000100`, then `aq restart --no-dashboard`.
2. `aq agent profile-reseed --profile-id spec-ingest --grants-only`, preserving
   the installed harness, and seed `fidelity-reviewer` with
   `aq agent profile-reseed --profile-id fidelity-reviewer`.
3. `aq playbook activate default-pipeline` for the rebuilt reviewed bundle.
4. `aq doctor --check ingestion.legacy_proposals` lists uncommitted `spec:`
   proposals from before holds existed; `--fix` adopts provable ones as held
   batches and `--fix --leave-gated` keeps them on their human gate.
5. Observe for one week with `aq doctor --check ingestion.held_batches`, which
   reports held batches, missing or exhausted reviewers, stale receipts and
   remaining legacy proposals.

These steps are filed as separate root operational tasks, never as children of
a delivery epic, and no worker performs them: a worker never migrates, restarts
the daemon, reseeds a profile or activates a bundle.
