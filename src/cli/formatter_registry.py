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
        format_agent_table,
        format_hook_run_table,
        format_hook_table,
        format_project_table,
        format_task_detail,
        format_task_table,
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

    # -- Project commands ----------------------------------------------------

    FORMATTERS["list_projects"] = FormatterSpec(
        render=format_project_table,
        extract="projects",
        proxy=project_proxy,
        many=True,
        empty_message="No projects found.",
    )


_register_all()
