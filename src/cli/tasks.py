"""Hand-crafted task CLI commands that require interactive features.

Simple list/detail commands are auto-generated with Rich formatters via
the formatter registry.  This file only contains commands that need
interactive prompts (wizard, confirmation dialogs, fuzzy search).
"""

from __future__ import annotations

from typing import Any

import click

from .app import cli, console, _run, _get_client, _handle_errors
from .envelope import emit


def _getval(obj: Any, key: str, default: Any = None) -> Any:
    """Get a value from a typed response or dict, normalising Unset → default."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    val = getattr(obj, key, default)
    if type(val).__name__ == "Unset":
        return default
    return val


@cli.group()
def task() -> None:
    """Task management commands."""
    pass


@task.command("create")
@click.option("-p", "--project", default=None, help="Project ID (skips wizard step)")
@click.option("-t", "--title", default=None, help="Task title (skips wizard step)")
@click.option("-d", "--description", default=None, help="Task description")
@click.option("--priority", default=None, type=int, help="Priority (1-300)")
@click.option("--type", "task_type", default=None, help="Task type")
@click.option("--approval/--no-approval", default=False, help="Require approval")
@click.option(
    "-P",
    "--profile",
    "profile_id",
    default=None,
    help="Agent profile id (e.g. claude-opus, claude-sonnet, claude-code)",
)
@click.option(
    "--agent-type",
    default=None,
    help="Agent type override (cascade falls back to the global profile of this name)",
)
@click.pass_context
@_handle_errors
def task_create(
    ctx: click.Context,
    project: str | None,
    title: str | None,
    description: str | None,
    priority: int | None,
    task_type: str | None,
    approval: bool,
    profile_id: str | None,
    agent_type: str | None,
) -> None:
    """Create a new task (interactive wizard or via flags).

    Use ``--profile`` / ``-P`` to pin a specific agent profile (model +
    tools + system prompt). Use ``--agent-type`` to pick the scope the
    task runs under when no explicit profile is given.
    """
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    if project and title and description:
        params = {
            "project_id": project,
            "title": title,
            "description": description,
            "priority": priority or 100,
            "task_type": task_type,
            "requires_approval": approval,
        }
        if profile_id:
            params["profile_id"] = profile_id
        if agent_type:
            params["agent_type"] = agent_type
    else:
        from .menus import task_creation_wizard

        async def _get_projects():
            async with _get_client(api_url) as client:
                result = await client.execute("list_projects")
                projects = _getval(result, "projects", [])
                return [_getval(p, "id") for p in projects]

        project_ids = _run(_get_projects())
        params = task_creation_wizard(project_ids)
        if not params:
            console.print("[dim]Task creation cancelled.[/]")
            return
        # CLI flag overrides persist through the wizard if the caller
        # mixed interactive + flag usage.
        if profile_id and "profile_id" not in params:
            params["profile_id"] = profile_id
        if agent_type and "agent_type" not in params:
            params["agent_type"] = agent_type

    async def _create():
        async with _get_client(api_url) as client:
            return await client.execute("create_task", params)

    result = _run(_create())
    task_id = _getval(result, "created", "?")
    console.print()
    console.print(f"[bold green]Task created:[/] [bold bright_cyan]{task_id}[/]")
    title = _getval(result, "title")
    if title:
        console.print(f"  [dim]{title}[/]")


@task.command("approve")
@click.argument("task_id")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@click.pass_context
@_handle_errors
def task_approve(ctx: click.Context, task_id: str, yes: bool) -> None:
    """Approve a task for execution."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    if not yes:
        from .menus import confirm

        if not confirm(f"Approve task '{task_id}'?"):
            console.print("[dim]Cancelled.[/]")
            return

    async def _approve():
        async with _get_client(api_url) as client:
            return await client.execute("approve_task", {"task_id": task_id})

    result = _run(_approve())
    console.print(f"[bold green]Task approved:[/] {_getval(result, 'approved', task_id)}")


