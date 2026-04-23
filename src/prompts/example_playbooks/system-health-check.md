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

1. Call `list_tasks(status="ASSIGNED")` and
   `list_tasks(status="IN_PROGRESS")` (or
   `list_active_tasks_all_projects()` for a cross-project dump).
   Each task dict contains `id`, `project_id`, `status`,
   `assigned_agent`, `created_at`, `updated_at` (Unix timestamps).
2. Use the trigger event's `tick_time` as "now". A task is stuck if
   `now - updated_at > 1800` (ASSIGNED) or `> 7200` (IN_PROGRESS).
   `updated_at` advances on every state transition, so it is the
   correct "time in current state" proxy — do not use `created_at`.
3. For each stuck task, cross-reference `list_agents()` to see
   whether the assigned agent slot is still `idle` or `busy`.
4. Remediate:
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

Verify that all agent slots marked as active are actually making
progress. Check for slots that have been busy for an unusually long
time or idle despite READY tasks waiting.

Concrete procedure:

1. Call `list_agents()` — each entry reports the workspace-derived
   slot state (`busy`/`idle`) and `current_task_id`.
2. Call `list_workspaces(project_id=...)` to see
   `locked_by_agent_id`, `locked_by_task_id`, and `lock_mode` per
   workspace.
3. Call `get_recent_events(event_type="agent.*", since="2h")` to
   detect recent crashes, errors, pickups, or replies.
4. For any task reported as failed, call
   `get_agent_error(task_id=...)` to retrieve the last
   `error_message` / `error_type`.
5. Call `get_status()` once for a system-wide overview (project
   counts, agent counts, task counts by status, `orchestrator_paused`).

Note: since the workspace-as-agent refactor, there is no standalone
"agent" entity — agent slots are derived from project workspaces.
`create_agent`/`edit_agent`/`delete_agent`/`pause_agent`/`resume_agent`
are deprecated. Use `list_agents` + `list_workspaces` +
`get_recent_events` + `get_agent_error` instead.

Do not double-report a slot whose busy state is already explained
by a stuck task flagged in the first section.

## Summary

If any issues are found, post a concise summary to the project
channel. Group findings by severity: critical (stuck tasks blocking
the queue), warning (blocked tasks with no path), info (minor
anomalies). Skip the summary entirely if everything looks healthy
— do not post "all clear" messages.

Save findings to system memory with tag `#system-health-check` so
that other playbooks (like the codebase inspector) can check for
related issues before creating duplicate tasks.
