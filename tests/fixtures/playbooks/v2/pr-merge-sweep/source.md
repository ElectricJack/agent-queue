---
id: pr-merge-sweep
kind: pipeline
role: pr-merge-sweep
scope: project:agent-queue
enabled: true
triggers:
  - timer.30m
cooldown: 1500
---

# PR merge sweep

This staged V2 authoring source replaces the V1 machine graph only during the
operator's atomic V1-to-V2 switch. Every `timer.30m` event for
`project:agent-queue` begins the one `sweep-open-prs` rule. The existing
`cooldown: 1500` remains documentary V1 metadata: the `timer.30m` interval is
longer than 1500 seconds, so it does not suppress any timer event.

## Rule: sweep-open-prs

There is no guard. The rule always performs the following two steps and then
ends. Both a successful command outcome and every rejected or runtime-error
outcome use the same terminal path, exactly as the V1 graph did.

1. Call `ensure_task` with `project_id` `agent-queue`, `dedup_key`
   `pr-merge-sweep`, `title` `Merge open PRs (sweep)`, `priority` `15`, and
   `profile_id` `pr-merger`. Its literal `description` instructs the task to merge clean PRs
   without tests, resolve conflicts with targeted and area tests only, leave
   unsafe conflicts open with a comment, and close with merged, resolved, and
   skipped PRs. Bind the resulting task as `sweep`. A successful `created` or
   `reused` outcome continues to step 2; a `rejected` or `runtime_error`
   outcome ends the rule.
2. Call `task_route` with `task_id` `sweep.task_id`, preserving `profile_id`
   `pr-merger` and `intelligence_class` `deep-medium`. A `routed`, `rejected`, or `runtime_error`
   outcome ends the rule.

## Failure handling, uniformly

The rule has no retry and never fails the enclosing playbook. Deduplication is
the safety guard: every timer event uses `pr-merge-sweep`, so an already-open
sweep is reused rather than duplicated. The project scope is the authorization
boundary: both commands run only for `agent-queue`.
