---
id: promotion-request
name: Promotion request
version: 1
scope: system
enabled: false
required_capabilities: [integration_promotion_policy_input, promote_request, promote_cancel,
  integration_promotion_publish, integration_backmerge_source, message_send]
triggers:
  - promotion.source_settled
  - promotion.request_due
  - promotion.hotfix_completed
  - promotion.intent_due
  - promotion.delivered
---

# Promotion request

The stored step `type` binds this disabled policy to its project; import and activation are operator actions.
Typed command results have no generic wrapper. Capabilities declare inventory, not operator authority.
Use `project_id` from `event.project_id` and `step_id` from `event.step_id`; never author a branch or SHA.

## Rule: advance-source

On `promotion.source_settled`, complete without commands or notifications.
Request steps await `promotion.request_due` or hotfix completion.
Continuous supersession belongs to the separate continuous policy.

## Rule: explicit-request

On `promotion.request_due`, call `integration_promotion_policy_input`, binding its typed result as `input`.
Only `policy_input` enters decisions: first test `policy.backmerge_pending` in `truthy` mode and notify;
then test `policy.active_request_id` in `truthy` mode and complete. Otherwise call `promote_request` with
`source_sha` from `input.policy.source_sha` and `notes_reviewed` from `event.notes_reviewed`.
`requested` and `already_requested` complete; other registered refusals notify once for this event.

## Rule: hotfix-completed

On `promotion.hotfix_completed`, use explicit-request's read, ordered decisions and request,
passing `from_task` from `event.from_task` to both commands. Notes require explicit review acknowledgement.
Held/refused hotfixes notify; publication follows the step PR gate.

## Rule: visit-intent

On `promotion.intent_due`, call `integration_promotion_publish` with `batch_id` from `event.batch_id`.
`delivered`, `moved`, `published` and `testing` complete; `held`, `unknown`, `not_found`,
`promotion_intent_invalid` and `unavailable` notify. The mechanism builds, gates, attests and publishes;
waiting checks end this run for train reobservation. Never supply approvals or bypass gates.

## Rule: backmerge-delivered

On `promotion.delivered`, call `integration_backmerge_source` with only `project_id` and `step_id`.
Registered successes complete; refusals notify. The mechanism observes published/lower heads,
authors independent batches and PR gates or records disabled backmerge debt; frozen members never change.

## Supervisor controls

Inspect `promote_status` and `promote_list`; use `promote_prepare` for versions/notes, `promote_hotfix`
for urgent fixes, `promote_cancel` to supersede unpublished requests, and operator `promote_approve`
for held steps. Repair tag conflicts/divergence before re-requesting a source containing all owed heads.
Intents past request_ttl remain visible. `promote_cancel` is a control, not an executable step here.

## Failure handling

`migrations_not_additive`, `migration_range_invalid` and `breaking_migration_override_forbidden` notify
once and end the run. Never set `allow_breaking_migration`, retry refusals or authorize overrides.
Each command's `runtime_error` fails directly. Missing event/result paths retain resolution failures;
never invent defaults. Other registered refusals use `message_send` with the event's `project_id`,
`to_kind` = `user`, `to_id` = `user`, `from_kind` = `system`, `from_id` = `promotion-request`, and `body`:
`Promotion needs operator attention. Inspect aq promote status; use prepare, approve or cancel after resolving the named hold.`
`queued` completes; `rejected` and `runtime_error` fail without retry. Durable replay deduplicates one event.

## Building-block compatibility

Explicit capabilities and exact typed paths enable stronger source-policy alignment without graph changes.
Keep per-event `message_send`: state episodes would suppress distinct events and change sender/provenance.
No collections, observation scans, scripts or agent steps need replacement; leave state/scripts undeclared.
