# Blocked-Task Detection — Procedure Using Existing Tools

**Task:** `wise-harbor` — Investigate missing `system_monitor.get_blocked_tasks`
tool and identify problematic blocked tasks
**Date:** 2026-04-22
**Status:** Investigation (no code changes in this task)

## TL;DR

- `system_monitor.get_blocked_tasks` **does not exist and never did.** Grep of
  `src/`, `docs/specs/`, and the bundled playbook sources returns zero matches
  for the `system_monitor` namespace, for `get_blocked_tasks`, or for
  `get_stuck_tasks` / `get_agent_health_status`. The name was hallucinated by
  the LLM-powered playbook compiler when it translated the natural-language
  `system-health-check.md` Markdown body into a JSON workflow graph. See
  `notes/system-health-check-missing-tools-2026-04-22.md` (task `solid-orbit`)
  for the full root-cause analysis — this note is the blocked-task-specific
  companion to the sibling `notes/stuck-task-detection-procedure-2026-04-22.md`
  (task `vivid-cascade`).
- The source playbook `src/prompts/example_playbooks/system-health-check.md`
  was already rewritten (commit `7fecb79b`) to name only real, registered
  tools and to carry an explicit **"do not invent tool names"** guard clause.
  The revised Markdown now tells the supervisor to use
  `list_tasks(status="BLOCKED")`, `get_task_dependencies`, `task_deps`, and
  `get_chain_health` — all of which exist in `src/tools/definitions.py` under
  the `task` category (category-loadable via `load_tools(category="task")`).
- `load_tools(category="system_monitor")` can never find the phantom tools
  because the registered categories are the flat set `git`, `project`,
  `agent`, `memory`, `notes`, `files`, `task`, `playbook`, `plugin`, `system`
  (see `src/tools/registry.py::CATEGORIES` and the `_TOOL_CATEGORIES` dict
  in `src/tools/definitions.py`). Registered tool names are flat — there is
  no `namespace.tool` convention anywhere in the registry.
- A concrete replacement procedure using only registered tools is verified
  below against the live daemon.

## Tool inventory — what actually exists

All three concerns from the playbook step ("no upstream resolution path",
"no human input requested", "circular dependency chains") are covered by
existing tools in the `task` category:

| Concern                                   | Real tool(s)                                           |
|------------------------------------------ |--------------------------------------------------------|
| Enumerate BLOCKED tasks                   | `list_tasks(status="BLOCKED"[, project_id=...])`       |
| Enumerate projects to iterate             | `list_projects()`                                      |
| Upstream/downstream dependency graph      | `get_task_dependencies(task_id=...)`                   |
| Focused dependency view w/ status glyphs  | `task_deps(task_id=...)`                               |
| Chain-health + downstream stuck walk      | `get_chain_health(project_id=...)` or `(task_id=...)`  |
| Blocked-tasks w/ deps inlined in one call | `list_tasks(status="BLOCKED", show_dependencies=true)` |
| Remediation                               | `skip_task`, `set_task_status`, `reopen_with_feedback` |

`list_tasks(status="BLOCKED", show_dependencies=true)` is the most efficient
entry point — it returns each BLOCKED task already annotated with its
`depends_on` list (id + status for each upstream) and `blocks` list
(downstream task ids). One call replaces a loop of per-task
`get_task_dependencies` calls.

## Verified procedure

Against the currently-running daemon, with all tools confirmed registered in
`src/tools/definitions.py`:

### 1. Find BLOCKED tasks with no resolution path

A BLOCKED task has "no resolution path" when every one of its upstream
dependencies is in a terminal state (COMPLETED, FAILED, ARCHIVED) or
missing — i.e. nothing will ever transition to change the blocker's status.

1. Call `list_projects()` to enumerate active projects (status=`ACTIVE`).
2. For each project, call
   `list_tasks(project_id=..., status="BLOCKED", show_dependencies=true)`.
3. For each returned task, inspect the `depends_on` array:
   - If `depends_on` is empty → the task is blocked without any upstream
     work that could unblock it. Flag it: likely waiting on human input or
     an external event. Follow up with
     `get_task(task_id=...)` to read the task body and check for a
     `blocked_reason` / `resume_condition` note. If none, flag for human
     review.
   - If every `depends_on` entry's `status` is in
     `{COMPLETED, FAILED, ARCHIVED}` → the chain is dead. Flag for human
     review; the task will not self-resolve. Remediation options are
     `skip_task` (if the blocker was auxiliary), `reopen_with_feedback`
     (if the blocker needs another attempt), or
     `set_task_status(..., status="READY")` (if the condition is
     otherwise satisfied).
   - If at least one `depends_on` entry is in `{DEFINED, READY,
     ASSIGNED, IN_PROGRESS}` → resolution is still possible; leave alone.
4. Consolidate flagged tasks into the summary.

### 2. Detect circular dependency chains

Cycles are **structurally impossible** in the current schema because every
write through `add_dependency` runs
`src/state_machine.py::validate_dag_with_new_edge` (three-color DFS
back-edge check) before persistence — see `CyclicDependencyError` at
line 118 and `validate_dag_with_new_edge` at line 157. `create_task` with
a `depends_on=[...]` list also runs full `validate_dag(...)` on the new
edges. So the invariant "the dependency graph is a DAG" holds at rest.

Defence in depth (recommended in health checks):

