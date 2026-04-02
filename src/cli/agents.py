"""Agent management CLI commands (aq agent ...)."""

from __future__ import annotations

import click

from .app import cli, console, _run, _get_client


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

    state_icon = AGENT_STATE_ICONS.get(a.state.value, "?")
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
