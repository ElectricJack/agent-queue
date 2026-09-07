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

The playbook never repairs, retries, reopens, or closes anything itself. It
hands the blocked task to the project's supervisor, whose job is to read the
tail of the session log and decide whether anything needs to be done: retry
with concrete guidance, hold, spawn a follow-up, or escalate to the human.

## Rule: escalate-blocked-task

There is no guard beyond the trigger filter. The rule performs one step and
then ends.

1. Call `message_send` with `project_id` bound to the event's `project_id`,
   `to_kind` `session`, `to_id` `supervisor-` followed by the event's
   `project_id` (the project supervisor's messaging address), `from_kind`
   `system`, `from_id` `playbook:blocked-task-escalation`, and `priority`
   `50`. The `subject` is `Blocked task:` followed by the event's `title` and
   its `task_id`. The `body` states that the task with that `task_id` ended
   `BLOCKED`, quotes the event's `context` (the close leg), `error` (the
   agent's closing notes or the failure detail, or `n/a` when absent), and
   `agent_id` (the agent that held it, or `unknown`), and then instructs the
   supervisor to read the tail of the session log with
   `aq session logs <session-id> -n 200` after finding the session in the
   task column of `aq session list`, to read `aq task show <task-id>` and
   `aq task explain <task-id>`, then check `aq integration status` for the
   event's project to identify any integration operation owning this task.
   For integration-owned repair or verification work, do not use generic task
   recovery or create replacement repair tasks. Let the existing primary/debug
   escalation run and do not reset its attempt or time budgets. If that operation
   requires human action, escalate with its operation ID; an authorized operator
   can use `aq integration resume` or `aq integration abort` after inspecting it.
   For ordinary tasks not owned by an integration operation, decide: retry with
   concrete feedback via `aq task recover`, hold, spawn a follow-up task, or message the human
   with `aq message send --to user:dashboard` when human judgment is needed.
   Bind the result as `notice`. A `queued` outcome ends the rule; a
   `rejected` or `runtime_error` outcome fails it.

## Failure handling, uniformly

The rule has no retry. A failed step ends the run with a `failed` terminal so
the run overlay shows what broke — a project without a supervisor address, a
disabled messages substrate — and the next blocked task starts a fresh run.
One message per blocked event is the intended cardinality: the message is the
escalation, and the supervisor's own delivery cascade wakes the on-demand
supervisor session or holds the notice until it is next primed. The event's
`project_id` is the authorization boundary: the message is addressed only to
that project's supervisor and carries only that project's task.
