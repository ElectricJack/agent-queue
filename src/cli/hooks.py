"""Hook management CLI commands (aq hook ...)."""

from __future__ import annotations

import json

import click

from .app import cli, console, _run, _get_client, _handle_errors


@cli.group()
def hook() -> None:
    """Hook automation commands."""
    pass


@hook.command("list")
@click.option("-p", "--project", default=None, help="Filter by project ID")
@click.option("--enabled/--all", default=False, help="Show only enabled hooks")
@click.pass_context
@_handle_errors
def hook_list(ctx: click.Context, project: str | None, enabled: bool) -> None:
    """List automation hooks."""
    from .adapters import hook_proxy
    from .formatters import format_hook_table

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _list():
        async with _get_client(api_url) as client:
            args = {}
            if project:
                args["project_id"] = project
            return await client.execute("list_hooks", args)

    result = _run(_list())
    hooks = [hook_proxy(h) for h in result.get("hooks", [])]

    if enabled:
        hooks = [h for h in hooks if h.enabled]

    if not hooks:
        console.print("[dim]No hooks found.[/]")
        return

    table = format_hook_table(hooks)
    console.print(table)


@hook.command("runs")
@click.argument("hook_id")
@click.option("--limit", default=20, help="Number of recent runs to show")
@click.pass_context
@_handle_errors
def hook_runs(ctx: click.Context, hook_id: str, limit: int) -> None:
    """Show execution history for a hook."""
    from .adapters import hook_run_proxy
    from .formatters import format_hook_run_table

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _runs():
        async with _get_client(api_url) as client:
            return await client.execute(
                "list_hook_runs", {"hook_id": hook_id, "limit": limit}
            )

    result = _run(_runs())
    runs = [hook_run_proxy(r) for r in result.get("runs", [])]

    if not runs:
        hook_name = result.get("hook_name", hook_id)
        console.print(f"[dim]No execution history for hook '{hook_name}'.[/]")
        return

    table = format_hook_run_table(runs)
    console.print(table)


@hook.command("details")
@click.argument("hook_id")
@click.pass_context
@_handle_errors
def hook_details(ctx: click.Context, hook_id: str) -> None:
    """Show detailed information about a hook."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _details():
        async with _get_client(api_url) as client:
            result = await client.execute("list_hooks")
            for h in result.get("hooks", []):
                if h.get("id") == hook_id:
                    return h
            return None

    h = _run(_details())
    if not h:
        console.print(f"[bold red]Hook not found:[/] {hook_id}")
        raise SystemExit(1)

    enabled_text = "Enabled" if h.get("enabled") else "Disabled"
    lines = [
        Text(enabled_text, style="green" if h.get("enabled") else "red"),
        Text(""),
    ]

    fields = [
        ("Project", h.get("project_id", "—")),
        ("Cooldown", f"{h.get('cooldown_seconds', 0)}s"),
    ]

    for label, value in fields:
        line = Text()
        line.append(f"  {label}: ", style="bold cyan")
        line.append(str(value), style="white")
        lines.append(line)

    # Trigger config
    lines.append(Text(""))
    lines.append(Text("  Trigger:", style="bold cyan"))
    trigger = h.get("trigger", {})
    if isinstance(trigger, dict):
        trigger_str = json.dumps(trigger, indent=2)
    else:
        trigger_str = str(trigger)
    for tl in trigger_str.split("\n"):
        lines.append(Text(f"    {tl}", style="dim"))

    # Prompt template
    prompt = h.get("prompt_template", "—")
    if prompt:
        lines.append(Text(""))
        lines.append(Text("  Prompt Template:", style="bold cyan"))
        for pl in (prompt or "—").split("\n")[:10]:
            lines.append(Text(f"    {pl}", style="white"))

    panel = Panel(
        Group(*lines),
        title=f"[bold bright_white]Hook: {h.get('name', '?')}[/] [dim]({hook_id})[/]",
        border_style="bright_blue",
        padding=(1, 2),
    )
    console.print(panel)
