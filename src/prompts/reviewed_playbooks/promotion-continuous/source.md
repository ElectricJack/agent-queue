---
id: promotion-continuous
name: Promotion continuous
version: 1
scope: system
enabled: false
required_capabilities: [integration_promotion_policy_input, promote_request, promote_cancel,
  integration_promotion_publish, integration_backmerge_source, message_send]
triggers: [promotion.source_settled, promotion.request_due, promotion.hotfix_completed,
  promotion.intent_due, promotion.delivered]
---

# Promotion continuous

The stored step `type` binds policy to its project; only operators import or activate it.
Use server facts: `event.project_id`, `event.step_id`, `event.batch_id`, `event.from_task`,
`event.notes_reviewed`. Never author branches or SHAs.
Save typed `integration_promotion_policy_input` as `input`; command outcomes control the graph.
Read `input.policy`: `policy.backmerge_pending`, `policy.newer_source`,
`policy.active_request_id`, `policy.source_sha`. Test the first three with `exists` in `truthy` mode.
Inputs: `step_id`, `source_sha`, `notes_reviewed`, `from_task` and `batch_id`.

## Rule: advance-source

On `promotion.source_settled`, read `integration_promotion_policy_input`; backmerge debt notifies.
A continuous step with `policy.newer_source` cancels
`policy.active_request_id` as `request_id` with `promote_cancel`, then `policy.source_sha` with
`promote_request`. Otherwise an active same-head intent waits; absent one, request.
Cancellation refusal notifies. Request steps wait for `promotion.request_due` or hotfix completion.

## Rule: explicit-request

On `promotion.request_due`, read `integration_promotion_policy_input`; debt notifies, active intents wait.
Otherwise `promote_request` uses `policy.source_sha` and `event.notes_reviewed`; refusals notify.

## Rule: hotfix-completed

On `promotion.hotfix_completed`, use `event.from_task` with
`integration_promotion_policy_input` and `promote_request`. Notes need explicit review;
a held or refused hotfix notifies. Publication follows the step PR gate.

## Rule: visit-intent

On `promotion.intent_due`, run `integration_promotion_publish` for `event.batch_id`.
The mechanism builds, gates, attests and publishes; waiting checks end this event so
the train reobserves exact evidence. Held intents, red checks, tag conflicts or divergent
targets notify. No playbook supplies approvals or bypasses gates.

## Rule: backmerge-delivered

On `promotion.delivered`, run `integration_backmerge_source`: observe the published head
and lower branches, retaining independent batches/PR gates or configured disabled backmerge
debt. Never append or rewrite frozen members; the server owns backmerge debt and replay.

## Supervisor controls

Inspect `promote_status`/`promote_list`; use `promote_prepare` for versions/notes,
`promote_hotfix` for urgent fixes, `promote_cancel` to supersede unpublished requests,
and operator `promote_approve` for held steps. Repair tag conflicts/divergent backmerges
before re-requesting; include all owed heads. Expired request_ttl intents stay visible.

## Failure handling

Migration refusals (`migrations_not_additive`, `migration_range_invalid`,
`breaking_migration_override_forbidden`) notify once and end. Never set
`allow_breaking_migration`, retry refusals or authorize overrides; those are operator decisions.
Other refusals notify; command `runtime_error` ends failed directly. Successful outcomes
advance, except publication `held`/`unknown` notify; `testing` ends completed for reobservation.
Notifications use `message_send`: `to_kind` = `user`, `to_id` = `user`, `from_kind` = `system`,
`from_id` = `promotion-continuous`, `project_id` = `event.project_id`, with `body`:
"Promotion needs operator attention. Inspect aq promote status; use prepare, approve or cancel after resolving the named hold."
Notify once per event with durable replay deduplication: `queued` completes; `rejected`/`runtime_error` fail.
