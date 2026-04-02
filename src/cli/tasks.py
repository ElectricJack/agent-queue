"""Task management CLI commands (aq task ...)."""

from __future__ import annotations

import click

from .app import cli, console, _run, _get_client


@cli.group()
def task() -> None:
    """Task management commands."""
    pass


@task.command("list")
@click.option("-p", "--project", default=None, help="Filter by project ID")
@click.option(
    "-s", "--status", "status_filter",
    default=None,
    type=click.Choice([
        "DEFINED", "READY", "ASSIGNED", "IN_PROGRESS", "WAITING_INPUT",
        "PAUSED", "VERIFYING", "AWAITING_APPROVAL", "AWAITING_PLAN_APPROVAL",
        "COMPLETED", "FAILED", "BLOCKED",
    ], case_sensitive=False),
    help="Filter by status",
)
@click.option("--active/--all", default=True, help="Show only active tasks (default) or all")
@click.option("--limit", default=50, help="Maximum number of tasks to display")
def task_list(
    project: str | None,
    status_filter: str | None,
    active: bool,
    limit: int,
) -> None:
    """List tasks with filtering by project, status, and activity."""
    from src.models import TaskStatus
    from .formatters import format_task_table

    async def _run_list():
        async with _get_client() as client:
            status = TaskStatus(status_filter) if status_filter else None
            if status:
                tasks = await client.list_tasks(project_id=project, status=status)
            elif active:
                tasks = await client.list_tasks(project_id=project, active_only=True)
            else:
                tasks = await client.list_tasks(project_id=project)
            return tasks

    try:
        tasks = _run(_run_list())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    # Sort by priority (highest first), then by status
    status_order = {
        "IN_PROGRESS": 0, "WAITING_INPUT": 1, "ASSIGNED": 2, "READY": 3,
        "AWAITING_APPROVAL": 4, "AWAITING_PLAN_APPROVAL": 5, "VERIFYING": 6,
        "DEFINED": 7, "BLOCKED": 8, "PAUSED": 9, "FAILED": 10, "COMPLETED": 11,
    }
    tasks.sort(key=lambda t: (status_order.get(t.status.value, 99), -t.priority))

    displayed = tasks[:limit]

    title_parts = ["Tasks"]
    if project:
        title_parts.append(f"project={project}")
    if status_filter:
        title_parts.append(f"status={status_filter}")
    elif active:
        title_parts.append("active only")
    title = " — ".join(title_parts)

    table = format_task_table(displayed, title=title)
    console.print(table)

    if len(tasks) > limit:
        console.print(
            f"\n[dim]Showing {limit} of {len(tasks)} tasks. "
            f"Use --limit to see more.[/]"
        )
    elif not tasks:
        console.print("[dim]No tasks found matching filters.[/]")


@task.command("details")
@click.argument("task_id")
def task_details(task_id: str) -> None:
    """Show complete details for a task."""
    from .formatters import format_task_detail

    async def _run_details():
        async with _get_client() as client:
            t = await client.get_task(task_id)
            if not t:
                return None, None, None, None
            deps_on = await client.get_task_dependencies(task_id)
            dependents = await client.get_task_dependents(task_id)

            # Get subtask stats if this is a parent task
            subtask_stats = None
            tree = await client.get_task_tree(task_id)
            if tree and tree.get("children"):
                children = tree["children"]
                total = len(children)
                from src.models import TaskStatus
                completed = sum(
                    1 for c in children if c["task"].status == TaskStatus.COMPLETED
                )
                subtask_stats = (completed, total)

            return t, deps_on, dependents, subtask_stats

    try:
        t, deps_on, dependents, subtask_stats = _run(_run_details())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not t:
        console.print(f"[bold red]Task not found:[/] {task_id}")
        raise SystemExit(1)

    panel = format_task_detail(t, deps_on=deps_on, dependents=dependents, subtask_stats=subtask_stats)
    console.print(panel)


