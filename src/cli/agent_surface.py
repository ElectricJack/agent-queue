"""Agent-facing surface commands: ``aq schema`` (Phase S0), ``aq prime``,
``aq handoff``, ``aq inbox --inject`` (Phase S1, design §5, §6 — see
docs/specs/implementation/aq-surface.md §5.3). Registered in ``app.py``
before ``register_auto_commands`` so these hand-crafted commands win over
any future auto-generated one (§5.3 ordering rule).

``aq prime`` / ``aq handoff`` accept an optional ``--task-id`` (falling back
to an ``AQ_TASK_ID`` env var) because session-scope resolution from a
bearer token (design §7) is Phase S2 — not implemented yet. Session-runtime
is expected to set ``AQ_TASK_ID`` in the session env once it lands, at
which point these commands need no flag at all inside a session (matching
"no per-command flags are needed inside a session", design §2); until then
the caller must pass ``--task-id`` explicitly (or export the env var).
"""

from __future__ import annotations

import os

import click

from .app import cli, console, _run, _get_client, _handle_errors
from .envelope import emit


@cli.command("schema")
@click.pass_context
@_handle_errors
def schema(ctx: click.Context) -> None:
    """Print the system's enum catalog (task statuses, types, dependency
    types, gate types/statuses, ...) so scripts and agents never guess
    magic strings.
    """
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _get_schema():
        async with _get_client(api_url) as client:
            return await client.execute("get_schema")

    result = _run(_get_schema())

    def _render(data: dict) -> None:
        from rich.table import Table

        enums = data.get("enums", {})
        table = Table(
            title=f"aq schema (schema_version={data.get('schema_version', '?')})",
            title_style="bold bright_white",
            border_style="bright_black",
        )
        table.add_column("Enum", style="bold bright_cyan")
        table.add_column("Values")
        for name, values in enums.items():
            table.add_row(name, ", ".join(str(v) for v in values))
        console.print(table)

    emit(ctx, result, render=_render)


# ---------------------------------------------------------------------------
# aq prime — design §5, implementation §5.3
# ---------------------------------------------------------------------------


@cli.command("prime")
@click.option("--task-id", "task_id", default=None, help="Task ID (defaults to $AQ_TASK_ID).")
@click.option("--session-id", "session_id", default=None, help="Session ID (optional).")
@click.option("--work-dir", "work_dir", default=None, help="Work dir override (optional).")
@click.option(
    "--hook-json",
    is_flag=True,
    help="Wrap output as the Claude Code SessionStart hook envelope.",
)
@click.option(
    "--hook-format",
    "hook_format",
    default=None,
    help="Wrap output for a specific harness's hook contract (e.g. 'claude').",
)
@click.pass_context
@_handle_errors
def prime(ctx: click.Context, task_id, session_id, work_dir, hook_json, hook_format) -> None:
    """Print this task's startup prime document (design §5).

    Plain mode prints the rendered markdown body. ``--hook-json`` /
    ``--hook-format`` wrap it in a harness's hook envelope — wrapping is a
    pure presentation step (``src/prime/hook_envelopes.py``), so it runs
    even for a suppressed hook call without needing the daemon.
    """
    from src.prime.hook_envelopes import suppressed, wrap

    hook_mode = hook_json or bool(hook_format)
    harness = "claude" if hook_json else (hook_format or "")

    if suppressed(os.environ, hook_mode):
        # Bootstrap argv prompt already delivered .aq/prompt.md — priming
        # again here would waste the exact tokens this design saves
        # (design §5.4). Post-compaction SessionStart events still reach
        # this command because session-runtime clears the env var's effect
        # there by design.
        click.echo(wrap("", harness))
        return

    resolved_task_id = task_id or os.environ.get("AQ_TASK_ID")
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _fetch():
        args: dict = {}
        if resolved_task_id:
            args["task_id"] = resolved_task_id
        if session_id:
            args["session_id"] = session_id
        if work_dir:
            args["work_dir"] = work_dir
        async with _get_client(api_url) as client:
            return await client.execute("prime", args)

    result = _run(_fetch())

    if hook_mode:
        body = result.get("body", "") if "error" not in result else result.get("error", "")
        click.echo(wrap(body, harness))
        return

    emit(ctx, result, render=lambda data: click.echo(data.get("body", "")))


# ---------------------------------------------------------------------------
# aq handoff — design §6.1, implementation §5.3
# ---------------------------------------------------------------------------


@cli.command("handoff")
@click.argument("subject", required=False)
@click.argument("detail", required=False)
@click.option(
    "--auto",
    is_flag=True,
    help="Note only, never requests a restart (wired to the PreCompact hook).",
)
@click.option("--task-id", "task_id", default=None, help="Task ID (defaults to $AQ_TASK_ID).")
@click.option(
    "--session-id", "session_id", default=None, help="Session ID (defaults to $AQ_SESSION_ID)."
)
@click.pass_context
@_handle_errors
def handoff(ctx: click.Context, subject, detail, auto, task_id, session_id) -> None:
    """Record a handoff note; request a session restart unless ``--auto`` (design §6.1)."""
    resolved_task_id = task_id or os.environ.get("AQ_TASK_ID")
    resolved_session_id = session_id or os.environ.get("AQ_SESSION_ID")
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _handoff():
        args: dict = {"auto": auto}
        if resolved_task_id:
            args["task_id"] = resolved_task_id
        if resolved_session_id:
            args["session_id"] = resolved_session_id
        if subject:
            args["subject"] = subject
        if detail:
            args["detail"] = detail
        async with _get_client(api_url) as client:
            return await client.execute("task_handoff", args)

    result = _run(_handoff())
    emit(ctx, result)


# ---------------------------------------------------------------------------
# aq inbox --inject — design §6.2, implementation §5.3
#
# STUB (Phase S1): the `messages` table query layer (supervisor-agent) is
# being built in a parallel lane and does not exist at this branch point.
# Per the implementation spec's own S1 note, this ships as a no-op: always
# prints nothing and exits 0 so the UserPromptSubmit hook never blocks a
# human's prompt from reaching the agent (design §6.2's "always exit 0"
# contract holds even before real delivery lands — this just short-circuits
# the 15s daemon round trip entirely for now).
# ---------------------------------------------------------------------------


@cli.command("inbox")
@click.option(
    "--inject",
    is_flag=True,
    help="UserPromptSubmit hook body: print pending messages for prompt injection.",
)
def inbox(inject: bool) -> None:
    """Print pending messages for this session's task (stub — see module docstring)."""
    return
