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
# /plugin — Plugin management commands
# ---------------------------------------------------------------------------


@cli.group()
def plugin() -> None:
    """Plugin management commands."""
    pass


@plugin.command("list")
def plugin_list() -> None:
    """List installed plugins."""
    from rich.table import Table

    async def _run_list():
        async with _get_client() as client:
            return await client.list_plugins()

    try:
        plugins = _run(_run_list())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not plugins:
        console.print("[dim]No plugins installed.[/dim]")
        return

    table = Table(title="Installed Plugins", show_lines=True)
    table.add_column("Name", style="bold cyan")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Source")

    status_colors = {
        "active": "green",
        "installed": "yellow",
        "disabled": "dim",
        "error": "red",
    }

    for p in plugins:
        status = p.get("status", "unknown")
        color = status_colors.get(status, "white")
        table.add_row(
            p.get("id", "?"),
            p.get("version", "?"),
            f"[{color}]{status}[/{color}]",
            p.get("source_url", ""),
        )

    console.print(table)


@plugin.command("info")
@click.argument("name")
def plugin_info(name: str) -> None:
    """Show detailed plugin info."""
    from rich.panel import Panel
    from rich.text import Text

    async def _run_info():
        async with _get_client() as client:
            return await client.get_plugin(name)

    try:
        p = _run(_run_info())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not p:
        console.print(f"[bold red]Plugin '{name}' not found.[/]")
        return

    lines = []
    lines.append(f"[bold]Name:[/] {p.get('id', name)}")
    lines.append(f"[bold]Version:[/] {p.get('version', '?')}")
    lines.append(f"[bold]Status:[/] {p.get('status', '?')}")
    lines.append(f"[bold]Source:[/] {p.get('source_url', '?')}")
    lines.append(f"[bold]Rev:[/] {p.get('source_rev', '?')[:12]}")
    lines.append(f"[bold]Path:[/] {p.get('install_path', '?')}")
    if p.get("error_message"):
        lines.append(f"[bold red]Error:[/] {p['error_message']}")

    console.print(Panel("\n".join(lines), title=f"Plugin: {name}"))


