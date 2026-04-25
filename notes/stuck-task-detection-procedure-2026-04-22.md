# Stuck-Task Detection — Procedure Using Existing Tools

**Task:** `vivid-cascade` — Investigate missing `system_monitor.get_stuck_tasks`
tool and identify stuck tasks
**Date:** 2026-04-22
**Status:** Investigation (no code changes in this task)

## TL;DR

- `system_monitor.get_stuck_tasks` does not exist and never did. It was
  hallucinated by the playbook compiler's LLM. See the companion note
  `notes/system-health-check-missing-tools-2026-04-22.md` (task
  `solid-orbit`) for the detailed root-cause analysis.
- The source playbook `src/prompts/example_playbooks/system-health-check.md`
  has already been rewritten (commit `7fecb79b`) to name real tools and
  carry an explicit "do not invent tool names" guard clause.
- Deployed compiled playbooks live under
  `~/.agent-queue/playbooks/compiled/`. There is currently **no**
  `system-health-check.json` compiled artifact in that directory — the
  playbook has never been installed into the vault/store on this machine,
  so the compiled graph that actually fires the `timer.30m` trigger is
  being produced elsewhere (likely compiled on demand from the example
  source, which may or may not already be stale).
- A concrete replacement procedure using only registered tools is
  verified below against the live daemon.

## Verified procedure

Against the currently-running daemon (registered tools `list_tasks`,
`list_agents`, `restart_task`, `set_task_status`, `get_recent_events` —
all present in `src/tools/definitions.py`):

1. **Pull the candidate set.**
   - `list_tasks(status="ASSIGNED")`
   - `list_tasks(status="IN_PROGRESS")`
   - Optional cross-project dump: `list_active_tasks_all_projects()`
   Each task dict carries `id`, `project_id`, `status`,
   `assigned_agent`, `created_at`, `updated_at` (Unix timestamps).

2. **Pick a reference "now".**
   - Preferred: the trigger event's `tick_time` (playbook executes in
     response to `timer.30m`, which carries this field).
   - Fallback: current wall-clock when no trigger event is available.

3. **Apply the thresholds.**
   - A task is stuck if
     `now - updated_at > 1800` (ASSIGNED, 30 min) or
     `now - updated_at > 7200` (IN_PROGRESS, 2 h).
   - Use `updated_at`, not `created_at` — `updated_at` advances on every
     state transition and is the correct "time in current state" proxy.

4. **Cross-reference agent health.**
   - `list_agents()` → check whether the assigned agent slot is still
     `busy` or has gone `idle`.
   - `get_recent_events(event_type="agent.*", since="2h")` → correlate
     with recent crashes, token-exhaust pauses, or rate-limit stalls.

5. **Remediate.**
   - Agent alive and idle → `restart_task(task_id=...)`.
   - Agent dead or unreachable →
     `set_task_status(task_id=..., status="READY",
     assigned_agent_id=None)` to let the scheduler re-assign.

### Live-fire check (today)

Running the procedure against the daemon at the time of this
investigation produced:

- `list_tasks(status="ASSIGNED")` → 0 tasks.
- `list_tasks(status="IN_PROGRESS")` → 3 tasks, all within a fresh
  `updated_at` window (this task plus two peers). No stuck tasks
  present right now.

The queries returned structured data exactly as the revised playbook
describes, confirming the procedure is sound against the real registry.

## What the follow-up should actually do

The revised playbook markdown instructs a supervisor LLM to combine
`list_tasks` + time arithmetic + `list_agents` at runtime. That works,
but it puts time-math and multi-tool orchestration on the LLM every
single invocation. A more robust fix is to **promote this logic to a
first-class tool** so the playbook (and any future caller) can just ask
for "stuck tasks" and receive a deterministic, structured answer.

Proposed follow-up: implement `get_stuck_tasks` as a real command
handler in the `system` category:

- Signature:
  `get_stuck_tasks(assigned_threshold_seconds=1800, in_progress_threshold_seconds=7200, now=None, project_id=None)`.
- Returns `{stuck: [{id, project_id, status, assigned_agent,
  updated_at, seconds_in_state}], now_used, thresholds}`.
- Piggybacks on existing `db.list_tasks(status=...)` queries; no new
  schema needed.
- Register in `src/tools/definitions.py` with
  `_TOOL_CATEGORIES["get_stuck_tasks"] = "system"` so it can be
  pulled in via `load_tools(category="system")`.
- Update `src/prompts/example_playbooks/system-health-check.md` to
  call `get_stuck_tasks(...)` directly and delete the multi-step
  supervisor procedure from the Markdown body.
- Force recompile of the system-health-check playbook so the
  deployed graph uses the new tool (or install the fixed markdown
  into the vault if the current deployment is still reading from
  the example source).

This converts the hallucinated name into a real thing, eliminates
time-math on the LLM side, and makes the same primitive available to
future playbooks (memory-consolidation, agent-health, etc.) without
any further prompt engineering.

## Files referenced

- `src/prompts/example_playbooks/system-health-check.md` (already fixed,
  commit `7fecb79b`)
- `notes/system-health-check-missing-tools-2026-04-22.md` (sibling
  investigation, task `solid-orbit`)
- `src/tools/definitions.py` (tool registry — `get_stuck_tasks` is
  **not** registered here)
- `src/tools/registry.py` (categories, no `system_monitor` namespace)
- `src/orchestrator/monitoring.py` (has `_check_stuck_defined_tasks`
  for DEFINED-state tasks only — not the same surface as the proposed
  `get_stuck_tasks`, which targets ASSIGNED/IN_PROGRESS)
- `src/database/queries/dependency_queries.py::get_stuck_defined_tasks`
  (internal DB helper for DEFINED tasks; a new helper for
  ASSIGNED/IN_PROGRESS would sit alongside it)
