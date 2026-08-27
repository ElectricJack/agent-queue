"""``aq session`` — the session-runtime CLI group.

Mostly an operator surface (list, show, peek, attach, nudge, logs, kill),
plus one command an *agent* runs about itself: ``aq session drain-ack``.

``drain-ack`` is the second half of the completion protocol and the reason
this group cannot wait for a later phase.  Completion is:

    aq task close <id> --outcome pass --work-outcome shipped
    aq session drain-ack

`aq task close` and `aq task heartbeat` come from the auto-generated CLI
(they have typed definitions in ``src/tools/definitions.py``); drain-ack has
no typed definition on purpose — it is not a task-scope MCP tool — so it is
hand-crafted here.

Session identity comes from ``AQ_SESSION_ID``, which the session runtime
sets in every session's environment (``src/sessions/env.py``).  That is the
same env-var handshake ``aq prime`` and ``aq handoff`` already use for
``AQ_TASK_ID``, so nothing inside a session needs a flag.

Registered in ``app.py`` *before* ``register_auto_commands`` so these
hand-crafted commands win over any auto-generated variant by name.
"""

from __future__ import annotations

import os

import click

from .app import _get_client, _handle_errors, _run, cli, console
from .envelope import emit


@cli.group("session")
def session() -> None:
    """Inspect and steer agent sessions."""


def _call(ctx: click.Context, command: str, args: dict):
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _go():
        async with _get_client(api_url) as client:
            return await client.execute(command, args)

    return _run(_go())


def _resolve_session_id(explicit: str | None) -> str | None:
    """Session id from the flag, else ``AQ_SESSION_ID`` from the env."""
    return explicit or os.environ.get("AQ_SESSION_ID")


@session.command("list")
@click.option("--state", default=None, help="Filter by state (running, sleeping, ...).")
@click.option("--lifecycle", default=None, help="Filter by lifecycle (task | named).")
@click.option("--live", "live_only", is_flag=True, help="Only starting/running/draining.")
@click.pass_context
@_handle_errors
def session_list(ctx: click.Context, state, lifecycle, live_only) -> None:
    """List sessions with state, task, harness and idle time."""
    args: dict = {"live_only": live_only}
    if state:
        args["state"] = state
    if lifecycle:
        args["lifecycle"] = lifecycle
    result = _call(ctx, "session_list", args)

    def _render(data: dict) -> None:
        from rich.table import Table

        rows = data.get("sessions", [])
        if not rows:
            console.print("[dim]No sessions.[/dim]")
            return
        table = Table(border_style="bright_black")
        for col in ("ID", "Name", "State", "Task", "Harness", "Idle", "Restarts"):
            table.add_column(col)
        for r in rows:
            idle = r.get("idle_seconds") or 0
            table.add_row(
                (r.get("id") or "")[:8],
                r.get("name") or "",
                # A stalled session is still "running" in the DB — stalled is
                # derived, so show it as an annotation rather than a state.
                _state_cell(r),
                r.get("task_id") or "",
                r.get("harness") or "",
                f"{int(idle)}s",
                str(r.get("restarts") or 0),
            )
        console.print(table)

    emit(ctx, result, render=_render)


@session.command("show")
@click.argument("session_id", required=False)
@click.pass_context
@_handle_errors
def session_show(ctx: click.Context, session_id) -> None:
    """Full detail for one session."""
    emit(ctx, _call(ctx, "session_show", {"session_id": _resolve_session_id(session_id)}))


@session.command("peek")
@click.argument("session_id", required=False)
@click.option("-n", "--lines", default=60, help="How many lines to show.")
@click.pass_context
@_handle_errors
def session_peek(ctx: click.Context, session_id, lines) -> None:
    """Last N lines of a session's visible output."""
    result = _call(
        ctx, "session_peek", {"session_id": _resolve_session_id(session_id), "lines": lines}
    )
    emit(ctx, result, render=lambda data: click.echo(data.get("output", "")))


