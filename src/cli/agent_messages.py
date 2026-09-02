"""Hand-crafted ``aq agent message`` command for live-worker guidance."""

from __future__ import annotations

import click

from .app import cli, console, _get_client, _handle_errors, _run
from .envelope import emit


@cli.group("agent")
def agent() -> None:
    """Agent management and live-worker messaging."""


@agent.command("message")
@click.argument("target", required=False)
@click.argument("body", required=False)
@click.option("--all-running", is_flag=True, help="Broadcast to every running worker.")
@click.option("--profile", default=None, help="Only broadcast to this profile.")
@click.option("--wait", type=click.IntRange(0, 60), default=None, help="Wait for delivery.")
@click.pass_context
@_handle_errors
def agent_message(ctx, target, body, all_running, profile, wait) -> None:
    """Send BODY to a live task, agent, or session."""
    if all_running:
        body, target = body or target, None
    if not body:
        raise click.UsageError("BODY is required")
    if not all_running and not target:
        raise click.UsageError("TARGET is required unless --all-running is set")
    api_url = ctx.obj.get("api_url") if ctx.obj else None
    params = {"body": body, "all_running": all_running}
    if target:
        params["target"] = target
    if profile:
        params["profile"] = profile
    if wait is not None:
        params["wait"] = wait

    async def _send():
        async with _get_client(api_url) as client:
            return await client.execute("agent_message", params)

    result = _run(_send())

    def _render(data):
        if "recipients" in data:
            console.print(f"[bold green]Queued guidance for {data['count']} live worker(s).[/]")
        else:
            console.print(f"[bold green]Message queued:[/] {data.get('message_id')}")

    emit(ctx, result, render=_render)
