"""Main CLI application for AgentQueue.

Provides a modern terminal interface mirroring Discord slash commands.
Uses Click for command structure and Rich for beautiful output.

Entry point: ``aq`` console script.

Usage examples::

    aq status                        # System overview
    aq task list                     # List active tasks
    aq task list --project myproj    # Filter by project
    aq task list --status FAILED     # Filter by status
    aq task create                   # Interactive creation wizard
    aq task details <id>             # Show task details
    aq task approve <id>             # Approve a task
    aq task stop <id>                # Stop a task
    aq task restart <id>             # Restart a task
    aq task search "bug fix"         # Search tasks
    aq agent list                    # List agents
    aq hook list                     # List hooks
    aq hook runs <hook_id>           # Hook execution history
    aq project list                  # List projects
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import click
from rich.console import Console

from .styles import AQ_THEME

# Create themed console
console = Console(theme=AQ_THEME)

# ---------------------------------------------------------------------------
# Async runner helper
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an existing event loop (unlikely for CLI)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def _get_client():
    """Create a CLIClient instance."""
    from .client import CLIClient
    db_path = os.environ.get("AGENT_QUEUE_DB")
    return CLIClient(db_path=db_path)


# ---------------------------------------------------------------------------
# Main CLI group
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option("--db", envvar="AGENT_QUEUE_DB", default=None, help="Path to AgentQueue database")
@click.version_option(version="0.1.0", prog_name="aq")
@click.pass_context
def cli(ctx: click.Context, db: str | None) -> None:
    """AgentQueue CLI — Modern terminal interface for task management.

    Mirrors Discord slash commands with rich formatting and interactive menus.
    """
    ctx.ensure_object(dict)
    if db:
        os.environ["AGENT_QUEUE_DB"] = db

    if ctx.invoked_subcommand is None:
        # Default: show status
        ctx.invoke(status)


# ---------------------------------------------------------------------------
# /status — System overview
# ---------------------------------------------------------------------------


@cli.command()
def status() -> None:
    """Show system status overview."""
    from .formatters import format_status_overview, format_agent_table

    async def _run_status():
        async with _get_client() as client:
            projects = await client.list_projects()
            agents = await client.list_agents()
            task_counts = await client.count_tasks_by_status()
            return projects, agents, task_counts

    try:
        projects, agents, task_counts = _run(_run_status())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    panel = format_status_overview(projects, agents, task_counts)
    console.print(panel)


# ---------------------------------------------------------------------------
# /task — Task management commands
# ---------------------------------------------------------------------------


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
    console.print(f"[bold green]✅ Task created:[/] [bold bright_cyan]{new_task.id}[/]")
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
            console.print(f"[bold green]✅ Task approved:[/] {task_id}")

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
            console.print(f"[bold yellow]⏹ Task stopped:[/] {task_id}")

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
            console.print(f"[bold green]🔄 Task restarted:[/] {task_id}")

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


# ---------------------------------------------------------------------------
# /agent — Agent management commands
# ---------------------------------------------------------------------------


@cli.group()
def agent() -> None:
    """Agent management commands."""
    pass


@agent.command("list")
def agent_list() -> None:
    """List all registered agents and their status."""
    from .formatters import format_agent_table

    async def _list():
        async with _get_client() as client:
            return await client.list_agents()

    try:
        agents = _run(_list())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not agents:
        console.print("[dim]No agents registered.[/]")
        return

    table = format_agent_table(agents)
    console.print(table)


@agent.command("details")
@click.argument("agent_id")
def agent_details(agent_id: str) -> None:
    """Show detailed information about an agent."""

    async def _details():
        async with _get_client() as client:
            return await client.get_agent(agent_id)

    try:
        a = _run(_details())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not a:
        console.print(f"[bold red]Agent not found:[/] {agent_id}")
        raise SystemExit(1)

    from .styles import AGENT_STATE_ICONS, AGENT_STATE_STYLES
    from rich.panel import Panel
    from rich.text import Text
    from rich.console import Group

    state_icon = AGENT_STATE_ICONS.get(a.state.value, "❓")
    state_style = AGENT_STATE_STYLES.get(a.state.value, "white")

    lines = [
        Text(f"{state_icon} {a.state.value}", style=state_style),
        Text(""),
    ]

    fields = [
        ("Name", a.name),
        ("Type", a.agent_type),
        ("Current Task", a.current_task_id or "—"),
        ("PID", str(a.pid) if a.pid else "—"),
        ("Total Tokens", f"{a.total_tokens_used:,}"),
        ("Session Tokens", f"{a.session_tokens_used:,}"),
    ]

    for label, value in fields:
        line = Text()
        line.append(f"  {label}: ", style="bold cyan")
        line.append(value, style="white")
        lines.append(line)

    panel = Panel(
        Group(*lines),
        title=f"[bold bright_white]Agent: {a.id}[/]",
        border_style=state_style,
        padding=(1, 2),
    )
    console.print(panel)


# ---------------------------------------------------------------------------
# /hook — Hook management commands
# ---------------------------------------------------------------------------


@cli.group()
def hook() -> None:
    """Hook automation commands."""
    pass


@hook.command("list")
@click.option("-p", "--project", default=None, help="Filter by project ID")
@click.option("--enabled/--all", default=False, help="Show only enabled hooks")
def hook_list(project: str | None, enabled: bool) -> None:
    """List automation hooks."""
    from .formatters import format_hook_table

    async def _list():
        async with _get_client() as client:
            en = True if enabled else None
            return await client.list_hooks(project_id=project, enabled=en)

    try:
        hooks = _run(_list())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not hooks:
        console.print("[dim]No hooks found.[/]")
        return

    table = format_hook_table(hooks)
    console.print(table)


@hook.command("runs")
@click.argument("hook_id")
@click.option("--limit", default=20, help="Number of recent runs to show")
def hook_runs(hook_id: str, limit: int) -> None:
    """Show execution history for a hook."""
    from .formatters import format_hook_run_table

    async def _runs():
        async with _get_client() as client:
            h = await client.get_hook(hook_id)
            if not h:
                console.print(f"[bold red]Hook not found:[/] {hook_id}")
                raise SystemExit(1)
            runs = await client.list_hook_runs(hook_id, limit=limit)
            return h, runs

    try:
        h, runs = _run(_runs())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not runs:
        console.print(f"[dim]No execution history for hook '{h.name}'.[/]")
        return

    table = format_hook_run_table(runs)
    console.print(table)


@hook.command("details")
@click.argument("hook_id")
def hook_details(hook_id: str) -> None:
    """Show detailed information about a hook."""
    import json
    from rich.panel import Panel
    from rich.text import Text
    from rich.console import Group
    from rich.syntax import Syntax

    async def _details():
        async with _get_client() as client:
            return await client.get_hook(hook_id)

    try:
        h = _run(_details())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not h:
        console.print(f"[bold red]Hook not found:[/] {hook_id}")
        raise SystemExit(1)

    enabled_text = "✅ Enabled" if h.enabled else "❌ Disabled"
    lines = [
        Text(enabled_text, style="green" if h.enabled else "red"),
        Text(""),
    ]

    fields = [
        ("Project", h.project_id),
        ("Cooldown", f"{h.cooldown_seconds}s"),
        ("Max Tokens/Run", str(h.max_tokens_per_run) if h.max_tokens_per_run else "—"),
    ]

    for label, value in fields:
        line = Text()
        line.append(f"  {label}: ", style="bold cyan")
        line.append(value, style="white")
        lines.append(line)

    # Trigger config
    lines.append(Text(""))
    lines.append(Text("  Trigger:", style="bold cyan"))
    try:
        trigger = json.loads(h.trigger) if isinstance(h.trigger, str) else h.trigger
        trigger_str = json.dumps(trigger, indent=2)
    except (json.JSONDecodeError, TypeError):
        trigger_str = str(h.trigger)
    for tl in trigger_str.split("\n"):
        lines.append(Text(f"    {tl}", style="dim"))

    # Prompt template
    lines.append(Text(""))
    lines.append(Text("  Prompt Template:", style="bold cyan"))
    for pl in (h.prompt_template or "—").split("\n")[:10]:
        lines.append(Text(f"    {pl}", style="white"))

    panel = Panel(
        Group(*lines),
        title=f"[bold bright_white]Hook: {h.name}[/] [dim]({h.id})[/]",
        border_style="bright_blue",
        padding=(1, 2),
    )
    console.print(panel)


# ---------------------------------------------------------------------------
# /project — Project management commands
# ---------------------------------------------------------------------------


@cli.group()
def project() -> None:
    """Project management commands."""
    pass


@project.command("list")
@click.option(
    "-s", "--status", "status_filter",
    default=None,
    type=click.Choice(["ACTIVE", "PAUSED", "ARCHIVED"], case_sensitive=False),
    help="Filter by status",
)
def project_list(status_filter: str | None) -> None:
    """List all projects."""
    from src.models import ProjectStatus
    from .formatters import format_project_table

    async def _list():
        async with _get_client() as client:
            status = ProjectStatus(status_filter) if status_filter else None
            return await client.list_projects(status=status)

    try:
        projects = _run(_list())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not projects:
        console.print("[dim]No projects found.[/]")
        return

    table = format_project_table(projects)
    console.print(table)


@project.command("details")
@click.argument("project_id")
def project_details(project_id: str) -> None:
    """Show detailed information about a project."""
    from rich.panel import Panel
    from rich.text import Text
    from rich.console import Group

    async def _details():
        async with _get_client() as client:
            p = await client.get_project(project_id)
            if not p:
                return None, {}
            counts = await client.count_tasks_by_status(project_id=project_id)
            return p, counts

    try:
        p, counts = _run(_details())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not p:
        console.print(f"[bold red]Project not found:[/] {project_id}")
        raise SystemExit(1)

    from .styles import STATUS_ICONS, STATUS_STYLES
    status_style = "green" if p.status.value == "ACTIVE" else "dim"

    lines = [
        Text(f"Status: {p.status.value}", style=status_style),
        Text(""),
    ]

    fields = [
        ("Name", p.name),
        ("Max Agents", str(p.max_concurrent_agents)),
        ("Credit Weight", str(p.credit_weight)),
        ("Total Tokens", f"{p.total_tokens_used:,}" if p.total_tokens_used else "—"),
        ("Budget Limit", f"{p.budget_limit:,}" if p.budget_limit else "unlimited"),
    ]

    for label, value in fields:
        line = Text()
        line.append(f"  {label}: ", style="bold cyan")
        line.append(value, style="white")
        lines.append(line)

    # Task breakdown
    if counts:
        lines.append(Text(""))
        lines.append(Text("  Tasks:", style="bold cyan"))
        for status, count in sorted(counts.items(), key=lambda x: -x[1]):
            if count == 0:
                continue
            icon = STATUS_ICONS.get(status, "⚪")
            sty = STATUS_STYLES.get(status, "white")
            tl = Text()
            tl.append(f"    {icon} {status}: ", style=sty)
            tl.append(str(count))
            lines.append(tl)

    panel = Panel(
        Group(*lines),
        title=f"[bold bright_white]Project: {p.id}[/]",
        border_style="bright_magenta",
        padding=(1, 2),
    )
    console.print(panel)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    cli()


if __name__ == "__main__":
    main()
