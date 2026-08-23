"""Stream CLI — ``aq stream start|tail|kill``.

Wraps POST/GET /api/streams* (a bespoke router, not /api/execute — see
CLIClient.start_stream/get_stream/tail_stream/kill_stream in client.py).
Implements docs/superpowers/specs/2026-08-22-pane-console-stream-design.md §10.
"""

from __future__ import annotations

import click

from .app import cli, console, _run, _get_client, _handle_errors
from .envelope import emit


@cli.group("stream")
def stream() -> None:
    """Streamable-command registry (console-stream pane view)."""


@stream.command(
    "start",
    context_settings={"ignore_unknown_options": True},
)
@click.option("--title", default=None, help="Header label shown in the pane")
@click.option("--session-id", "session_id", required=True, help="Owning session id")
@click.option("-p", "--project", "project_id", default=None, help="Owning project id")
@click.option("--cwd", required=True, help="Working directory for the command")
@click.argument("argv", nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_context
@_handle_errors
def stream_start(
    ctx: click.Context, title: str | None, session_id: str,
    project_id: str | None, cwd: str, argv: tuple[str, ...],
) -> None:
    """Start a streamable command: ``aq stream start -- pytest tests/ -x``."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None
    command = list(argv)

    async def _start():
        async with _get_client(api_url) as client:
            return await client.start_stream(
                command, cwd, title=title, session_id=session_id, project_id=project_id,
            )

    result = _run(_start())

    def _render(data: dict) -> None:
        console.print(
            f"[bold green]Stream started:[/] [bold bright_cyan]{data.get('stream_id')}[/] "
            f"({data.get('status')})"
        )

    emit(ctx, result, render=_render)


@stream.command("tail")
@click.argument("stream_id")
@click.option("--after-seq", default=-1, type=int, help="Only frames after this sequence number")
@click.pass_context
@_handle_errors
def stream_tail(ctx: click.Context, stream_id: str, after_seq: int) -> None:
    """Poll buffered output since ``--after-seq`` (non-SSE fallback)."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _tail():
        async with _get_client(api_url) as client:
            return await client.tail_stream(stream_id, after_seq=after_seq)

    result = _run(_tail())

    def _render(data: dict) -> None:
        for frame in data.get("frames", []):
            if frame.get("type") == "line":
                prefix = "!" if frame.get("stream") == "stderr" else " "
                console.print(f"{prefix} {frame.get('text', '')}")
            elif frame.get("type") == "exit":
                console.print(f"[dim]exited ({frame.get('rc')})[/]")
            elif frame.get("type") == "killed":
                console.print("[dim]killed[/]")

    emit(ctx, result, render=_render)


@stream.command("kill")
@click.argument("stream_id")
@click.pass_context
@_handle_errors
def stream_kill(ctx: click.Context, stream_id: str) -> None:
    """Kill a running stream (SIGTERM -> SIGINT -> SIGKILL escalation)."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _kill():
        async with _get_client(api_url) as client:
            return await client.kill_stream(stream_id)

    result = _run(_kill())

    def _render(data: dict) -> None:
        console.print(f"[bold yellow]Stream {data.get('stream_id')}:[/] {data.get('status')}")

    emit(ctx, result, render=_render)
