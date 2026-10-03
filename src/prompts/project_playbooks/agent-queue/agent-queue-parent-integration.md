---
id: agent-queue-parent-integration
name: Agent Queue parent integration
version: 1
scope: project:agent-queue
enabled: false
triggers:
  - task.completed
  - task.failed
  - task.child_added
  - task.parent_checkpointed
  - delivery.ready
  - delivery.applied
  - task.integration_ready
  - task.integration_verified
  - integration.ci_completed
  - integration.repair_exhausted
  - integration.repair_deadline_due
  - integration.resolution_push_observed
  - integration.repair_delegate_closed
---

# Agent Queue parent integration

This project-scoped policy connects durable hierarchy lifecycle facts to deterministic
integration commands. It never treats a repair-task close or a resolution push
observation as delivery evidence, and it never supplies resolution Git object IDs.

## Rules: child-terminal-readiness

On `task.completed` or `task.failed` for a hydrated task whose
`task.parent_task_id` is present, call `integration_delivery_readiness` with that
immediate parent as `task_id` and bind `readiness`. Outcomes `ready` and `waiting`
complete. On `failed`, inspect `on_failed_child`: `block` terminates the run as blocked while
`ask` calls `gate_create` with stable failed-parent `await_id`, the event `project_id`,
literal `gate_type` `human`, failed-child `title` and `question`, and the parent in
`waiter_task_ids`. Gate outcomes `created`, `reused`, and `skipped` complete;
`rejected`, `invariant_error`, and runtime failures fail. These are two artifact rules,
`completed-child-readiness` and `failed-child-readiness`, because each trigger is exact.

## Rule: file-children

On `task.child_added`, call `integration_file_children` with `parent_id`, `children`,
and `expected_generation`. Outcomes `filed`, `stale_parent`, and `invalid` are
terminal; runtime failure is terminal failure.

## Rule: checkpoint-parent

On `task.parent_checkpointed`, call `integration_checkpoint_parent` with `task_id`,
`head_sha`, and `generation`. Outcomes `checkpointed` and `already_waiting` complete;
`dirty` and `stale` fail.

## Rule: promote-delivery

On `delivery.ready`, call `delivery_promote` with `operation_key`, `source_task_id`,
`source_head`, `source_base`, `expected_target`, and `fence`. Outcomes `promoted` and
`already_promoted` complete. `source_moved` and `target_moved` fail. On `conflict`, call
`integration_repair_start` with the event `operation_id`, `expected_target` as
`starting_sha`, and `operation_key` as `trigger_id`; on `started` or `already_started`,
call `integration_repair_dispatch` for literal `stage` zero. Repair outcomes
`dispatched`, `already_dispatched`, and `writer_reused` complete; `busy`,
`configuration_blocked`, `stale`, and `human_required` fail. Start outcomes `stale` and
`invariant_error` fail.

## Rule: project-delivery-readiness

On `delivery.applied`, call `integration_delivery_readiness` for `target_task_id` and
bind `readiness`. Outcomes `ready` and `waiting` complete. On `failed`, inspect
`on_failed_child`: `block` terminates the run as blocked and preserves the parent's suspended
failed
child blocker; `ask` calls `gate_create` with the event `project_id`, literal `gate_type`
`human`, a failed-child `title`, a failed-child `question`, and a one-item
`waiter_task_ids` list containing `target_task_id`. Bind stable `await_id` from the
failed parent's `target_task_id`. Gate outcomes `created`, `reused`,
and `skipped` complete; `rejected` fails. `invariant_error` and runtime failures fail.
Resolving the gate only wakes an ordinary waiter; a later delivery event must re-run
readiness and cannot waive missing child evidence.

## Rule: wake-parent-verifier

On `task.integration_ready`, call `integration_transfer_owner` with `target`,
`expected_token`, `next_owner_id`, and `next_role`. `transferred` completes;
`busy`, `stale_owner`, and `human_required` fail. This handoff wakes the exact persisted
parent verifier on the collected head.

## Rule: record-repair-result

On `integration.ci_completed` where `conclusion` is `failure`, call
`integration_record_repair` with `operation_id` and `evidence_id`. Outcomes `continue`
and `escalate` complete; `human_required` and `budget_exhausted` fail. The typed result
`action` carries escalation decisions; the event is not itself success evidence.

## Rule: verify-parent

On `integration.ci_completed` where `conclusion` is `success` and `target_kind` is
`parent`, call `integration_parent_verify` with `task_id`, `generation`, `head_sha`, and
`evidence_ids`. `verified` completes; `stale_generation`, `stale_head`, and
`invalid_evidence` fail.

## Rule: dispatch-debug

On `integration.repair_exhausted`, call `integration_repair_dispatch` with
`operation_id` and literal `stage` one. Outcomes `dispatched`, `already_dispatched`, and
`writer_reused` complete; `busy`, `configuration_blocked`, `stale`, and
`human_required` fail.

