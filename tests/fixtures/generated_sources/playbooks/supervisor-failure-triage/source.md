---
id: supervisor-failure-triage
name: Supervisor failure triage
version: 1
scope: system
enabled: true
triggers:
  - task.failed
---

# Supervisor failure triage

Every durable `task.failed` event starts one `triage-failed-task` rule for
that event's project. The event is the domain event, not the dashboard's
`notify.task_failed` transport notification. The command creates or reuses the
one durable failure incident for the task's failure generation, and only a new
actionable incident wakes the project supervisor. Event replay, concurrent
delivery, and the periodic recovery scan reuse that incident and never create
a second supervisor notice.

The incident is diagnostic data, not a direction to change a task directly.
The supervisor reads the task, comments, transcript, dependencies, phase and
delivery state, routing, holds, and current guards before recording one durable
verdict. It may choose one bounded, guard-approved action: retry or reopen with
concrete feedback; route a new attempt to an eligible provider/class; split
separable work while preserving dependencies, provenance, and phase membership;
file a narrowly scoped corrective task; or escalate the decision to Jack.
Failures of a remediation task and exhausted budgets attach evidence to the
same incident and escalate; they do not form an automatic remediation chain.

The playbook neither force-completes work nor waives a phase, ownership,
delivery, retry, or routing guard. A phase remains visibly held until every
child reaches `COMPLETED`; triage does not change phase admission or
settlement. Provider failover remains the authority for provider health.

## Rule: triage-failed-task

There is no status filter: the command decides whether the durable failure is
actionable. The rule performs one idempotent step and then ends.

1. Call `task_failure_triage_notify` with `task_id` bound to the event's
   `task_id` and `project_id` bound to the event's `project_id`. Bind the
   result as `incident`. `queued` (new incident), `existing` (event/scan
   replay), `not_actionable` (the terminal state or stopped attempt is not
   ready yet), and `retired` (an ended integration operation retired its
   delegate) complete the rule. `rejected` and `runtime_error` fail it.

## Failure handling, uniformly

The rule has no retry. A refused or failed command ends the run visibly. The
next durable failure starts a fresh run, while a replay of the same generation
uses the existing incident and message. The event's `project_id` remains the
authorization boundary for the incident and supervisor wake.
