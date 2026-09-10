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
---

# Default Pipeline

The system default pipeline handles approved specs and task-batch proposals.
Integration provides code validation and delivery; this pipeline does not create
per-task reviewers, final branch reviewers, or review/PR gates on downstream work.

Ships three rules:

- **Spec ingest** (`spec.approved`) creates a spec-ingest task.
- **Proposal gate** (`proposal.ready`) requests approval for a task batch.
- **Proposal commit** (`gate.resolved`, human gate) commits the approved batch.

## Rule: spec-ingest-on-approve

Trigger: `spec.approved`. No guard.

1. Call `ensure_task` in the event's `project_id` with `dedup_key`
   `spec-ingest:{event.spec_path}` (the `spec_path` the event carries), `title`
   `Ingest spec {event.spec_path}`,
   and `profile_id` `spec-ingest`. The `description` instructs the agent to read
   the spec, list the project's existing tasks, emit `task_batch_propose` with
   the derived task graph, and iterate on validation errors. The dedup key is
   what makes this exactly one ingest task per approved spec file.

## Rule: proposal-ready-gate

Trigger: `proposal.ready`. No guard.

1. Call `gate_create` in the event's `project_id` with `gate_type` `human`,
   `title` `Approve task batch?`, `question`
   `Approve proposal {event.proposal_id}?`, and `await_id` pinned to
   `event.proposal_id` so the resolution can be matched back to the proposal it
   approves.

## Rule: commit-on-gate-resolve

Trigger: `gate.resolved`, filtered by the playbook's trigger to
`gate_type: human`.

1. Call `task_batch_commit` with `proposal_id` taken from the resolved gate's
   `await_id`, writing the approved batch into the task graph. The `await_id`
   pinned by `proposal-ready-gate` is the only thing that connects the two
   rules; nothing else carries the proposal identity across the human decision.

## Failure handling

Each rule ends at its terminal step after success or failure. A failed rule
leaves already completed actions in place.
