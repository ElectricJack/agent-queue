# System Health Check — Missing `system_monitor.*` Tools (Investigation)

**Task:** `solid-orbit` — Investigate missing `system_monitor` tools for
health-check playbook
**Date:** 2026-04-22

## Observed failure

The `system-health-check` playbook (trigger `timer.30m`, scope `system`)
failed three consecutive steps:

- `stuck_tasks_identified` — "the `system_monitor` category is not
  available in my tool list"
- `blocked_tasks_found` — same error
- `agent_health_issues_found` — same error

At each step the executing supervisor attempted to call
`system_monitor.get_stuck_tasks`, `system_monitor.get_blocked_tasks`,
and `system_monitor.get_agent_health_status`, then tried
`load_tools(category="system_monitor")`. Both approaches failed.

## Root cause

The `system_monitor` namespace **does not exist anywhere in the
codebase**. Grep of `src/`, `docs/specs/`, and the bundled playbook
sources returned zero matches for `system_monitor`,
`get_stuck_tasks`, `get_blocked_tasks`, or `get_agent_health_status`.

The registered tool categories (`src/tools/registry.py`, `CATEGORIES`
dict, plus category tags in `src/tools/definitions.py`) are:
`git`, `project`, `agent`, `memory`, `notes`, `files`, `task`,
`playbook`, `plugin`, `system`.

Tool names in the registry are **flat** (e.g. `list_tasks`,
`list_agents`, `get_chain_health`). No dotted-namespace tool name
exists — `load_tools(category="system_monitor")` will never find one.

### Where the phantom tool came from

`src/prompts/example_playbooks/system-health-check.md` is written in
**natural-language Markdown** and names no tools. The compiler,
`PlaybookCompiler` (`src/playbooks/compiler.py`), is an LLM-powered
one-shot translator: it hands the Markdown to an LLM and asks it to
emit a JSON workflow graph whose non-terminal nodes carry a `prompt`
instruction. The compiler prompt does **not** constrain the LLM to
the real tool registry.

When the markdown body said "Look for tasks in ASSIGNED or IN_PROGRESS
status…" with no tool named, the compiler's LLM **hallucinated** a
plausible-sounding dotted name (`system_monitor.get_stuck_tasks`) and
baked it into the compiled node prompt. The runner then executed the
hallucinated prompt verbatim. The supervisor tried to honor it, could
not find the tool, and gave up.

This was previously diagnosed at the per-tool level by two separate
investigation tasks:

- `brisk-meadow` — `system_monitor.get_stuck_tasks` is phantom.
- `fleet-apex` — `system_monitor.get_agent_health_status` is phantom.

Both landed insights in project/agent-type memory. This task rolls
those into a single actionable fix.

## Fix applied

Rewrote `src/prompts/example_playbooks/system-health-check.md` to
name real, registered tools in every step. The rewrite follows the
same pattern that makes the newer `system-pulse.md` playbook
compile cleanly — explicit tool names, an explicit tool contract,
and a "do not invent tool names" guard clause.

Concrete tool mapping now encoded in the playbook:

| Section        | Real tools used                                            |
|----------------|------------------------------------------------------------|
| Stuck tasks    | `list_tasks(status=...)`, `list_active_tasks_all_projects`, `list_agents`, `restart_task`, `set_task_status` |
| Blocked tasks  | `list_tasks(status="BLOCKED")`, `get_task_dependencies`, `task_deps`, `get_chain_health` |
| Agent health   | `list_agents`, `list_workspaces`, `get_recent_events`, `get_agent_error`, `get_status` |

A compile-time re-run of the playbook will now yield node prompts
that reference tools that actually exist. No changes to the tool
registry or category definitions are required — the "missing" tools
were fabricated, not deleted.

## Broader issue (out of scope for this task)

Every file under `src/prompts/example_playbooks/` is susceptible to
the same LLM-compiler hallucination. Grep showed that none of
`codebase-inspector.md`, `dependency-audit.md`, `task-outcome.md`, or
`vibecop-weekly-scan.md` name any of the common tools (`list_tasks`,
`list_agents`, `get_chain_health`, `list_workspaces`,
`get_recent_events`). They rely on the compiler's LLM to pick
appropriate tool names, and the compiler's LLM can invent names.

Two longer-term fixes are worth considering as separate work:

1. **Ground the compiler prompt.** Feed `PlaybookCompiler` the flat
   list of real tool names (from
   `ToolRegistry.get_all_tools()`) and instruct it that any tool not
   on that list must be expressed as natural-language intent.
   `src/playbooks/runner_context.py::_build_node_prompt` already
   flags this as a future extension point ("injecting tool-availability
   hints").
2. **Runtime fallback.** When a compiled step names a tool that isn't
   in the active or loadable tool set, have the runner/supervisor
   fall back to the natural-language intent of the step rather than
   abort the node.

Neither is required to unblock this playbook — the source rewrite
closes the immediate gap.

## Files changed

- `src/prompts/example_playbooks/system-health-check.md` — rewrote
  with explicit tool references and a "do not invent tool names"
  guard clause.
