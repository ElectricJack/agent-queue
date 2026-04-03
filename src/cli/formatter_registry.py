"""Formatter registry: maps CommandHandler commands to Rich formatters.

Auto-generated CLI commands check this registry before falling back to
raw JSON output.  Each entry describes how to extract data from the
CommandHandler result dict, adapt it via proxy, and render it with a
Rich formatter.

Registration is done via the ``register`` decorator or direct dict
assignment, so formatters can live near the code they format.

Example::

    @formatter_for("list_tasks", extract="tasks", proxy=task_proxy, many=True)
    def _fmt(items):
        return format_task_table(items)

Or equivalently::

    FORMATTERS["list_tasks"] = FormatterSpec(
        extract="tasks", proxy=task_proxy, many=True,
        render=lambda items: format_task_table(items),
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class FormatterSpec:
    """Describes how to format a CommandHandler result for the terminal.

    Attributes:
        render: Function that takes the adapted data and returns a Rich
            renderable (Table, Panel, Group, etc.).  For ``many=True``
            this receives a list; for ``many=False`` a single object.
        extract: Key to extract from the result dict before formatting.
            ``None`` means pass the entire result dict.
        proxy: Adapter function to apply to each item (e.g. ``task_proxy``).
            ``None`` skips adaptation (raw dicts passed through).
        many: If True, the extracted data is a list and proxy is applied
            to each element.  If False, proxy is applied to the single value.
        sort_key: Optional sort function applied to the list (many=True only).
        empty_message: Message to print when the list is empty.
    """

    render: Callable[..., Any]
    extract: str | None = None
    proxy: Callable[[dict], Any] | None = None
    many: bool = True
    sort_key: Callable | None = None
    empty_message: str | None = None


# The registry: command_name → FormatterSpec
FORMATTERS: dict[str, FormatterSpec] = {}


def formatter_for(
    command: str,
    *,
    extract: str | None = None,
    proxy: Callable | None = None,
    many: bool = True,
    sort_key: Callable | None = None,
    empty_message: str | None = None,
):
    """Decorator to register a render function for a command."""

    def decorator(fn: Callable) -> Callable:
        FORMATTERS[command] = FormatterSpec(
            render=fn,
            extract=extract,
            proxy=proxy,
            many=many,
            sort_key=sort_key,
            empty_message=empty_message,
        )
        return fn

    return decorator


def apply_formatter(command: str, result: dict, console) -> bool:
    """Try to format a command's result using the registry.

    Returns True if a formatter was found and applied, False otherwise
    (caller should fall back to JSON output).
    """
    spec = FORMATTERS.get(command)
    if spec is None:
        return False

    # Extract data from result (handle both dicts and typed objects)
    if spec.extract:
        if isinstance(result, dict):
            data = result.get(spec.extract, [] if spec.many else result)
        else:
            data = getattr(result, spec.extract, [] if spec.many else result)
            # Generated client uses Unset sentinel for missing fields
            if type(data).__name__ == "Unset":
                data = [] if spec.many else result
    else:
        data = result

    # Apply proxy adapter
    if spec.proxy:
        if spec.many and isinstance(data, list):
            data = [spec.proxy(item) for item in data]
        elif not spec.many:
            data = spec.proxy(data)

    # Sort if requested
    if spec.many and spec.sort_key and isinstance(data, list):
        data.sort(key=spec.sort_key)

    # Check empty
    if spec.many and not data:
        if spec.empty_message:
            console.print(f"[dim]{spec.empty_message}[/]")
        else:
            console.print("[dim]No results.[/]")
        return True

    # Render
    renderable = spec.render(data)
    console.print(renderable)
    return True


# ---------------------------------------------------------------------------
# Register formatters for all commands with Rich output
# ---------------------------------------------------------------------------
# These live here (near the registry) rather than scattered across CLI files.
# Imports are deferred to avoid circular dependencies at module load time.


def _register_all():
    """Register all built-in formatters. Called once at import time."""
    from .adapters import (
        agent_proxy,
        hook_proxy,
        hook_run_proxy,
        project_proxy,
        task_proxy,
    )
    from .formatters import (
        format_active_tasks_all,
        format_agent_table,
        format_archived_tasks,
        format_available_tools,
        format_chain_health,
        format_confirmation,
        format_entity_detail,
        format_event_list,
        format_hook_run_table,
        format_hook_table,
        format_key_value,
        format_profile_detail,
        format_profile_list,
        format_prompt_list,
        format_project_table,
        format_rule_list,
        format_schedule_list,
        format_task_deps,
        format_task_detail,
        format_task_table,
        format_task_tree,
        format_text_content,
        format_token_usage,
        format_workspace_list,
    )

    # -- Task commands -------------------------------------------------------

    def _task_sort(t):
        return (
            {
                "IN_PROGRESS": 0,
                "WAITING_INPUT": 1,
                "ASSIGNED": 2,
                "READY": 3,
                "AWAITING_APPROVAL": 4,
                "AWAITING_PLAN_APPROVAL": 5,
                "VERIFYING": 6,
                "DEFINED": 7,
                "BLOCKED": 8,
                "PAUSED": 9,
                "FAILED": 10,
                "COMPLETED": 11,
            }.get((t.status or "").upper(), 99),
            -(t.priority or 0),
        )

    FORMATTERS["list_tasks"] = FormatterSpec(
        render=format_task_table,
        extract="tasks",
        proxy=task_proxy,
        many=True,
        sort_key=_task_sort,
        empty_message="No tasks found.",
    )

    def _render_task_detail(task):
        # get_task returns deps/blocks/subtasks inline — extract IDs for display.
        # The proxy wraps either a dict or typed model; use attribute access.
        deps_raw = task.depends_on or []
        blocks_raw = task.blocks or []
        subtasks_raw = task.subtasks or []
        deps_on = [d.id if hasattr(d, "id") else d["id"] for d in deps_raw]
        dependents = [d.id if hasattr(d, "id") else d["id"] for d in blocks_raw]
        subtask_stats = None
        if subtasks_raw:
            total = len(subtasks_raw)
            done = sum(
                1
                for s in subtasks_raw
                if (s.status if hasattr(s, "status") else s.get("status", "")).upper()
                == "COMPLETED"
            )
            subtask_stats = (done, total)
        return format_task_detail(
            task,
            deps_on=deps_on,
            dependents=dependents,
            subtask_stats=subtask_stats,
        )

    FORMATTERS["get_task"] = FormatterSpec(
        render=_render_task_detail,
        extract=None,
        proxy=task_proxy,
        many=False,
    )

    # -- Agent commands ------------------------------------------------------

    FORMATTERS["list_agents"] = FormatterSpec(
        render=format_agent_table,
        extract="agents",
        proxy=agent_proxy,
        many=True,
        empty_message="No agents found.",
    )

    # -- Hook commands -------------------------------------------------------

    FORMATTERS["list_hooks"] = FormatterSpec(
        render=format_hook_table,
        extract="hooks",
        proxy=hook_proxy,
        many=True,
        empty_message="No hooks found.",
    )

    FORMATTERS["list_hook_runs"] = FormatterSpec(
        render=format_hook_run_table,
        extract="runs",
        proxy=hook_run_proxy,
        many=True,
        empty_message="No hook runs found.",
    )

    # -- Task extra commands ---------------------------------------------------

    FORMATTERS["get_task_tree"] = FormatterSpec(
        render=format_task_tree, extract=None, many=False,
    )
    for _dep_cmd in ("task_deps", "get_task_dependencies"):
        FORMATTERS[_dep_cmd] = FormatterSpec(
            render=format_task_deps, extract=None, many=False,
        )
    FORMATTERS["list_archived"] = FormatterSpec(
        render=format_archived_tasks, extract=None, many=False,
    )
    FORMATTERS["get_chain_health"] = FormatterSpec(
        render=format_chain_health, extract=None, many=False,
    )
    FORMATTERS["list_active_tasks_all_projects"] = FormatterSpec(
        render=format_active_tasks_all, extract=None, many=False,
    )
    FORMATTERS["get_task_result"] = FormatterSpec(
        render=format_entity_detail, extract=None, many=False,
    )
    FORMATTERS["get_task_diff"] = FormatterSpec(
        render=format_text_content, extract=None, many=False,
    )
    FORMATTERS["archive_settings"] = FormatterSpec(
        render=format_key_value, extract=None, many=False,
    )

    # Task status-change confirmations
    for _task_confirm in (
        "archive_task", "archive_tasks", "restore_task", "delete_task",
        "set_task_status", "skip_task", "edit_task", "approve_plan",
        "reject_plan", "delete_plan", "process_plan", "reopen_with_feedback",
        "process_task_completion", "add_dependency", "remove_dependency",
    ):
        FORMATTERS[_task_confirm] = FormatterSpec(
            render=format_confirmation, extract=None, many=False,
        )

    # -- Agent commands ------------------------------------------------------

    FORMATTERS["list_agents"] = FormatterSpec(
        render=format_agent_table,
        extract="agents",
        proxy=agent_proxy,
        many=True,
        empty_message="No agents found.",
    )
    FORMATTERS["list_profiles"] = FormatterSpec(
        render=format_profile_list, extract=None, many=False,
    )
    FORMATTERS["get_profile"] = FormatterSpec(
        render=format_profile_detail, extract=None, many=False,
    )
    FORMATTERS["list_available_tools"] = FormatterSpec(
        render=format_available_tools, extract=None, many=False,
    )
    FORMATTERS["get_agent_error"] = FormatterSpec(
        render=format_entity_detail, extract=None, many=False,
    )

    # Agent confirmations
    for _agent_confirm in (
        "create_agent", "delete_agent", "edit_agent", "pause_agent",
        "resume_agent", "create_profile", "edit_profile", "delete_profile",
        "check_profile", "install_profile", "export_profile", "import_profile",
    ):
        FORMATTERS[_agent_confirm] = FormatterSpec(
            render=format_confirmation, extract=None, many=False,
        )

    # -- Hook/rule commands --------------------------------------------------

    FORMATTERS["list_hooks"] = FormatterSpec(
        render=format_hook_table,
        extract="hooks",
        proxy=hook_proxy,
        many=True,
        empty_message="No hooks found.",
    )

    FORMATTERS["list_hook_runs"] = FormatterSpec(
        render=format_hook_run_table,
        extract="runs",
        proxy=hook_run_proxy,
        many=True,
        empty_message="No hook runs found.",
    )

    for _rule_list_cmd in ("browse_rules", "list_rules"):
        FORMATTERS[_rule_list_cmd] = FormatterSpec(
            render=format_rule_list, extract=None, many=False,
        )
    FORMATTERS["load_rule"] = FormatterSpec(
        render=format_entity_detail, extract=None, many=False,
    )
    for _sched_cmd in ("hook_schedules", "list_scheduled"):
        FORMATTERS[_sched_cmd] = FormatterSpec(
            render=format_schedule_list, extract=None, many=False,
        )

    # Hook/rule confirmations
    for _hook_confirm in (
        "create_hook", "edit_hook", "delete_hook", "fire_hook",
        "save_rule", "delete_rule", "refresh_hooks", "schedule_hook",
        "cancel_scheduled", "fire_all_scheduled_hooks", "toggle_project_hooks",
    ):
        FORMATTERS[_hook_confirm] = FormatterSpec(
            render=format_confirmation, extract=None, many=False,
        )

    # -- Project commands ----------------------------------------------------

    FORMATTERS["list_projects"] = FormatterSpec(
        render=format_project_table,
        extract="projects",
        proxy=project_proxy,
        many=True,
        empty_message="No projects found.",
    )
    FORMATTERS["list_workspaces"] = FormatterSpec(
        render=format_workspace_list, extract=None, many=False,
    )
    FORMATTERS["get_project_channels"] = FormatterSpec(
        render=format_key_value, extract=None, many=False,
    )
    FORMATTERS["get_project_for_channel"] = FormatterSpec(
        render=format_key_value, extract=None, many=False,
    )
    FORMATTERS["find_merge_conflict_workspaces"] = FormatterSpec(
        render=format_entity_detail, extract=None, many=False,
    )

    # Project confirmations
    for _proj_confirm in (
        "create_project", "delete_project", "pause_project", "resume_project",
        "add_workspace", "remove_workspace", "release_workspace",
        "queue_sync_workspaces", "set_active_project", "set_control_interface",
    ):
        FORMATTERS[_proj_confirm] = FormatterSpec(
            render=format_confirmation, extract=None, many=False,
        )

    # -- System commands -----------------------------------------------------

    FORMATTERS["get_recent_events"] = FormatterSpec(
        render=format_event_list, extract=None, many=False,
    )
    FORMATTERS["get_token_usage"] = FormatterSpec(
        render=format_token_usage, extract=None, many=False,
    )
    FORMATTERS["claude_usage"] = FormatterSpec(
        render=format_token_usage, extract=None, many=False,
    )
    FORMATTERS["list_prompts"] = FormatterSpec(
        render=format_prompt_list, extract=None, many=False,
    )
    FORMATTERS["read_prompt"] = FormatterSpec(
        render=format_text_content, extract=None, many=False,
    )
    FORMATTERS["render_prompt"] = FormatterSpec(
        render=format_text_content, extract=None, many=False,
    )

    # System confirmations
    for _sys_confirm in ("reload_config", "orchestrator_control", "provide_input"):
        FORMATTERS[_sys_confirm] = FormatterSpec(
            render=format_confirmation, extract=None, many=False,
        )

    # -- Internal plugin formatters (auto-discovered) -------------------------

    try:
        from src.plugins.internal import collect_internal_formatters

        FORMATTERS.update(collect_internal_formatters())
    except Exception:
        pass


_register_all()
