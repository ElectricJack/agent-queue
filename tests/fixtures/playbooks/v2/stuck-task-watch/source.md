---
id: stuck-task-watch
name: Stuck-task watch
version: 1
scope: system
enabled: true
triggers:
  - timer.10m
---

# Stuck-task watch

Every ten minutes check ACTIVE projects for stuck tasks and send the current
list to `session:supervisor-global` while anything is stuck. Paused projects,
containers and human-gated holds are excluded. Detection is documented in
`docs/specs/stuck-task-watch.md`.

There is no memory or deduplication: each tick sends again until the supervisor
fixes the work. Each line carries project, task, state, age, reason and a
suggested diagnostic command. This playbook only reads and notifies; it never
repairs, stops, restarts, resumes, routes or closes tasks.

## Rule: watch-stalled-work

1. Call `get_stuck_tasks` with no arguments and bind the result as `watch`.
   On `read`, continue. On `rejected` or `runtime_error`, fail the rule.
2. If `watch.stuck` is empty, end successfully; otherwise continue.
3. Call `message_send` with `to_kind` `session`, `to_id` `supervisor-global`,
   `from_kind` `system`, `from_id` `stuck-task-watch`, `subject` `Stuck tasks`
   and `body` bound to `watch.summary`. On `queued`, end successfully.
   On `rejected` or `runtime_error`, fail the rule.

## Failure handling, uniformly

There is no in-run retry or model call. A failed read or refused message ends
the run visibly; the next ten-minute tick reads again. Shipping this
first-install default never replaces an existing installation's policy or
changes daemon readiness.