@task.command("create")
@click.option("-p", "--project", default=None, help="Project ID (skips wizard step)")
@click.option("-t", "--title", default=None, help="Task title (skips wizard step)")
@click.option("-d", "--description", default=None, help="Task description")
@click.option("--priority", default=None, type=int, help="Priority (1-300)")
@click.option("--type", "task_type", default=None, help="Task type")
@click.option("--approval/--no-approval", default=False, help="Require approval")
def task_create(
    project: str | None,
    title: str | None,
    description: str | None,
    priority: int | None,
    task_type: str | None,
    approval: bool,
) -> None:
    """Create a new task (interactive wizard or via flags)."""
    from .formatters import format_task_detail

    # If all required fields provided, skip wizard
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
        # Interactive wizard
        from .menus import task_creation_wizard

        async def _get_projects():
            async with _get_client() as client:
                projects = await client.list_projects()
                return [p.id for p in projects]

        try:
            project_ids = _run(_get_projects())
        except FileNotFoundError as e:
            console.print(f"[bold red]Error:[/] {e}")
            raise SystemExit(1)

        params = task_creation_wizard(project_ids)
        if not params:
            console.print("[dim]Task creation cancelled.[/]")
            return

    async def _create():
        async with _get_client() as client:
            return await client.create_task(**params)

    try:
        new_task = _run(_create())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)
    except Exception as e:
        console.print(f"[bold red]Error creating task:[/] {e}")
        raise SystemExit(1)

    console.print()
    console.print(f"[bold green]Task created:[/] [bold bright_cyan]{new_task.id}[/]")
    panel = format_task_detail(new_task)
    console.print(panel)


@task.command("approve")
@click.argument("task_id")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def task_approve(task_id: str, yes: bool) -> None:
    """Approve a task for execution."""

    async def _approve():
        async with _get_client() as client:
            t = await client.get_task(task_id)
            if not t:
                console.print(f"[bold red]Task not found:[/] {task_id}")
                raise SystemExit(1)

            if not yes:
                from .menus import confirm
                if not confirm(f"Approve task '{t.title}' ({task_id})?"):
                    console.print("[dim]Cancelled.[/]")
                    return

            await client.approve_task(task_id)
            console.print(f"[bold green]Task approved:[/] {task_id}")

    try:
        _run(_approve())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)


@task.command("stop")
@click.argument("task_id")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def task_stop(task_id: str, yes: bool) -> None:
    """Stop a running task."""

    async def _stop():
        async with _get_client() as client:
            t = await client.get_task(task_id)
            if not t:
                console.print(f"[bold red]Task not found:[/] {task_id}")
                raise SystemExit(1)

            if not yes:
                from .menus import confirm
                if not confirm(f"Stop task '{t.title}' ({task_id})? This will mark it as FAILED."):
                    console.print("[dim]Cancelled.[/]")
                    return

            await client.stop_task(task_id)
            console.print(f"[bold yellow]Task stopped:[/] {task_id}")

    try:
        _run(_stop())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)


@task.command("restart")
@click.argument("task_id")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def task_restart(task_id: str, yes: bool) -> None:
    """Restart a failed or stopped task."""

    async def _restart():
        async with _get_client() as client:
            t = await client.get_task(task_id)
            if not t:
                console.print(f"[bold red]Task not found:[/] {task_id}")
                raise SystemExit(1)

            if not yes:
                from .menus import confirm
                if not confirm(f"Restart task '{t.title}' ({task_id})?"):
                    console.print("[dim]Cancelled.[/]")
                    return

            await client.restart_task(task_id)
            console.print(f"[bold green]Task restarted:[/] {task_id}")

    try:
        _run(_restart())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)


@task.command("search")
@click.argument("query")
@click.option("-p", "--project", default=None, help="Limit search to project")
def task_search(query: str, project: str | None) -> None:
    """Search tasks by title or description."""
    from .formatters import format_task_table

    async def _search():
        async with _get_client() as client:
            return await client.search_tasks(query, project_id=project)

    try:
        tasks = _run(_search())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    title = f"Search results for '{query}'"
    if project:
        title += f" in {project}"

    table = format_task_table(tasks, title=title)
    console.print(table)

    if not tasks:
        console.print("[dim]No tasks matched your search.[/]")


@task.command("select")
@click.option("-p", "--project", default=None, help="Filter by project")
def task_select(project: str | None) -> None:
    """Interactively select a task and show its details."""
    from .menus import fuzzy_select_task
    from .formatters import format_task_detail

    async def _select():
        async with _get_client() as client:
            tasks = await client.list_tasks(project_id=project, active_only=True)
            if not tasks:
                console.print("[dim]No active tasks found.[/]")
                return

            selected = fuzzy_select_task(tasks, prompt_text="Select task (ID or search): ")
            if not selected:
                console.print("[dim]No task selected.[/]")
                return

            deps_on = await client.get_task_dependencies(selected.id)
            dependents = await client.get_task_dependents(selected.id)
            panel = format_task_detail(selected, deps_on=deps_on, dependents=dependents)
            console.print(panel)

    try:
        _run(_select())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)