@task.command("stop")
@click.argument("task_id")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@click.pass_context
@_handle_errors
def task_stop(ctx: click.Context, task_id: str, yes: bool) -> None:
    """Stop a running task."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    if not yes:
        from .menus import confirm

        if not confirm(f"Stop task '{task_id}'? This will mark it as FAILED."):
            console.print("[dim]Cancelled.[/]")
            return

    async def _stop():
        async with _get_client(api_url) as client:
            return await client.execute("stop_task", {"task_id": task_id})

    result = _run(_stop())
    console.print(f"[bold yellow]Task stopped:[/] {_getval(result, 'stopped', task_id)}")


@task.command("restart")
@click.argument("task_id")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@click.pass_context
@_handle_errors
def task_restart(ctx: click.Context, task_id: str, yes: bool) -> None:
    """Restart a failed or stopped task."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    if not yes:
        from .menus import confirm

        if not confirm(f"Restart task '{task_id}'?"):
            console.print("[dim]Cancelled.[/]")
            return

    async def _restart():
        async with _get_client(api_url) as client:
            return await client.execute("restart_task", {"task_id": task_id})

    result = _run(_restart())
    console.print(f"[bold green]Task restarted:[/] {_getval(result, 'restarted', task_id)}")


@task.command("search")
@click.argument("query")
@click.option("-p", "--project", default=None, help="Limit search to project")
@click.pass_context
@_handle_errors
def task_search(ctx: click.Context, query: str, project: str | None) -> None:
    """Search tasks by title or description."""
    from .adapters import task_proxy
    from .formatters import format_task_table

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _search():
        async with _get_client(api_url) as client:
            args = {"include_completed": True}
            if project:
                args["project_id"] = project
            return await client.execute("list_tasks", args)

    result = _run(_search())

    q = query.lower()
    raw_tasks = _getval(result, "tasks", [])
    matched = [
        t
        for t in raw_tasks
        if q in (_getval(t, "title", "")).lower() or q in (_getval(t, "description", "")).lower()
    ]
    tasks = [task_proxy(t) for t in matched]

    title = f"Search results for '{query}'"
    if project:
        title += f" in {project}"

    table = format_task_table(tasks, title=title)
    console.print(table)

    if not tasks:
        console.print("[dim]No tasks matched your search.[/]")


@task.command("select")
@click.option("-p", "--project", default=None, help="Filter by project")
@click.pass_context
@_handle_errors
def task_select(ctx: click.Context, project: str | None) -> None:
    """Interactively select a task and show its details."""
    from .adapters import task_proxy
    from .formatters import format_task_detail
    from .menus import fuzzy_select_task

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _select():
        async with _get_client(api_url) as client:
            args = {}
            if project:
                args["project_id"] = project
            result = await client.execute("list_tasks", args)
            raw_tasks = _getval(result, "tasks", [])
            tasks = [task_proxy(t) for t in raw_tasks]

            if not tasks:
                console.print("[dim]No active tasks found.[/]")
                return

            selected = fuzzy_select_task(tasks, prompt_text="Select task (ID or search): ")
            if not selected:
                console.print("[dim]No task selected.[/]")
                return

            detail = await client.execute("get_task", {"task_id": selected.id})
            t = task_proxy(detail)
            deps_raw = _getval(detail, "depends_on", [])
            blocks_raw = _getval(detail, "blocks", [])
            deps_on = [d.id if hasattr(d, "id") else d["id"] for d in deps_raw]
            dependents = [d.id if hasattr(d, "id") else d["id"] for d in blocks_raw]
            panel = format_task_detail(t, deps_on=deps_on, dependents=dependents)
            console.print(panel)

    _run(_select())


# ---------------------------------------------------------------------------
# show / set / list / details — aq-surface Phase S0 (output contract)
#
# Hand-crafted (not auto-generated) so they can route through `emit()` for
# the versioned JSON envelope + --brief projection. `list` shadows the
# auto-generated `list_tasks`-backed command (same backend command, nicer
# front end); `show`/`details` are new, backed by the new `task_show`
# CommandHandler command; `set` is new, backed by `task_set`. See
# docs/specs/implementation/aq-surface.md §5.3 / §9.
# ---------------------------------------------------------------------------


@task.command("list")
@click.option("-p", "--project", "project_id", default=None, help="Filter by project")
@click.option("--status", default=None, help="Filter by status (see `aq schema`)")
@click.option(
    "--include-completed",
    is_flag=True,
    default=False,
    help="Include COMPLETED/FAILED/BLOCKED tasks (hidden by default)",
)
@click.pass_context
@_handle_errors
def task_list(
    ctx: click.Context,
    project_id: str | None,
    status: str | None,
    include_completed: bool,
) -> None:
    """List tasks."""
    from .adapters import task_proxy
    from .formatters import format_task_table

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _list():
        async with _get_client(api_url) as client:
            args: dict[str, Any] = {}
            if project_id:
                args["project_id"] = project_id
            if status:
                args["status"] = status
            if include_completed:
                args["include_completed"] = True
            return await client.execute("list_tasks", args)

    result = _run(_list())
    raw_tasks = _getval(result, "tasks", [])
    total = _getval(result, "total", len(raw_tasks))

    def _render(data: list) -> None:
        proxied = [task_proxy(t) for t in data]
        table = format_task_table(proxied)
        console.print(table)
        if not proxied:
            console.print("[dim]No tasks found.[/]")

    emit(ctx, raw_tasks, entity="task", total=total, render=_render)


