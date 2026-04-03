"""Project management CLI commands (aq project ...)."""

from __future__ import annotations

import click

from .app import cli, console, _run, _get_client, _handle_errors


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
@click.pass_context
@_handle_errors
def project_list(ctx: click.Context, status_filter: str | None) -> None:
    """List all projects."""
    from .adapters import project_proxy
    from .formatters import format_project_table

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _list():
        async with _get_client(api_url) as client:
            return await client.execute("list_projects")

    result = _run(_list())
    projects = [project_proxy(p) for p in result.get("projects", [])]

    if status_filter:
        status_filter_upper = status_filter.upper()
        projects = [p for p in projects if p.status and p.status.value == status_filter_upper]

    if not projects:
        console.print("[dim]No projects found.[/]")
        return

    table = format_project_table(projects)
    console.print(table)


@project.command("details")
@click.argument("project_id")
@click.pass_context
@_handle_errors
def project_details(ctx: click.Context, project_id: str) -> None:
    """Show detailed information about a project."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    from .styles import STATUS_ICONS, STATUS_STYLES

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _details():
        async with _get_client(api_url) as client:
            proj_result = await client.execute("list_projects")
            task_result = await client.execute("list_tasks", {
                "project_id": project_id,
                "include_completed": True,
            })
            return proj_result, task_result

    proj_result, task_result = _run(_details())

    # Find our project
    p = None
    for proj in proj_result.get("projects", []):
        if proj.get("id") == project_id:
            p = proj
            break

    if not p:
        console.print(f"[bold red]Project not found:[/] {project_id}")
        raise SystemExit(1)

    status = p.get("status", "ACTIVE")
    status_style = "green" if status == "ACTIVE" else "dim"

    lines = [
        Text(f"Status: {status}", style=status_style),
        Text(""),
    ]

    fields = [
        ("Name", p.get("name", "—")),
        ("Channel", p.get("discord_channel_id", "—") or "—"),
        ("Max Agents", str(p.get("max_concurrent_agents", "—"))),
        ("Credit Weight", str(p.get("credit_weight", "—"))),
    ]

    for label, value in fields:
        line = Text()
        line.append(f"  {label}: ", style="bold cyan")
        line.append(value, style="white")
        lines.append(line)

    # Task breakdown from list_tasks result
    tasks = task_result.get("tasks", [])
    if tasks:
        counts: dict[str, int] = {}
        for t in tasks:
            s = t.get("status", "UNKNOWN").upper()
            counts[s] = counts.get(s, 0) + 1

        lines.append(Text(""))
        lines.append(Text("  Tasks:", style="bold cyan"))
        for status_name, count in sorted(counts.items(), key=lambda x: -x[1]):
            if count == 0:
                continue
            icon = STATUS_ICONS.get(status_name, "o")
            sty = STATUS_STYLES.get(status_name, "white")
            tl = Text()
            tl.append(f"    {icon} {status_name}: ", style=sty)
            tl.append(str(count))
            lines.append(tl)

    panel = Panel(
        Group(*lines),
        title=f"[bold bright_white]Project: {project_id}[/]",
        border_style="bright_magenta",
        padding=(1, 2),
    )
    console.print(panel)


@project.command("set")
@click.argument("project_id")
@click.argument("key")
@click.argument("value")
@click.pass_context
@_handle_errors
def project_set(ctx: click.Context, project_id: str, key: str, value: str) -> None:
    """Set a project property. e.g. aq project set myproj channel 123456"""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    # Map friendly key names to edit_project field names
    KEY_MAP = {
        "channel": "discord_channel_id",
        "name": "name",
        "max-agents": "max_concurrent_agents",
        "credit-weight": "credit_weight",
        "budget-limit": "budget_limit",
        "branch": "default_branch",
    }

    field = KEY_MAP.get(key)
    if not field:
        console.print(
            f"[bold red]Unknown key:[/] {key}\n"
            f"[dim]Allowed: {', '.join(sorted(KEY_MAP))}[/]"
        )
        raise SystemExit(1)

    # Type coercion
    coerced: str | int | float | None = value
    if field == "max_concurrent_agents":
        coerced = int(value)
    elif field == "credit_weight":
        coerced = float(value)
    elif field == "budget_limit":
        coerced = None if value.lower() in ("none", "null", "unlimited") else int(value)

    # Use the appropriate command for the field
    if field == "default_branch":
        cmd = "set_default_branch"
        args = {"project_id": project_id, "branch": value}
    elif field == "discord_channel_id":
        cmd = "set_project_channel"
        args = {"project_id": project_id, "channel_id": value}
    else:
        cmd = "edit_project"
        args = {"project_id": project_id, field: coerced}

    async def _set():
        async with _get_client(api_url) as client:
            return await client.execute(cmd, args)

    _run(_set())
    console.print(f"[green]Updated[/] {project_id} [bold cyan]{key}[/] = {value}")
