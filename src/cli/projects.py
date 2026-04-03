"""Hand-crafted project CLI commands that need composite logic or UX sugar.

Simple list commands are auto-generated with Rich formatters via the
formatter registry.  This file only contains commands that compose
multiple API calls or provide friendly key aliasing.
"""

from __future__ import annotations

from typing import Any

import click

from .app import cli, console, _run, _get_client, _handle_errors


def _getval(obj: Any, key: str, default: Any = None) -> Any:
    """Get a value from a typed response or dict, normalising Unset → default."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    val = getattr(obj, key, default)
    if type(val).__name__ == "Unset":
        return default
    return val


@cli.group()
def project() -> None:
    """Project management commands."""
    pass


@project.command("details")
@click.argument("project_id")
@click.pass_context
@_handle_errors
def project_details(ctx: click.Context, project_id: str) -> None:
    """Show detailed information about a project with task breakdown."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    from .styles import STATUS_ICONS, STATUS_STYLES

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _details():
        async with _get_client(api_url) as client:
            proj_result = await client.execute("list_projects")
            task_result = await client.execute(
                "list_tasks",
                {
                    "project_id": project_id,
                    "include_completed": True,
                },
            )
            return proj_result, task_result

    proj_result, task_result = _run(_details())

    p = None
    for proj in _getval(proj_result, "projects", []):
        if _getval(proj, "id") == project_id:
            p = proj
            break

    if not p:
        console.print(f"[bold red]Project not found:[/] {project_id}")
        raise SystemExit(1)

    status = (_getval(p, "status", "ACTIVE") or "ACTIVE").upper()
    status_style = "green" if status == "ACTIVE" else "dim"

    lines = [
        Text(f"Status: {status}", style=status_style),
        Text(""),
    ]

    fields = [
        ("Name", _getval(p, "name", "—")),
        ("Channel", _getval(p, "discord_channel_id", "—") or "—"),
        ("Max Agents", str(_getval(p, "max_concurrent_agents", "—"))),
        ("Credit Weight", str(_getval(p, "credit_weight", "—"))),
    ]

    for label, value in fields:
        line = Text()
        line.append(f"  {label}: ", style="bold cyan")
        line.append(value, style="white")
        lines.append(line)

    tasks = _getval(task_result, "tasks", [])
    if tasks:
        counts: dict[str, int] = {}
        for t in tasks:
            s = (_getval(t, "status", "UNKNOWN") or "UNKNOWN").upper()
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
            f"[bold red]Unknown key:[/] {key}\n[dim]Allowed: {', '.join(sorted(KEY_MAP))}[/]"
        )
        raise SystemExit(1)

    coerced: str | int | float | None = value
    if field == "max_concurrent_agents":
        coerced = int(value)
    elif field == "credit_weight":
        coerced = float(value)
    elif field == "budget_limit":
        coerced = None if value.lower() in ("none", "null", "unlimited") else int(value)

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