@plugin.command("install")
@click.argument("url")
@click.option("--branch", "-b", default=None, help="Branch to install")
@click.option("--name", "-n", default=None, help="Override plugin name")
def plugin_install(url: str, branch: str | None, name: str | None) -> None:
    """Install a plugin from a git repository."""
    console.print(f"[bold]Installing plugin from {url}...[/]")

    async def _run_install():
        async with _get_client() as client:
            from src.plugins.loader import clone_plugin_repo, parse_plugin_yaml, install_requirements, setup_prompts
            from pathlib import Path
            import json

            # Derive name from URL
            plugin_name = name
            if not plugin_name:
                plugin_name = url.rstrip("/").rsplit("/", 1)[-1]
                if plugin_name.endswith(".git"):
                    plugin_name = plugin_name[:-4]

            plugins_dir = Path(os.environ.get("AGENT_QUEUE_DATA", os.path.expanduser("~/.agent-queue"))) / "plugins"
            install_path = str(plugins_dir / plugin_name)
            Path(install_path).mkdir(parents=True, exist_ok=True)

            # Clone
            rev = await clone_plugin_repo(url, install_path, branch=branch)

            # Parse and validate
            info = parse_plugin_yaml(install_path)
            install_requirements(install_path)
            setup_prompts(install_path)

            # Record in DB
            await client.create_plugin(
                plugin_id=info.name,
                version=info.version,
                source_url=url,
                source_rev=rev,
                source_branch=branch or "",
                install_path=install_path,
                status="installed",
                config=json.dumps(info.default_config),
                permissions=json.dumps([p.value for p in info.permissions]),
            )
            return info.name, info.version

    try:
        pname, pversion = _run(_run_install())
        console.print(f"[bold green]Installed plugin '{pname}' v{pversion}[/]")
        console.print("[dim]Restart the daemon to activate the plugin.[/dim]")
    except Exception as e:
        console.print(f"[bold red]Installation failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("remove")
@click.argument("name")
@click.confirmation_option(prompt="Are you sure you want to remove this plugin?")
def plugin_remove(name: str) -> None:
    """Remove an installed plugin."""
    import shutil

    async def _run_remove():
        async with _get_client() as client:
            p = await client.get_plugin(name)
            if not p:
                return None
            install_path = p.get("install_path")
            await client.delete_plugin_data_all(name)
            await client.delete_plugin(name)
            return install_path

    try:
        install_path = _run(_run_remove())
        if install_path is None:
            console.print(f"[bold red]Plugin '{name}' not found.[/]")
            raise SystemExit(1)

        if install_path and os.path.exists(install_path):
            shutil.rmtree(install_path)

        console.print(f"[bold green]Plugin '{name}' removed.[/]")
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"[bold red]Removal failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("enable")
@click.argument("name")
def plugin_enable(name: str) -> None:
    """Enable a disabled plugin."""
    async def _run_enable():
        async with _get_client() as client:
            await client.update_plugin(name, status="installed")

    try:
        _run(_run_enable())
        console.print(f"[bold green]Plugin '{name}' enabled.[/]")
        console.print("[dim]Restart the daemon to activate.[/dim]")
    except Exception as e:
        console.print(f"[bold red]Enable failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("disable")
@click.argument("name")
def plugin_disable(name: str) -> None:
    """Disable a plugin without removing it."""
    async def _run_disable():
        async with _get_client() as client:
            await client.update_plugin(name, status="disabled")

    try:
        _run(_run_disable())
        console.print(f"[bold green]Plugin '{name}' disabled.[/]")
    except Exception as e:
        console.print(f"[bold red]Disable failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("update")
@click.argument("name")
def plugin_update(name: str) -> None:
    """Update a plugin (git pull + reinstall requirements)."""
    from src.plugins.loader import pull_plugin_repo, install_requirements, parse_plugin_yaml

    async def _run_update():
        async with _get_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            install_path = p["install_path"]
            new_rev = await pull_plugin_repo(install_path)
            install_requirements(install_path)
            info = parse_plugin_yaml(install_path)
            await client.update_plugin(
                name, version=info.version, source_rev=new_rev,
            )
            return info.version, new_rev

    try:
        console.print(f"[bold]Updating plugin '{name}'...[/]")
        version, rev = _run(_run_update())
        console.print(
            f"[bold green]Plugin '{name}' updated to v{version} "
            f"(rev {rev[:12]})[/]"
        )
        console.print("[dim]Restart the daemon to activate changes.[/dim]")
    except Exception as e:
        console.print(f"[bold red]Update failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("reload")
@click.argument("name")
def plugin_reload(name: str) -> None:
    """Reload a plugin module."""
    from src.plugins.loader import parse_plugin_yaml, import_plugin_module

    async def _run_reload():
        async with _get_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            install_path = p["install_path"]
            info = parse_plugin_yaml(install_path)
            import_plugin_module(install_path)
            await client.update_plugin(name, version=info.version)
            return info.version

    try:
        version = _run(_run_reload())
        console.print(
            f"[bold green]Plugin '{name}' reloaded (v{version}).[/]"
        )
        console.print("[dim]Restart the daemon to apply in-process.[/dim]")
    except Exception as e:
        console.print(f"[bold red]Reload failed:[/] {e}")
        raise SystemExit(1)


@plugin.command("config")
@click.argument("name")
@click.argument("key_values", nargs=-1)
def plugin_config(name: str, key_values: tuple[str, ...]) -> None:
    """View or set plugin configuration.

    With no KEY=VALUE arguments, shows current config.
    With KEY=VALUE pairs, sets those values.
    """
    import json

    async def _run_config():
        async with _get_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            return p

    async def _set_config(updates: dict):
        async with _get_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            current = json.loads(p.get("config", "{}") or "{}")
            current.update(updates)
            await client.update_plugin(name, config=json.dumps(current))
            return current

    try:
        if not key_values:
            p = _run(_run_config())
            cfg = json.loads(p.get("config", "{}") or "{}")
            if not cfg:
                console.print(f"[dim]No configuration for plugin '{name}'.[/dim]")
                return
            from rich.table import Table

            table = Table(title=f"Config: {name}")
            table.add_column("Key", style="bold cyan")
            table.add_column("Value")
            for k, v in sorted(cfg.items()):
                table.add_row(k, str(v))
            console.print(table)
        else:
            updates = {}
            for kv in key_values:
                if "=" not in kv:
                    console.print(
                        f"[bold red]Invalid format:[/] '{kv}' "
                        "(expected KEY=VALUE)"
                    )
                    raise SystemExit(1)
                k, v = kv.split("=", 1)
                updates[k] = v
            result = _run(_set_config(updates))
            console.print(f"[bold green]Config updated for '{name}'.[/]")
            for k, v in sorted(result.items()):
                console.print(f"  [cyan]{k}[/] = {v}")
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"[bold red]Config error:[/] {e}")
        raise SystemExit(1)


@plugin.command("logs")
@click.argument("name")
@click.option("--limit", default=20, help="Number of recent runs to show")
def plugin_logs(name: str, limit: int) -> None:
    """View plugin hook execution history."""
    from .formatters import format_hook_run_table

    async def _run_logs():
        async with _get_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            hooks = await client.list_hooks()
            plugin_hooks = [
                h for h in hooks if getattr(h, "plugin_id", None) == name
            ]
            all_runs = []
            for h in plugin_hooks:
                runs = await client.list_hook_runs(h.id, limit=limit)
                all_runs.extend(runs)
            all_runs.sort(
                key=lambda r: getattr(r, "started_at", 0) or 0,
                reverse=True,
            )
            return all_runs[:limit]

    try:
        runs = _run(_run_logs())
    except Exception as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not runs:
        console.print(f"[dim]No hook runs found for plugin '{name}'.[/dim]")
        return

    table = format_hook_run_table(runs)
    console.print(table)


@plugin.command("prompts")
@click.argument("name")
def plugin_prompts(name: str) -> None:
    """List prompts provided by a plugin."""
    from pathlib import Path

    async def _run_prompts():
        async with _get_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            return p["install_path"]

    try:
        install_path = _run(_run_prompts())
    except Exception as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    inst_dir = Path(install_path) / "prompts"
    src_dir = Path(install_path) / "src" / "prompts"

    from rich.table import Table

    table = Table(title=f"Prompts: {name}")
    table.add_column("File", style="bold cyan")
    table.add_column("Source", style="dim")
    table.add_column("Instance")

    prompt_names: set[str] = set()
    if src_dir.exists():
        for f in src_dir.iterdir():
            if f.is_file():
                prompt_names.add(f.name)
    if inst_dir.exists():
        for f in inst_dir.iterdir():
            if f.is_file():
                prompt_names.add(f.name)

    if not prompt_names:
        console.print(f"[dim]No prompts found for plugin '{name}'.[/dim]")
        return

    for pname in sorted(prompt_names):
        has_src = (src_dir / pname).exists() if src_dir.exists() else False
        has_inst = (inst_dir / pname).exists() if inst_dir.exists() else False
        table.add_row(
            pname,
            "[green]yes[/]" if has_src else "[dim]no[/]",
            "[green]yes[/]" if has_inst else "[dim]no[/]",
        )

    console.print(table)


@plugin.command("diff-prompts")
@click.argument("name")
def plugin_diff_prompts(name: str) -> None:
    """Diff instance prompts vs source defaults."""
    import difflib
    from pathlib import Path

    async def _run_diff():
        async with _get_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            return p["install_path"]

    try:
        install_path = _run(_run_diff())
    except Exception as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    src_dir = Path(install_path) / "src" / "prompts"
    inst_dir = Path(install_path) / "prompts"

    if not src_dir.exists():
        console.print(f"[dim]No source prompts for plugin '{name}'.[/dim]")
        return

    any_diff = False
    for src_file in sorted(src_dir.iterdir()):
        if not src_file.is_file():
            continue
        inst_file = inst_dir / src_file.name
        if not inst_file.exists():
            console.print(
                f"[yellow]{src_file.name}:[/] instance file missing"
            )
            any_diff = True
            continue

        src_lines = src_file.read_text().splitlines(keepends=True)
        inst_lines = inst_file.read_text().splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            src_lines,
            inst_lines,
            fromfile=f"source/{src_file.name}",
            tofile=f"instance/{src_file.name}",
        ))
        if diff:
            any_diff = True
            console.print(f"\n[bold]{src_file.name}[/]")
            for line in diff:
                line = line.rstrip("\n")
                if line.startswith("+"):
                    console.print(f"[green]{line}[/]")
                elif line.startswith("-"):
                    console.print(f"[red]{line}[/]")
                else:
                    console.print(line)

    if not any_diff:
        console.print(
            f"[bold green]All prompts match source defaults for '{name}'.[/]"
        )


@plugin.command("reset-prompts")
@click.argument("name")
@click.confirmation_option(prompt="Reset all prompts to source defaults?")
def plugin_reset_prompts(name: str) -> None:
    """Reset instance prompts to source defaults."""
    from src.plugins.loader import reset_prompts

    async def _run_reset():
        async with _get_client() as client:
            p = await client.get_plugin(name)
            if not p:
                raise ValueError(f"Plugin '{name}' not found.")
            return p["install_path"]

    try:
        install_path = _run(_run_reset())
        count = reset_prompts(install_path)
        console.print(
            f"[bold green]Reset {count} prompt(s) for '{name}'.[/]"
        )
    except Exception as e:
        console.print(f"[bold red]Reset failed:[/] {e}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    cli()


if __name__ == "__main__":
    main()
