"""Hook management CLI commands (aq hook ...)."""

from __future__ import annotations

import click

from .app import cli, console, _run, _get_client


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

    enabled_text = "Enabled" if h.enabled else "Disabled"
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