@session.command("attach")
@click.argument("session_id", required=False)
@click.pass_context
@_handle_errors
def session_attach(ctx: click.Context, session_id) -> None:
    """Print the command that attaches a terminal to this session."""
    result = _call(ctx, "session_attach", {"session_id": _resolve_session_id(session_id)})
    emit(ctx, result, render=lambda data: click.echo(data.get("attach_command", "")))


@session.command("nudge")
@click.argument("session_id")
@click.argument("text")
@click.pass_context
@_handle_errors
def session_nudge(ctx: click.Context, session_id, text) -> None:
    """Inject TEXT into a session and submit it.

    Reports ``not_submitted`` when the text was pasted but Enter could not
    be confirmed — that is a real outcome, not a transport error, and the
    caller should re-queue rather than assume delivery.
    """
    emit(ctx, _call(ctx, "session_nudge", {"session_id": session_id, "text": text}))


@session.command("logs")
@click.argument("session_id", required=False)
@click.option("-n", "--lines", default=200, help="How many lines to show.")
@click.pass_context
@_handle_errors
def session_logs(ctx: click.Context, session_id, lines) -> None:
    """Normalized session output (peek tail until transcript readers land)."""
    result = _call(
        ctx, "session_logs", {"session_id": _resolve_session_id(session_id), "lines": lines}
    )
    emit(ctx, result, render=lambda data: click.echo(data.get("output", "")))


def _state_cell(row: dict) -> str:
    """Observed state, annotated when it disagrees with what we want.

    "stalled" is derived (lease TTL vs activity), so it shows as an
    annotation rather than a state.  ``desired_state`` is shown the same
    way and only when the two differ: agreement is the boring case and
    printing it on every row would bury the one row that matters.
    """
    state = row.get("state") or ""
    cell = state + (" (stalled)" if row.get("stalled") else "")
    desired = row.get("desired_state")
    if desired and desired != state:
        cell += f" [yellow]→{desired}[/yellow]"
    return cell


@session.command("sleep")
@click.argument("session_id")
@click.pass_context
@_handle_errors
def session_sleep(ctx: click.Context, session_id) -> None:
    """Mark a session as not wanted running.

    Intent only — the process is not signalled.  The reconciler drains it
    on a later tick; use ``session kill`` to stop it now.
    """
    emit(ctx, _call(ctx, "session_sleep", {"session_id": session_id}))


@session.command("wake")
@click.argument("session_id")
@click.pass_context
@_handle_errors
def session_wake(ctx: click.Context, session_id) -> None:
    """Mark a sleeping named session as wanted again.

    The next reconciler tick starts it.  Named sessions only — task
    sessions are started by the task lifecycle.
    """
    emit(ctx, _call(ctx, "session_wake", {"session_id": session_id}))


@session.command("kill")
@click.argument("session_id")
@click.pass_context
@_handle_errors
def session_kill(ctx: click.Context, session_id) -> None:
    """Kill a session (instance-token fenced).

    Does not transition the task: the next reconciler tick classifies the
    dead process, so a manual kill and a crash follow the same path.
    """
    emit(ctx, _call(ctx, "session_kill", {"session_id": session_id}))


@session.command("drain-ack")
@click.option(
    "--session-id",
    "session_id",
    default=None,
    help="Session ID (defaults to $AQ_SESSION_ID).",
)
@click.pass_context
@_handle_errors
def session_drain_ack(ctx: click.Context, session_id) -> None:
    """Agent-facing: declare this session finished so it can be reaped.

    Run this *after* ``aq task close``.  Acking with the task still open is
    a premature drain: the daemon nudges you to close the task rather than
    killing the session, because acking must never be a way to end a task.
    """
    resolved = _resolve_session_id(session_id)
    if not resolved:
        raise click.ClickException(
            "no session in scope — pass --session-id or run inside a session "
            "(AQ_SESSION_ID is set by the session runtime)"
        )
    emit(ctx, _call(ctx, "session_drain_ack", {"session_id": resolved}))