## Rule: expire-repair-stage

On `integration.repair_deadline_due`, call `integration_repair_timeout` with
`operation_id` and `stage`. Outcomes `expired`, `not_due`, and `already_terminal`
complete; `stale` fails. Escalation remains the command's typed `action`, not a renamed
timeout outcome.

## Rule: reconcile-resolution-push

On `integration.resolution_push_observed`, call `integration_reconcile_promotion` with
`promotion_intent_id` bound to `intent_id`. `applied` completes; `not_applied` and
`invariant_error` fail. This lifecycle fact triggers exact remote reconciliation but is
not a receipt or check-success assertion.

## Rule: complete-verified-parent

On `task.integration_verified`, call `integration_complete_parent` with `task_id`,
`generation`, and `head_sha`. `completed` and `already_completed` complete; `waiting`, `stale_verification`,
`failed`, and `invariant_error` fail.

## Rule: observe-repair-close

On `integration.repair_delegate_closed`, terminate completed without invoking a delivery,
readiness, verification, or completion command. The fields `stage`, `session_id`,
`instance_token`, `workspace_id`, and `fence_token` are lifecycle evidence only.

## Parent subject decision table

The inactive reviewed table chooses one parent primitive per durable visit.
Identity refresh precedes action. Failed children, conflicts and legacy repair
dossiers require explicit human decisions. A failed verifier reopens through the
existing proof-based recovery when a new child fix is available; its unchanged
failed head waits. Verifier tasks use the shared writer and lease primitives.
Legacy event rules remain compatibility adapters until explicit engine transfer.

