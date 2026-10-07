---
id: default-pipeline
kind: pipeline
role: default-pipeline
scope: system
triggers:
  - spec.approved
  - proposal.ready
  - event_type: gate.resolved
    filter:
      gate_type: human
      resolution: [approve, approved]
---

# Default Pipeline

The system default pipeline handles approved specs and task-batch proposals.
Integration provides code validation and delivery; this pipeline does not create
per-task reviewers, final branch reviewers, or review/PR gates on downstream work.

Ships three rules:

- **Spec ingest** (`spec.approved`) creates a spec-ingest task.
- **Proposal gate** (`proposal.ready`) requests approval for a task batch.
- **Proposal commit** (`gate.resolved`, approved human gate) commits the approved batch.

## Rule: spec-ingest-on-approve

Trigger: `spec.approved`. No guard.

1. Call `ensure_task` in the event's `project_id` with `dedup_key`
   `spec-ingest:{event.spec_path}` (the `spec_path` the event carries), `title`
   `Ingest spec {event.spec_path}`,
   `profile_id` `spec-ingest`, and `intelligence_class` `deep-high`.
   The `description` names the absolute `spec_path` and instructs the agent to
   classify `spec_kind` and check implementation grounding. Design specs get
   one deep-high implementation-spec authoring child in an epic, submitted
   to Jack's review queue. Implementation specs get parallel phase epics
   and self-contained children; validate, then `task_batch_propose` followed
   directly by `task_batch_commit`, without a human proposal gate. The dedup key is
   what makes this exactly one ingest task per approved spec file. The
   `spec-ingest` profile makes this a role task: `ensure_task` creates it with
   `route_source` `role` and the role profile's `default_class` as its execution
   class. The supplied `deep-high` is recorded as a class hint, not the
   route. A role task needs no routing decision even though `ensure_task`
   suppresses `task.created`.
   `created` and `reused` end the rule `completed`.

## Rule: proposal-ready-gate

Trigger: `proposal.ready`. No guard.

Only ordinary proposals emit this event. A live spec-ingest assignment's
approved-spec batch receives a server-owned stamp and emits no `proposal.ready`;
it commits directly after validation. A caller-supplied `source: spec:...`
never grants this exception.

1. Call `gate_create` in the event's `project_id` with `gate_type` `human`,
   `title` `Approve task batch?`, `question`
   `Approve proposal {event.proposal_id}?`, and `await_id` pinned to
   `event.proposal_id` so the resolution can be matched back to the proposal it
   approves. `created`, `reused` and `skipped` end the rule `completed`.

## Rule: commit-on-gate-resolve

Trigger: `gate.resolved`, filtered by the playbook's trigger to
`gate_type: human` AND a `resolution` of `approve` or `approved` (the values the
dashboard's approve actions write). A rejected, expired, or otherwise resolved
gate never dispatches this rule, so it cannot commit a proposal.

Guard: the event carries a nonempty `await_id`; missing proposal identity ends
the rule without invoking a commit.

1. Call `task_batch_commit` with `proposal_id` taken from the resolved gate's
   `await_id`, `gate_id` the event's `gate_id`, and `project_id` the event's
   `project_id`, writing the approved batch into the task graph. The `await_id`
   pinned by `proposal-ready-gate` is the only thing that connects the two
   rules; nothing else carries the proposal identity across the human decision.
   `committed` ends the rule `completed`, and so does `already_committed`: a
   replayed approval gets the original receipt and no second graph.

## Failure handling

Successful command outcomes end at a `completed` terminal. `not_approved`,
`rejected` and `runtime_error` end at a distinct `failed` terminal; they must
never report completion. There are no automatic retries in these rules.
Previously completed actions remain in place, and command deduplication
protects deliberate replays.

Event filtering is not a substitute for authorization. For ordinary proposals,
`task_batch_commit` re-reads the named gate and answers `not_approved` unless it is a
resolved `human` gate in the proposal's own project, awaiting that proposal,
with an approval `resolution`; a direct call without `gate_id` is held to the
newest such gate. A proposal's payload is frozen once its approval gate exists,
so the decision covers exactly the revision that is committed.


Spec-ingest batches carry server-derived approved-document provenance and
bypass the human approval check. They are committed with their epics, children,
routing gates and edges in one database transaction on every project. The
exception is created only for a live assigned spec-ingest role, never from
an arbitrary caller's source or payload. They only create work: edits, edge
removals and comments on existing tasks need an ordinary, human-approved change set.