@task.command("show")
@click.argument("task_id")
@click.pass_context
@_handle_errors
def task_show(ctx: click.Context, task_id: str) -> None:
    """Show full task detail: fields, dependencies, context, labels."""
    from .adapters import task_proxy
    from .formatters import format_task_detail

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _show():
        async with _get_client(api_url) as client:
            return await client.execute("task_show", {"task_id": task_id})

    result = _run(_show())

    def _render(data: dict) -> None:
        t = task_proxy(data)
        deps_raw = _getval(data, "depends_on", [])
        blocks_raw = _getval(data, "blocks", [])
        deps_on = [d.get("id") if isinstance(d, dict) else d for d in deps_raw]
        dependents = [d.get("id") if isinstance(d, dict) else d for d in blocks_raw]
        panel = format_task_detail(t, deps_on=deps_on, dependents=dependents)
        console.print(panel)

        labels = _getval(data, "labels", [])
        if labels:
            console.print(f"[dim]Labels:[/] {', '.join(labels)}")
        context = _getval(data, "context", [])
        if context:
            console.print(f"[dim]Context entries:[/] {len(context)}")

    emit(ctx, result, entity="task", render=_render)


@task.command("details")
@click.argument("task_id")
@click.pass_context
def task_details_alias(ctx: click.Context, task_id: str) -> None:
    """Alias of `aq task show` (kept for backward compatibility)."""
    ctx.invoke(task_show, task_id=task_id)


@task.command("set")
@click.argument("task_id")
@click.option("--branch", default=None, help="Set the task's branch name")
@click.option("--pr-url", default=None, help="Set the task's PR URL")
@click.option("--work-dir", default=None, help="Record the task's working directory")
@click.option("--note", default=None, help="Append a note to the task's context")
@click.option(
    "--label",
    "labels",
    multiple=True,
    metavar="+LABEL|-LABEL",
    help="Add (+label) or remove (-label) a label; repeatable.",
)
@click.option(
    "--meta",
    "meta_kv",
    multiple=True,
    metavar="KEY=VALUE",
    help="Set a metadata key; repeatable.",
)
@click.pass_context
@_handle_errors
def task_set(
    ctx: click.Context,
    task_id: str,
    branch: str | None,
    pr_url: str | None,
    work_dir: str | None,
    note: str | None,
    labels: tuple[str, ...],
    meta_kv: tuple[str, ...],
) -> None:
    """Work-state contract writes: branch, PR URL, work dir, notes, labels, metadata.

    Never changes task status — use `aq task approve|stop|restart` for that.
    """
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    labels_add: list[str] = []
    labels_remove: list[str] = []
    for entry in labels:
        if entry.startswith("-"):
            labels_remove.append(entry[1:])
        elif entry.startswith("+"):
            labels_add.append(entry[1:])
        else:
            labels_add.append(entry)

    meta: dict[str, str] = {}
    for kv in meta_kv:
        if "=" not in kv:
            console.print(f"[bold red]Error:[/] --meta expects KEY=VALUE, got '{kv}'")
            raise SystemExit(2)
        key, _, value = kv.partition("=")
        meta[key] = value

    args: dict[str, Any] = {"task_id": task_id}
    if branch is not None:
        args["branch"] = branch
    if pr_url is not None:
        args["pr_url"] = pr_url
    if work_dir is not None:
        args["work_dir"] = work_dir
    if note is not None:
        args["note"] = note
    if labels_add:
        args["labels_add"] = labels_add
    if labels_remove:
        args["labels_remove"] = labels_remove
    if meta:
        args["meta"] = meta

    async def _set():
        async with _get_client(api_url) as client:
            return await client.execute("task_set", args)

    result = _run(_set())

    def _render(data: dict) -> None:
        changed = _getval(data, "fields_changed", [])
        console.print(f"[bold green]Task updated:[/] {task_id}")
        if changed:
            console.print(f"  [dim]Fields:[/] {', '.join(changed)}")

    emit(ctx, result, entity="task", render=_render)