```integration-policy
{
  "max_wait_seconds": 3600,
  "tables": {
    "parent_episode": {
      "default": "idle",
      "cases": [
        {"rule": "human-hold", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "held"}, "right": {"type": "literal", "value": true}}, "action": "human-wait"},
        {"rule": "refresh-identity", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "identity_moved"}, "right": {"type": "literal", "value": true}}, "action": "refresh"},
        {"rule": "legacy-repair-dossier", "when": {"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "legacy_repair_dossier"}, "mode": "present"}, "action": "legacy-repair"},
        {"rule": "failed-child", "when": {"type": "comparison", "op": "gt", "left": {"type": "binding_ref", "binding": "s", "path": "failed_child_count"}, "right": {"type": "literal", "value": 0}}, "action": "failed-child"},
        {"rule": "child-conflict", "when": {"type": "comparison", "op": "gt", "left": {"type": "binding_ref", "binding": "s", "path": "conflict_count"}, "right": {"type": "literal", "value": 0}}, "action": "conflict"},
        {"rule": "resume-publication", "when": {"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "pending_publication"}, "mode": "present"}, "action": "publish"},
        {"rule": "collect-after-verifier-failure", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "verifier_failed"}, "right": {"type": "literal", "value": true}}, {"type": "comparison", "op": "ne", "left": {"type": "binding_ref", "binding": "s", "path": "collection_members"}, "right": {"type": "literal", "value": []}}]}, "action": "merge"},
        {"rule": "collect-child", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "collection_state"}, "right": {"type": "literal", "value": "awaiting_children"}}, {"type": "comparison", "op": "ne", "left": {"type": "binding_ref", "binding": "s", "path": "collection_members"}, "right": {"type": "literal", "value": []}}]}, "action": "merge"},
        {"rule": "unchanged-failed-aggregate", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "failed_aggregate_unchanged"}, "right": {"type": "literal", "value": true}}, "action": "unchanged-red"},
        {"rule": "completed-parent", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "parent_completed"}, "right": {"type": "literal", "value": true}}, "action": "complete"},
        {"rule": "verified-parent", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "aggregate_verified"}, "right": {"type": "literal", "value": true}}, "action": "complete"},
        {"rule": "legacy-verifier-binding", "when": {"type": "bool", "op": "and", "operands": [{"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "verifier_task_id"}, "mode": "present"}, {"type": "bool", "op": "not", "operands": [{"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "budget"}, "mode": "present"}]}]}, "action": "legacy-verifier"},
        {"rule": "file-verifier", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "readiness"}, "right": {"type": "literal", "value": "ready"}}, {"type": "bool", "op": "not", "operands": [{"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "verifier_task_id"}, "mode": "present"}]}]}, "action": "verify"},
        {"rule": "capacity-expired", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "filed"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "budget_expired"}, "right": {"type": "literal", "value": true}}, {"type": "bool", "op": "not", "operands": [{"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "writer.fence_token"}, "mode": "present"}]}]}, "action": "capacity"},
        {"rule": "writer-budget-exhausted", "when": {"type": "bool", "op": "or", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "budget_expired"}, "right": {"type": "literal", "value": true}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "budget_exhausted"}, "right": {"type": "literal", "value": true}}]}, "action": "writer-budget"},
        {"rule": "lease-verifier", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "writer_status"}, "right": {"type": "literal", "value": "filed"}}, {"type": "bool", "op": "not", "operands": [{"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "writer.fence_token"}, "mode": "present"}]}, {"type": "exists", "value": {"type": "binding_ref", "binding": "s", "path": "verifier_task_id"}, "mode": "present"}]}, "action": "lease"},
        {"rule": "red-ci", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "ci_state"}, "right": {"type": "literal", "value": "red"}}, "action": "red"},
        {"rule": "observe-green", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "ci_state"}, "right": {"type": "literal", "value": "green"}}, "action": "observe-ci"},
        {"rule": "request-parent-ci", "when": {"type": "bool", "op": "and", "operands": [{"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "collection_state"}, "right": {"type": "literal", "value": "verifying"}}, {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "ci_state"}, "right": {"type": "literal", "value": "none"}}]}, "action": "request-ci"},
        {"rule": "observe-parent-ci", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "collection_state"}, "right": {"type": "literal", "value": "verifying"}}, "action": "observe-ci"},
        {"rule": "overdue", "when": {"type": "comparison", "op": "eq", "left": {"type": "binding_ref", "binding": "s", "path": "wait_overdue"}, "right": {"type": "literal", "value": true}}, "action": "idle"}
      ],
      "actions": {
        "human-wait": {"primitive": "wait", "inputs": {"seconds": {"type": "literal", "value": 300}, "reason": {"type": "literal", "value": "binding_human_hold"}}, "outcomes": {"waiting": {"kind": "wait", "seconds": 300, "reason": "binding_human_hold"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "refresh": {"primitive": "wait", "inputs": {"seconds": {"type": "literal", "value": 1}, "reason": {"type": "literal", "value": "refresh_parent_identity"}}, "outcomes": {"waiting": {"kind": "wait", "seconds": 1, "reason": "refresh_parent_identity"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "failed-child": {"primitive": "gate", "inputs": {"question": {"type": "literal", "value": "A child failed. Choose retry after a fix, or hold this parent."}, "choices": {"type": "literal", "value": ["retry", "hold"]}, "no_default": {"type": "literal", "value": true}}, "outcomes": {"reused": {"kind": "hold"}, "answered": {"kind": "progress"}, "created": {"kind": "hold"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}, "answers": {"retry": {"kind": "progress"}, "hold": {"kind": "wait", "seconds": 3600, "reason": "human_hold"}}},
        "legacy-repair": {"primitive": "gate", "inputs": {"question": {"type": "literal", "value": "Legacy parent repair dossier requires a decision; unchanged-head stages will not advance."}, "choices": {"type": "literal", "value": ["retry", "hold"]}, "no_default": {"type": "literal", "value": true}}, "outcomes": {"reused": {"kind": "hold"}, "answered": {"kind": "progress"}, "created": {"kind": "hold"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}, "answers": {"retry": {"kind": "progress"}, "hold": {"kind": "wait", "seconds": 3600, "reason": "human_hold"}}},
        "conflict": {"primitive": "gate", "inputs": {"question": {"type": "literal", "value": "Child collection conflicts. Choose retry after a published resolution, or hold."}, "choices": {"type": "literal", "value": ["retry", "hold"]}, "no_default": {"type": "literal", "value": true}}, "outcomes": {"reused": {"kind": "hold"}, "answered": {"kind": "progress"}, "created": {"kind": "hold"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}, "answers": {"retry": {"kind": "progress"}, "hold": {"kind": "wait", "seconds": 3600, "reason": "human_hold"}}},
        "merge": {"primitive": "git_merge_members", "inputs": {"target_ref": {"type": "binding_ref", "binding": "subject", "path": "target_ref"}, "base_sha": {"type": "binding_ref", "binding": "subject", "path": "head_sha"}, "members": {"type": "binding_ref", "binding": "s", "path": "collection_members"}}, "outcomes": {"source_moved": {"kind": "progress"}, "base_moved": {"kind": "progress"}, "merged": {"kind": "progress"}, "conflict": {"kind": "progress"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "verify": {"primitive": "writer_file", "inputs": {"role": {"type": "literal", "value": "verifier"}, "ordinal": {"type": "binding_ref", "binding": "s", "path": "writer_file_ordinal"}, "intelligence_class": {"type": "literal", "value": "standard-high"}, "budget_seconds": {"type": "literal", "value": 5400}, "attempt_limit": {"type": "literal", "value": 2}, "brief": {"type": "literal", "value": "Verify the exact parent aggregate. Record trusted check evidence for the bound operation, generation and published head; close with the result."}}, "outcomes": {"filed": {"kind": "progress"}, "configuration_blocked": {"kind": "progress"}, "exists": {"kind": "progress"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "lease": {"primitive": "writer_lease", "inputs": {"ref": {"type": "binding_ref", "binding": "subject", "path": "target_ref"}, "owner_task_id": {"type": "binding_ref", "binding": "s", "path": "writer.task_id"}, "ttl_seconds": {"type": "literal", "value": 5400}}, "outcomes": {"leased": {"kind": "progress"}, "stale": {"kind": "progress"}, "busy": {"kind": "progress"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "capacity": {"primitive": "wait", "inputs": {"seconds": {"type": "literal", "value": 1800}, "reason": {"type": "literal", "value": "verifier_waiting_for_capacity"}}, "outcomes": {"waiting": {"kind": "wait", "seconds": 1800, "reason": "verifier_waiting_for_capacity"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "unchanged-red": {"primitive": "wait", "inputs": {"seconds": {"type": "literal", "value": 300}, "reason": {"type": "literal", "value": "failed_aggregate_requires_new_child_fix"}}, "outcomes": {"waiting": {"kind": "wait", "seconds": 300, "reason": "failed_aggregate_requires_new_child_fix"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "request-ci": {"primitive": "ci_request", "inputs": {"head": {"type": "binding_ref", "binding": "s", "path": "tested_head"}}, "outcomes": {"already_running": {"kind": "progress"}, "unavailable": {"kind": "progress"}, "requested": {"kind": "progress"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "observe-ci": {"primitive": "ci_observe", "inputs": {"head": {"type": "binding_ref", "binding": "s", "path": "tested_head"}}, "outcomes": {"green": {"kind": "progress"}, "red": {"kind": "progress"}, "pending": {"kind": "progress"}, "untrusted": {"kind": "progress"}, "none": {"kind": "progress"}, "infra": {"kind": "progress"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "red": {"primitive": "gate", "inputs": {"question": {"type": "literal", "value": "Aggregate CI is red. Choose retry after a new child fix, or hold."}, "choices": {"type": "literal", "value": ["retry", "hold"]}, "no_default": {"type": "literal", "value": true}}, "outcomes": {"reused": {"kind": "hold"}, "answered": {"kind": "progress"}, "created": {"kind": "hold"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}, "answers": {"retry": {"kind": "progress"}, "hold": {"kind": "wait", "seconds": 3600, "reason": "human_hold"}}},
        "complete": {"primitive": "cleanup", "inputs": {}, "outcomes": {"clean": {"kind": "close", "reason": "parent_completed", "phase": "done"}, "pending": {"kind": "close", "reason": "parent_completed", "phase": "done"}, "irreversible_marker": {"kind": "close", "reason": "parent_completed", "phase": "done"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "idle": {"primitive": "wait", "inputs": {"seconds": {"type": "literal", "value": 60}, "reason": {"type": "literal", "value": "parent_work_pending"}}, "outcomes": {"waiting": {"kind": "wait", "seconds": 60, "reason": "parent_work_pending"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "publish": {"primitive": "git_publish", "inputs": {"fence": {"type": "binding_ref", "binding": "s", "path": "pending_publication.fence"}, "expected_old_sha": {"type": "binding_ref", "binding": "s", "path": "pending_publication.expected_old_sha"}, "new_sha": {"type": "binding_ref", "binding": "s", "path": "pending_publication.new_sha"}, "require_green": {"type": "literal", "value": false}}, "outcomes": {"published": {"kind": "progress"}, "target_moved": {"kind": "progress"}, "unknown_after_push": {"kind": "progress"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}},
        "legacy-verifier": {"primitive": "gate", "inputs": {"question": {"type": "literal", "value": "A legacy verifier binding needs a reviewed handoff before unified leasing."}, "choices": {"type": "literal", "value": ["retry", "hold"]}, "no_default": {"type": "literal", "value": true}}, "outcomes": {"reused": {"kind": "hold"}, "answered": {"kind": "progress"}, "created": {"kind": "hold"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}, "answers": {"retry": {"kind": "progress"}, "hold": {"kind": "wait", "seconds": 3600, "reason": "human_hold"}}},
        "writer-budget": {"primitive": "gate", "inputs": {"question": {"type": "literal", "value": "The parent writer budget ended. Review retained work and choose retry after a fix, or hold."}, "choices": {"type": "literal", "value": ["retry", "hold"]}, "no_default": {"type": "literal", "value": true}}, "outcomes": {"reused": {"kind": "hold"}, "answered": {"kind": "progress"}, "created": {"kind": "hold"}, "unknown": {"kind": "backoff", "seconds": 5, "ceiling_seconds": 300}}, "answers": {"retry": {"kind": "progress"}, "hold": {"kind": "wait", "seconds": 3600, "reason": "human_hold"}}}
      }
    }
  }
}
```
