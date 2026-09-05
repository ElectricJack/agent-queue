---
id: hierarchical-delivery
name: Hierarchical delivery
version: 1
scope: system
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

# Hierarchical delivery

This disabled policy connects durable hierarchy lifecycle facts to deterministic
integration commands. It never treats a repair-task close or a resolution push
observation as delivery evidence, and it never supplies resolution Git object IDs.

## Rules: child-terminal-readiness

On `task.completed` or `task.failed` for a hydrated task whose
`task.parent_task_id` is present, call `integration_delivery_readiness` with that
immediate parent as `task_id` and bind `readiness`. Outcomes `ready` and `waiting`
complete. On `failed`, inspect `on_failed_child`: `block` terminates failed while
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
`on_failed_child`: `block` terminates failed and preserves the parent's suspended failed
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
`generation`, and `head_sha`. `completed` completes; `waiting`, `stale_verification`,
and `invariant_error` fail.

## Rule: observe-repair-close

On `integration.repair_delegate_closed`, terminate completed without invoking a delivery,
readiness, verification, or completion command. The fields `stage`, `session_id`,
`instance_token`, `workspace_id`, and `fence_token` are lifecycle evidence only.
