---
id: blocked-task-escalation
name: Blocked task escalation
version: 1
scope: system
enabled: true
triggers:
  - task.failed
---

# Blocked task escalation

Every `task.failed` event whose `status` field is `BLOCKED` begins the one
`escalate-blocked-task` rule. The value is the task status enum's own
upper-case spelling, exactly as the orchestrator puts it on the wire. The
orchestrator emits that event for every terminal `BLOCKED` leg of a session
close — a hard failure, a spent retry budget, and a pass whose pipeline
stopped short — and for a timed-out attempt.
A `task.failed` event with any other `status` (a retry that re-queued the task,
say) is not a blocked task and never starts a run. The dependency-graph
`task.blocked` flip is a different fact about a different kind of blocking and
is deliberately not a trigger: there is no session to read.

The playbook never repairs, retries, reopens, or closes anything itself, and it
writes no message of its own. It wakes the task's one durable recovery
incident — the same record the daemon's periodic recovery scan reconciles — so
the failure event, a replay of it and the scan produce one incident and one
supervisor notice. That notice names the incident's owner (the project
supervisor, or the integration operation that owns the task), the remaining
worker-retry and supervisor-recovery budget, which deadline stopped the attempt
(runtime, inactivity, or an integration stage's own clock or acceptance wait),
and the next action. The supervisor decides with `aq task recover`. An
integration-owned failure stays with its operation, which keeps its own attempt
and time budgets. A genuinely human decision goes through one escalation bound
to the incident.

## Rule: escalate-blocked-task

There is no guard beyond the trigger filter. The rule performs one step and
then ends.

1. Call `task_recovery_notify` with `task_id` bound to the event's `task_id`
   and `project_id` bound to the event's `project_id`. Bind the result as
   `incident`. A `queued` outcome (a new incident), an `existing` outcome (the
   same incident, recorded by the scan or an earlier event), a `not_actionable`
   outcome (no stopped attempt or recorded failure yet; the scan records the
   incident once there is one) or a `retired` outcome (an ended integration
   operation retired the delegate, so nothing is recovered) ends the rule; a
   `rejected` or `runtime_error` outcome fails it.

## Failure handling, uniformly

The rule has no retry. A failed step ends the run with a `failed` terminal so
the run overlay shows what broke — a disabled messages substrate, a task outside
the event's project — and the next blocked task starts a fresh run. The command
is idempotent: a replayed event reuses the incident instead of filing another,
and the supervisor's own delivery cascade wakes the on-demand supervisor session
or durably holds the notice for a restarted/replacement supervisor. The event's
`project_id` is the authorization boundary: the command refuses a task outside
that project, and the notice goes only to that project's supervisor.
