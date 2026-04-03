"""Agent management CLI commands (aq agent ...)."""

from __future__ import annotations

import click

from .app import cli, console, _run, _get_client, _handle_errors


@cli.group()
def agent() -> None:
    """Agent management commands."""
    pass


@agent.command("list")
@click.option("-p", "--project", default=None, help="Filter by project ID")
@click.pass_context
@_handle_errors
def agent_list(ctx: click.Context, project: str | None) -> None:
    """List all agents (workspaces) and their status."""
    from .adapters import agent_proxy
    from .formatters import format_agent_table

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _list():
        async with _get_client(api_url) as client:
            args = {}
            if project:
                args["project_id"] = project
            return await client.execute("list_agents", args)

    result = _run(_list())
    agents = [agent_proxy(a) for a in result.get("agents", [])]

    if not agents:
        console.print("[dim]No agents found.[/]")
        return

    table = format_agent_table(agents)
    console.print(table)


@agent.command("details")
@click.argument("agent_id")
@click.option("-p", "--project", default=None, help="Project ID")
@click.pass_context
@_handle_errors
def agent_details(ctx: click.Context, agent_id: str, project: str | None) -> None:
    """Show detailed information about an agent."""
    from .adapters import agent_proxy
    from .styles import AGENT_STATE_ICONS, AGENT_STATE_STYLES
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _details():
        async with _get_client(api_url) as client:
            args = {}
            if project:
                args["project_id"] = project
            result = await client.execute("list_agents", args)
            for a in result.get("agents", []):
                if a.get("workspace_id") == agent_id or a.get("name") == agent_id:
                    return a
            return None

    raw = _run(_details())
    if not raw:
        console.print(f"[bold red]Agent not found:[/] {agent_id}")
        raise SystemExit(1)

    a = agent_proxy(raw)
    state_icon = AGENT_STATE_ICONS.get(a.state.value, "?")
    state_style = AGENT_STATE_STYLES.get(a.state.value, "white")

    lines = [
        Text(f"{state_icon} {a.state.value}", style=state_style),
        Text(""),
    ]

    fields = [
        ("Name", a.name or "—"),
        ("Project", a.get("project_id") or "—"),
        ("Current Task", a.current_task_id or "—"),
        ("Tokens", f"{a.session_tokens_used:,}" if a.session_tokens_used else "—"),
    ]

    for label, value in fields:
        line = Text()
        line.append(f"  {label}: ", style="bold cyan")
        line.append(str(value), style="white")
        lines.append(line)

    panel = Panel(
        Group(*lines),
        title=f"[bold bright_white]Agent: {a.id}[/]",
        border_style=state_style,
        padding=(1, 2),
    )
    console.print(panel)