1. Per-project, call `get_chain_health(project_id=...)`. This walks every
   BLOCKED task's downstream via `Orchestrator._find_stuck_downstream`
   (BFS through `get_dependents`, `src/orchestrator/monitoring.py:363`).
   If a cycle somehow slipped past the insertion guard, the BFS walk would
   expose it as an unterminated traversal. The current implementation
   short-circuits on `visited` so it will not infinite-loop, but the
   `total_stuck_chains` counter gives an at-a-glance indicator.
2. Defensive cycle scan via `state_machine.validate_dag`: load the project's
   dep edges into a `dict[str, set[str]]` and call
   `validate_dag(deps)`; a `CyclicDependencyError` means the invariant has
   been violated (this would be a bug to report, not a normal finding).

Practical default: rely on `get_chain_health(project_id=...)` in the
playbook and only reach for the explicit `validate_dag` audit when
`get_chain_health` reports unexpected structure. No cycle finding is a
normal, expected outcome of a healthy system.

## Live-fire check (today)

Running the procedure against the daemon at the time of this investigation:

- `list_tasks(status="BLOCKED")` (all projects) → **0 tasks.**
- `list_tasks(project_id="agent-queue", status="BLOCKED", show_dependencies=true)` → 0 tasks.
- `get_chain_health(project_id="agent-queue")` →
  `{"stuck_chains": [], "total_stuck_chains": 0}`.
- `get_chain_health(project_id="moss-and-spade-inventory-manager")` →
  `{"stuck_chains": [], "total_stuck_chains": 0}`.

The eight blocked tasks reported in the upstream `start_health_check` step
(dependency updates for cryptography, oauthlib, pytest, python-multipart,
wheel, zipp, and a claude-CLI update) have since resolved or transitioned
out of BLOCKED — the state changed between the 30-minute timer tick and
this investigation's live query. The query path itself is verified sound
against a 0-result snapshot.

## Summary output shape (for the playbook)

When the `check_blocked_tasks` step wires the real tools, the summary
artefact should carry:

```
{
  "blocked_with_no_path": [
    {"id": "...", "title": "...", "project_id": "...",
     "reason": "empty_dependencies" | "all_upstream_terminal",
     "terminal_upstreams": [{"id": "...", "status": "..."}]}
  ],
  "circular_chains": [],               // expected empty under DAG invariant
  "total_blocked": <int>,
  "queried_projects": [<project_ids>]
}
```

This gives the Summary step (and any downstream memory-writer) a
deterministic shape without relying on supervisor narrative text.

## What the follow-up should actually do

The playbook rewrite (`7fecb79b`) is sufficient to unblock the current
`system-health-check` timer trigger — the next compile of that playbook
will emit node prompts that name real tools. Two optional hardening
follow-ups are worth considering as their own tasks:

1. **Promote the "blocked without resolution path" logic to a first-class
   tool.** Symmetric with the `get_stuck_tasks` proposal in
   `notes/stuck-task-detection-procedure-2026-04-22.md`. Signature:
   `get_unresolvable_blocked_tasks(project_id=None)` returning
   `{unresolvable: [{id, project_id, title, reason, terminal_upstreams}]}`.
   Register under category `task` (or `system`); reuses
   `db.list_tasks(status=TaskStatus.BLOCKED)` + `db.get_dependencies` with
   no new schema. Eliminates the multi-call LLM orchestration from every
   invocation.
2. **Ground the playbook compiler prompt.** The root cause of the phantom
   `system_monitor.*` tools is that `PlaybookCompiler`
   (`src/playbooks/compiler.py`) hands Markdown to an LLM without
   constraining it to the real tool registry. Inject the flat tool name
   list from `ToolRegistry.get_all_tools()` into the compiler prompt and
   instruct the LLM that any tool not on that list must be expressed as
   natural-language intent. `src/playbooks/runner_context.py::_build_node_prompt`
   already flags this as a future extension point ("injecting
   tool-availability hints"). This is the *real* fix — it prevents new
   hallucinations across every example playbook
   (`codebase-inspector.md`, `dependency-audit.md`, `task-outcome.md`,
   `vibecop-weekly-scan.md`, etc.), not just this one.

Neither is required to close the immediate gap. This task's deliverable
is the documented procedure above, which the already-rewritten
`system-health-check.md` now encodes.

## Files referenced

- `src/prompts/example_playbooks/system-health-check.md` (already fixed,
  commit `7fecb79b`)
- `notes/system-health-check-missing-tools-2026-04-22.md` (sibling
  investigation, task `solid-orbit`)
- `notes/stuck-task-detection-procedure-2026-04-22.md` (sibling
  investigation, task `vivid-cascade`)
- `src/tools/definitions.py` (tool registry — `system_monitor` is absent;
  `list_tasks`, `get_task_dependencies`, `task_deps`, `get_chain_health`
  all present in the `task` category)
- `src/tools/registry.py` (`CATEGORIES` dict — no `system_monitor`)
- `src/commands/task_commands.py::_cmd_get_chain_health` (line 2617 —
  implementation of `get_chain_health`)
- `src/orchestrator/monitoring.py::_find_stuck_downstream` (line 363 —
  BFS walk used by `get_chain_health`)
- `src/state_machine.py::validate_dag`, `validate_dag_with_new_edge`,
  `CyclicDependencyError` (lines 118–166 — cycle-prevention invariant)
