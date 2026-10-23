---
id: promotion-request
name: Promotion request
version: 1
scope: system
enabled: false
required_capabilities:
  - integration_promotion_policy_input
  - promote_request
  - promote_cancel
  - integration_promotion_publish
  - integration_backmerge_source
  - message_send
triggers:
  - promotion.source_settled
  - promotion.request_due
  - promotion.hotfix_completed
  - promotion.intent_due
  - promotion.delivered
---

# Promotion request

The stored step `type` binds this reviewed policy to its project. Import and
activation are separate operator actions; the shipped policy is disabled.
The event's `event.project_id`, `event.step_id`, `event.batch_id`,
`event.from_task` and `event.notes_reviewed` identify durable server facts.
No branch name or SHA is authored in this policy.
Command argument names are `project_id`, `step_id`, `batch_id`, `from_task`,
`source_sha`, `request_id`, `notes_reviewed`, `to_kind`, `to_id`, `from_kind`,
`from_id` and `body`. The result binding is `input.policy`.

Use typed command outcomes and exact result paths, without a generic-result
wrapper. The declared capabilities retain the reviewed command inventory;
they do not grant operator authority. `promote_cancel` remains a supervisor
control and is not an executable step of this request policy.

## Rule: advance-source

On `promotion.source_settled`, complete without a command or notification.
Request steps wait for an explicit `promotion.request_due` or hotfix completion.
Continuous-step supersession belongs to the separate continuous policy.

## Rule: explicit-request

On `promotion.request_due`, call `integration_promotion_policy_input` with
`project_id` from `event.project_id` and `step_id` from `event.step_id`, saving
the typed result as `input`. Only outcome `policy_input` enters the decision.
First test `policy.backmerge_pending` in `truthy` mode: notify if true.
Then test `policy.active_request_id` in `truthy` mode: complete if true.
Otherwise call `promote_request` with the same project and step, `source_sha`
from `input.policy.source_sha`, and `notes_reviewed` from `event.notes_reviewed`.
Both `requested` and `already_requested` complete. Refusals notify once for
this durable event. No request result is needed by a subsequent step.

## Rule: hotfix-completed

On `promotion.hotfix_completed`, use the same read, ordered decision and request
as explicit-request, additionally passing `from_task` from `event.from_task`
to both `integration_promotion_policy_input` and `promote_request`.
Notes still need the explicit `event.notes_reviewed` acknowledgement.
A held or refused hotfix notifies the operator; publication always follows
the step PR gate.

## Rule: visit-intent

On `promotion.intent_due`, run `integration_promotion_publish` for
`event.batch_id`. That mechanism builds, gates, attests and publishes in order;
a waiting check ends this run and the train reobserves exact evidence. A held
intent, red checks, tag conflict or diverged target notifies with `message_send`.
Outcomes `delivered`, `moved`, `published` and `testing` complete; `held`,
`unknown`, `not_found`, `promotion_intent_invalid` and `unavailable` notify.
No playbook supplies approvals or bypasses a gate.

## Rule: backmerge-delivered

On `promotion.delivered`, run `integration_backmerge_source`. It observes the
published head and each lower branch, retaining independent new batches and PR
gates or recording the configured disabled backmerge debt. No frozen member is
appended or rewritten. Pass only `project_id` from `event.project_id` and
`step_id` from `event.step_id`; registered success outcomes complete and
registered refusals notify.

## Supervisor controls

Inspect `promote_status` and `promote_list`. Use `promote_prepare` for a version
bump and notes, `promote_hotfix` for an urgent fix, and `promote_cancel` to
supersede an unpublished request. An operator uses `promote_approve` for a held
step. A tag conflict or divergent backmerge requires inspection and an ordinary
repair; the operator cancels and re-requests only after the repaired source
contains all owed heads. Intents beyond request_ttl remain visible in status.

## Failure handling

Migration refusals (`migrations_not_additive`, `migration_range_invalid`, and
`breaking_migration_override_forbidden`) notify the operator once and end the
run. These policies never set `allow_breaking_migration`, retry a refused
request, or authorize an override; that decision belongs to the local operator.

Each command's `runtime_error` goes directly to a failed terminal. Other
registered refusals take the notification path described above. Missing required
event or result paths retain the engine's existing resolution-failure behavior;
do not coalesce them into invented defaults.

Notification uses `message_send`, with `project_id` from `event.project_id`,
`to_kind` = `user`, `to_id` = `user`, `from_kind` = `system`, and
`from_id` = `promotion-request`. Its exact `body` is:
`Promotion needs operator attention. Inspect aq promote status; use prepare, approve or cancel after resolving the named hold.`
Outcome `queued` completes; `rejected` and `runtime_error` fail without retry.
Durable command replay prevents duplicate messages for the same event.
Publication and target movement remain server-owned mechanisms.

## Building-block compatibility

This policy already uses typed commands and deterministic decisions. Explicit
capabilities and result paths opt its source into the reusable building blocks'
policy-alignment checks while preserving the executable graph. Keep
`message_send`: namespace notification episodes would suppress distinct durable
events and change sender/provenance, unlike the existing per-event replay.
There is no collection scan, diagnostic observation, script or agent step to
replace. Leave state version and script manifests undeclared; do not add a
second state store or reimplement publication/backmerge mechanisms in scripts.
