---
spec_kind: implementation
status: partially implemented
---

# Approved document ingestion

This page describes the mechanisms present at `52d2efe4c` (2026-10-22).
Implementation graphs are held; release is not yet wired end to end. See the
[system spec](../systems/reviews-and-knowledge.md#5-known-gaps) for the remaining
integration gaps and [reviews](../../guides/reviews.md) for operator decisions.

## Approval authority

Approval of review kind `spec` or `plan` freezes a decision packet with an
immutable `decision_id` and `packet_sha256`. The asks operation dispatches
`spec.approved`; the default pipeline calls `ensure_spec_ingest`, deduplicated
on `spec-ingest:decision:<id>`. Other document kinds and stale or rejected
decisions do not ingest. Standalone `spec-approve` snapshots use the same
immutable-packet ingestion path. A vault path is a mirror link, not authority.

Replaying an approval returns the original task receipt, including after task
completion, obsolete closure or archive. The graph receipt similarly prevents
recreation of existing or archived work. A new approved decision is distinct.
See [work-graph §15](../design/work-graph.md#15-immutable-approval-ingestion-2026-10-11).

The ingest role must be a live `spec-ingest` assignment holding the task stamped
with that decision. Its proposal source must be `spec:decision:<id>` for the
same project and approved packet. Caller-chosen source text cannot confer this
authority. Generic pool workers cannot use the role's proposal path.
The role launches on its enabled global agent definition, preserving its saved
name, id and execution overrides. Busy or disabled definitions do not create
replacement agents, and the ingestion agent cannot take ordinary tasks.
Its shipped grants include `knowledge_show`, `record_search` and
`record_capabilities` for scoped discovery; core knowledge activation is still
independent. Existing installs receive missing grants through the operator's
`aq agent profile-reseed --profile-id spec-ingest --grants-only`, preserving
local Role/Rules and harness choices.

## Design and implementation graphs

`spec_kind: design | implementation` is retained on each review revision and
survives vault regeneration. The approved-packet reader defaults an omitted
classification to `design`; the agent does not choose the fallback.

For a design packet, the server replaces the submitted graph with its own
shape: one epic containing one deep-high design task to write a code-grounded
implementation spec and submit it for review. That batch is released as design
authoring work. It does not authorize implementation work.

For an implementation packet, `task_batch_propose` atomically creates a **held**
batch: proposal, tasks, membership, edges, routing gates, provenance, digest,
revision and outbox events. The proposal status is `held`. Root tasks must be
epics or permitted operational roots, with the required `delivery-of` linkage.
Dependency edges connect leaves; structural parent-child edges are separate.
Validation rejects invalid roots, multiple parents, container dependencies and
cycles. Ingest authority permits new tasks and edges only; edits, comments and
edge removals on existing work require an ordinary approved change set.

Held members exist in the database but are excluded from admission through
`not_ingest_held`. Graph creation is not permission to claim, route or deliver
them. Replays return the original receipt. `task_batch_commit` refuses a held
proposal with `fidelity_review_required`; it cannot release the graph.

## Fidelity review and the current release boundary

`ensure_fidelity_review` can file a reviewer task for a held batch, with bounded
dispatch/retry handling and an ask on exhaustion. Scoped `task_batch_update`
edits advance its revision and digest. `task_batch_review_record` records a
receipt against those exact values. A receipt does not itself release work.

At this revision, the database release primitive exists, but there is no
`task_batch_release` command, shipped `fidelity-reviewer` profile, or default
pipeline consumer for `batch.held` / `batch.receipt_recorded`. An implementation
batch therefore remains held. Reconciliation re-emits work; it does not grant
release authority. The shipped spec-ingest profile's immediate-commit wording
and commit grant also lag this mechanism. Do not clear holds or invoke database
release directly to work around these gaps.

Ordinary proposals retain their separate staged flow: a `ready` proposal emits
`proposal.ready`, a human gate authorizes `task_batch_commit`, and that commit
applies the change set. The role-authorized ingest path emits no such proposal
gate. `aq task create --from-spec ... --after-review ...` is a separate manual
graph-creation path: its review gate does not provide held-batch fidelity review.

## Source and verification

- [Approved packets](../../../src/asks/ingestion.py) and
  [ingest task creation](../../../src/commands/spec_ingest_commands.py).
- [Atomic proposals](../../../src/commands/proposal_commands.py),
  [fidelity review](../../../src/commands/fidelity_review_commands.py),
  [hold predicate](../../../src/database/queries/ingest_hold.py) and
  [batch persistence](../../../src/database/queries/ingest_batch_queries.py).
- [Reviewed default pipeline](../../../src/prompts/reviewed_playbooks/default-pipeline/source.md)
  and [shipped ingest profile](../../../src/profiles/defaults/spec-ingest/profile.md).

The operator owns schema upgrades and reconciliation of installed profiles and
reviewed policy. Shipped profiles are write-if-absent; grants-only reseeding does
not refresh their Role or Rules. Workers ship source changes and preserve the
installed harness. This page records no rollout or production release evidence.
