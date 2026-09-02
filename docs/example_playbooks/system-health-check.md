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

**Tool contract.** Every step below names the exact tools that are
available in the registry. Do **not** invent dotted-namespace tool
names like `system_monitor.get_stuck_tasks` — no such namespace
exists. If a step below does not name a tool that covers your need,
report that plainly in the summary rather than fabricating one.

Load the relevant categories at startup:
`load_tools(category="task")`, `load_tools(category="agent")`,
`load_tools(category="system")`.

## Stuck tasks

Look for tasks in ASSIGNED or IN_PROGRESS status that have been
in that state for an unusually long time (more than 30 minutes for
ASSIGNED, more than 2 hours for IN_PROGRESS). These may indicate
an agent crash, a rate-limit stall, or a task that is spinning
without making progress.

Concrete procedure:

1. Call `get_stuck_tasks(now=<tick_time>)`. Pass the trigger event's
   `tick_time` as `now` so repeated runs are deterministic. The tool
   handles threshold arithmetic internally (ASSIGNED > 30 min,
   IN_PROGRESS > 2 h by default — override via
   `assigned_threshold_seconds` / `in_progress_threshold_seconds` if
   needed). The response is
   `{stuck: [{id, project_id, status, assigned_agent, updated_at,
   seconds_in_state}, ...], now_used, thresholds}`.
2. For each entry in `stuck`, cross-reference `list_agents()` to see
   whether the assigned agent slot is still `idle` or `busy`.
3. Remediate:
   - Agent alive and idle → `restart_task(task_id=...)`.
   - Agent dead or unreachable →
     `set_task_status(task_id=..., status="READY")` with
     `assigned_agent_id=None` so the scheduler can reassign it.

## Blocked tasks

Check for BLOCKED tasks that have no resolution path — no upstream
dependency that would unblock them, no human input requested. These
tasks are stuck forever unless someone intervenes. Flag them for
review.

Also check for circular dependency chains where two or more tasks
block each other.

Concrete procedure:

1. Call `list_tasks(status="BLOCKED")` for each active project
   (enumerate via `list_projects()` if needed).
2. For each blocked task, call `get_task_dependencies(task_id=...)`
   (or `task_deps(...)`) to see whether any upstream task could
   still complete.
3. Call `get_chain_health(project_id=...)` per project — this
   already surfaces circular dependencies and long-blocked chains
   in a single structured response.
4. Flag any BLOCKED task whose dependencies are all COMPLETED,
   FAILED, or missing for human review in the summary.

## Agent health

Agents are derived from project workspaces (workspace-as-agent
model). `list_agents` is project-scoped, so this step must do the
project fan-out itself in one node — do not split into
per-project nodes.

Since the workspace-as-agent refactor, there is no standalone
"agent" entity — agent slots are derived from project workspaces.
`create_agent`/`edit_agent`/`delete_agent`/`pause_agent`/`resume_agent`
are deprecated. Use `list_agents(project_id=...)` + `list_workspaces`
+ `get_recent_events` + `get_agent_error` instead.

In a single LLM turn, call `list_projects` to enumerate active
projects, then call `list_agents(project_id=...)` once per
project, then aggregate the results. A project counts as
unhealthy **only** if it has pending or ready tasks but every
workspace is busy or locked (stalled throughput); an all-idle
project with no queued work is healthy. If `list_agents` errors
for a specific project, record the error and continue with the
remaining projects — do not abort the step and do not treat the
error as a health finding.

For any project flagged as unhealthy, enrich the finding with:

- `list_workspaces(project_id=...)` to see `locked_by_agent_id`,
  `locked_by_task_id`, and `lock_mode` per workspace.
- `get_recent_events(event_type="agent.*", since="2h")` to detect
  recent crashes, errors, pickups, or replies.
- `get_agent_error(task_id=...)` for any failed task to retrieve
  the last `error_message` / `error_type`.
- `get_status()` once for a system-wide overview (project counts,
  agent counts, task counts by status, `orchestrator_paused`).

Do not double-report a slot whose busy state is already explained
by a stuck task flagged in the first section.

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
memory under `system_health_check_results` (tagged
`#system-health-check`) so downstream playbooks (like the codebase
inspector) can consult them before creating duplicate tasks.
