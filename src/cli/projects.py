"""Project management CLI commands (aq project ...)."""

from __future__ import annotations

import click

from .app import cli, console, _run, _get_client


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
        ("Channel", p.discord_channel_id or "—"),
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
            icon = STATUS_ICONS.get(status, "o")
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


@project.command("set")
@click.argument("project_id")
@click.argument("key")
@click.argument("value")
def project_set(project_id: str, key: str, value: str) -> None:
    """Set a project property. e.g. aq project set myproj channel 123456"""
    ALLOWED_KEYS = {
        "channel": "discord_channel_id",
        "name": "name",
        "max-agents": "max_concurrent_agents",
        "credit-weight": "credit_weight",
        "budget-limit": "budget_limit",
        "branch": "repo_default_branch",
    }

    db_key = ALLOWED_KEYS.get(key)
    if not db_key:
        console.print(
            f"[bold red]Unknown key:[/] {key}\n"
            f"[dim]Allowed: {', '.join(sorted(ALLOWED_KEYS))}[/]"
        )
        raise SystemExit(1)

    # Type coercion
    coerced: str | int | float | None = value
    if db_key == "max_concurrent_agents":
        coerced = int(value)
    elif db_key == "credit_weight":
        coerced = float(value)
    elif db_key == "budget_limit":
        coerced = None if value.lower() in ("none", "null", "unlimited") else int(value)

    async def _set():
        async with _get_client() as client:
            p = await client.get_project(project_id)
            if not p:
                return False
            await client.update_project(project_id, **{db_key: coerced})
            return True

    try:
        found = _run(_set())
    except FileNotFoundError as e:
        console.print(f"[bold red]Error:[/] {e}")
        raise SystemExit(1)

    if not found:
        console.print(f"[bold red]Project not found:[/] {project_id}")
        raise SystemExit(1)

    console.print(f"[green]Updated[/] {project_id} [bold cyan]{key}[/] = {value}")
