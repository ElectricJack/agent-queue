"""Hand-crafted task CLI commands that require interactive features.

Simple list/detail commands are auto-generated with Rich formatters via
the formatter registry.  This file only contains commands that need
interactive prompts (wizard, confirmation dialogs, fuzzy search).
"""

from __future__ import annotations

import click

from .app import cli, console, _run, _get_client, _handle_errors


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
) -> None:
    """Create a new task (interactive wizard or via flags)."""
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
    else:
        from .menus import task_creation_wizard

        async def _get_projects():
            async with _get_client(api_url) as client:
                result = await client.execute("list_projects")
                return [p["id"] for p in result.get("projects", [])]

        project_ids = _run(_get_projects())
        params = task_creation_wizard(project_ids)
        if not params:
            console.print("[dim]Task creation cancelled.[/]")
            return

    async def _create():
        async with _get_client(api_url) as client:
            return await client.execute("create_task", params)

    result = _run(_create())
    task_id = result.get("created", "?")
    console.print()
    console.print(f"[bold green]Task created:[/] [bold bright_cyan]{task_id}[/]")
    if result.get("title"):
        console.print(f"  [dim]{result['title']}[/]")


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
    console.print(f"[bold green]Task approved:[/] {result.get('approved', task_id)}")


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
    console.print(f"[bold yellow]Task stopped:[/] {result.get('stopped', task_id)}")


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
    console.print(f"[bold green]Task restarted:[/] {result.get('restarted', task_id)}")


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
    raw_tasks = result.get("tasks", [])
    matched = [
        t for t in raw_tasks
        if q in (t.get("title", "")).lower() or q in (t.get("description", "")).lower()
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
            raw_tasks = result.get("tasks", [])
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
            deps_on = [d["id"] for d in detail.get("depends_on", [])]
            dependents = [d["id"] for d in detail.get("blocks", [])]
            panel = format_task_detail(t, deps_on=deps_on, dependents=dependents)
            console.print(panel)

    _run(_select())
