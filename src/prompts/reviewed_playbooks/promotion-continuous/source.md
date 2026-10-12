---
id: promotion-continuous
name: Promotion continuous
version: 1
scope: system
enabled: false
triggers:
  - promotion.source_settled
  - promotion.request_due
  - promotion.hotfix_completed
  - promotion.intent_due
  - promotion.delivered
---

# Promotion continuous

The stored step `type` binds this reviewed policy to its project. Import and
activation are separate operator actions; the shipped policy is disabled.
The event's `event.project_id`, `event.step_id`, `event.batch_id`,
`event.from_task` and `event.notes_reviewed` identify durable server facts.
No branch name or SHA is authored in this policy.
Command argument names are `project_id`, `step_id`, `batch_id`, `from_task`,
`source_sha`, `request_id`, `notes_reviewed`, `to_kind`, `to_id`, `from_kind`,
`from_id` and `body`. The result binding is `input.policy`.

## Rule: advance-source

On `promotion.source_settled`, continuous steps read
`integration_promotion_policy_input`, wait when `policy.backmerge_pending`,
and notify the operator with `message_send`. Request steps wait for an explicit
`promotion.request_due` or hotfix completion. A continuous step with a newer
source cancels its outstanding `policy.active_request_id` using `promote_cancel`
and requests `policy.source_sha` through `promote_request`; a same-head intent
waits. Cancellation refusal preserves the existing intent and notifies.

## Rule: explicit-request

On `promotion.request_due`, read `integration_promotion_policy_input`; outstanding
backmerge debt waits and notifies. A current intent waits. Otherwise run
`promote_request` with the source returned by the mechanism and the explicit
`event.notes_reviewed` acknowledgement. The input bindings are `input.policy`
and `requested.batch_id`; a refusal notifies once for this durable event.

## Rule: hotfix-completed

On `promotion.hotfix_completed`, use `event.from_task` with
`integration_promotion_policy_input` and `promote_request`. Notes still need an
explicit review acknowledgement. A held or refused hotfix notifies the operator;
publication always follows the step PR gate.

## Rule: visit-intent

On `promotion.intent_due`, run `integration_promotion_publish` for
`event.batch_id`. That mechanism builds, gates, attests and publishes in order;
a waiting check ends this run and the train reobserves exact evidence. A held
intent, red checks, tag conflict or diverged target notifies with `message_send`.
No playbook supplies approvals or bypasses a gate.

## Rule: backmerge-delivered

On `promotion.delivered`, run `integration_backmerge_source`. It observes the
published head and each lower branch, retaining independent new batches and PR
gates or recording the configured disabled backmerge debt. No frozen member is
appended or rewritten.

## Supervisor controls

Inspect `promote_status` and `promote_list`. Use `promote_prepare` for a version
bump and notes, `promote_hotfix` for an urgent fix, and `promote_cancel` to
supersede an unpublished request. An operator uses `promote_approve` for a held
step. A tag conflict or divergent backmerge requires inspection and an ordinary
repair; the operator cancels and re-requests only after the repaired source
contains all owed heads. Intents beyond request_ttl remain visible in status.

## Failure handling

Every missing fact, daemon refusal, and runtime error ends visibly failed or
waits and notifies once per event. Message delivery is `message_send` to the
operator user; durable command replay prevents duplicate messages for that event.
Publication and target movement remain server-owned mechanisms.
