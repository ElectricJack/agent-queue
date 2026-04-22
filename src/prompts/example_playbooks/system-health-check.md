---
id: system-health-check
triggers:
  - timer.30m
scope: system
---

# System Health Check

Every 30 minutes, check overall system health and surface problems
that need attention. Focus on conditions that block progress or
indicate something is silently broken.

## Stuck tasks

Look for tasks in ASSIGNED or IN_PROGRESS status that have been
in that state for an unusually long time (more than 30 minutes for
ASSIGNED, more than 2 hours for IN_PROGRESS). These may indicate
an agent crash, a rate-limit stall, or a task that is spinning
without making progress.

If a stuck task is found, check whether the assigned agent is still
alive. If the agent is running but idle, restart the task. If the
agent is dead, reassign the task to another available agent or move
it back to PENDING.

## Blocked tasks

Check for BLOCKED tasks that have no resolution path — no upstream
dependency that would unblock them, no human input requested. These
tasks are stuck forever unless someone intervenes. Flag them for
review.

Also check for circular dependency chains where two or more tasks
block each other.

## Agent health

Agents are derived from project workspaces (workspace-as-agent
model). `list_agents` is project-scoped, so this step must do the
project fan-out itself in one node — do not split into
per-project nodes.

In a single LLM turn, call `list_projects` to enumerate active
projects, then call `list_agents(project_id=...)` once per
project, then aggregate the results. A project counts as
unhealthy **only** if it has pending or ready tasks but every
workspace is busy or locked (stalled throughput); an all-idle
project with no queued work is healthy. If `list_agents` errors
for a specific project, record the error and continue with the
remaining projects — do not abort the step and do not treat the
error as a health finding.

Return an object with two fields: `unhealthy_projects` (array of
project ids that meet the stalled-throughput criterion) and
`tool_errors` (array of per-project errors encountered while
checking).

## Decide whether to post

Consolidate findings from the prior steps: stuck tasks, blocked
tasks, and unhealthy projects from the agent-health step.

If `stuck_tasks` is empty AND `blocked_issues` is empty AND
`unhealthy_projects` is empty, there is nothing to post. Go
straight to saving findings — do not post, do not draft a
summary.

Otherwise, draft a concise summary grouped by severity: critical
(stuck tasks blocking the queue), warning (blocked tasks with no
path, unhealthy projects), info (minor anomalies). Do **not**
include tool-integration errors from this playbook's own steps in
the summary — those are operator diagnostics, not queue findings.

## Post to notifications channel

Post the summary to the system notifications Discord channel.

First, call `get_system_channel(name="notifications")` to resolve
the channel id from daemon config. Then call `send_message` with
that `channel_id` and the drafted summary as `content`. Do **not**
pull a channel id from project memory, facts, or any other
playbook's output. This playbook runs at system scope; the
audience is the operator, not any project team.

## Save findings

Save the structured findings (stuck tasks, blocked issues,
unhealthy projects, and any tool errors recorded above) to system
memory under `system_health_check_results` so downstream
playbooks (like the codebase inspector) can consult them before
creating duplicate tasks.
