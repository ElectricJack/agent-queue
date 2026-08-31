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
from .claim_epoch import claim_epoch_option, read_claim_epoch, resolve_claim_epoch  # noqa: F401
from .envelope import emit
from .tasks import task


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
@claim_epoch_option
@click.pass_context
@_handle_errors
def handoff(ctx: click.Context, subject, detail, auto, task_id, session_id, claim_epoch) -> None:
    """Record a handoff note; request a session restart unless ``--auto`` (design §6.1)."""
    resolved_task_id = task_id or os.environ.get("AQ_TASK_ID")
    resolved_session_id = session_id or os.environ.get("AQ_SESSION_ID")
    resolved_epoch = resolve_claim_epoch(claim_epoch)
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
        if resolved_epoch is not None:
            args["claim_epoch"] = resolved_epoch
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
# prints nothing and exits 0.
#
# 2026-08-27: the query layer landed long ago under `aq message inbox
# --inject` and this was never repointed at it.  The UserPromptSubmit hook
# that called this has been removed rather than left paying ~1.3 s of
# interpreter startup per prompt for a command that returns immediately.
# The stub stays so an already-launched session whose rendered hook file
# still names it does not start failing; anything wanting real prompt-
# boundary injection should call `aq message inbox --inject` and be
# measured against the nudge path first.
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


# ---------------------------------------------------------------------------
# aq task claim|close|heartbeat|set — pull-based work selection
# (swarm-work-model §10). Hand-crafted (not auto-generated) because the CLI
# ergonomics (a positional TASK_ID, and reading claim_epoch from
# .aq/claim.json / $AQ_CLAIM_EPOCH rather than making the caller pass it)
# don't fit the generic ``--property-name`` auto layer.  Registered on the
# ``task`` group tasks.py defines, before ``register_auto_commands`` runs
# (see app.py's import order), so these win over any auto-generated
# ``aq task claim|close|heartbeat|set``.
# ---------------------------------------------------------------------------


@task.command("claim")
@click.argument("task_id", required=False)
@click.option(
    "--next", "claim_next", is_flag=True, help="Claim whatever ready task matches this profile."
)
@click.option(
    "--wait",
    type=int,
    default=None,
    help="Seconds to long-poll for ready work before returning no_ready_work (optional).",
)
@click.pass_context
@_handle_errors
def task_claim(ctx: click.Context, task_id, claim_next, wait) -> None:
    """Claim a ready task for this session (pull-based work selection, §10)."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None
    args: dict = {}
    if task_id:
        args["task_id"] = task_id
    if claim_next:
        args["next"] = True
    if wait is not None:
        args["wait"] = wait

    async def _claim():
        async with _get_client(api_url) as client:
            return await client.execute("task_claim", args)

    result = _run(_claim())

    def _render(data: dict) -> None:
        result_code = data.get("result", "?")
        t = data.get("task") or {}
        line = f"[bold]{result_code}[/]"
        if t:
            line += f": {t.get('id')} — {t.get('title', '')}"
        console.print(line)
        if data.get("claim_epoch") is not None:
            console.print(f"claim_epoch={data['claim_epoch']}")
        if data.get("reason"):
            console.print(f"[dim]{data['reason']}[/]")

    emit(ctx, result, render=_render)


@task.command("close")
@click.argument("task_id", required=False)
@click.option(
    "--outcome", type=click.Choice(["pass", "fail"]), required=True, help="Overall task outcome."
)
@click.option("--summary", default=None, help="Summary for the reviewer/dashboard/vault note.")
@click.option(
    "--failure-class",
    "failure_class",
    type=click.Choice(["transient", "hard"]),
    default=None,
    help="Failure classification, when --outcome fail.",
)
@click.option(
    "--work-outcome",
    "work_outcome",
    type=click.Choice(["shipped", "no-op", "blocked", "abandoned"]),
    default=None,
)
@click.option("--commit", default=None, help="Commit SHA (optional).")
@click.option("--notes", default=None, help="Closing notes (optional).")
@click.option("--changes", default=None, help="What changed while completing the task.")
@click.option("--verification", default=None, help="How the work was verified.")
@click.option("--test", "tests", multiple=True, help="Test command run; repeatable.")
@click.option("--command", "commands", multiple=True, help="Other command run; repeatable.")
@click.option("--abandon-children", is_flag=True, help="Abandon open child tasks.")
@click.option(
    "--claim-next",
    "claim_next",
    is_flag=True,
    help="Immediately claim the next ready task after closing (pool worker loop).",
)
@click.option(
    "--wait", type=int, default=None, help="Seconds to long-poll for the next claim (optional)."
)
@claim_epoch_option
@click.pass_context
@_handle_errors
def task_close(
    ctx: click.Context,
    task_id,
    outcome,
    summary,
    failure_class,
    work_outcome,
    commit,
    notes,
    changes,
    verification,
    tests,
    commands,
    abandon_children,
    claim_next,
    wait,
    claim_epoch,
) -> None:
    """Close TASK_ID with an outcome; only way a session-run task reaches COMPLETED.

    TASK_ID is optional: omit it and the daemon closes whichever task the
    calling session currently holds (``sessions.task_id``).  That is what
    the pool bootstrap prompt and ``aq-tasks`` tell workers to run --
    ``aq task close --outcome pass --claim-next`` -- since a pool worker's
    task changes with every claim.
    """
    api_url = ctx.obj.get("api_url") if ctx.obj else None
    resolved_epoch = resolve_claim_epoch(claim_epoch)
    args: dict = {"outcome": outcome}
    if task_id:
        args["task_id"] = task_id
    if summary:
        args["summary"] = summary
    if failure_class:
        args["failure_class"] = failure_class
    if work_outcome:
        args["work_outcome"] = work_outcome
    if commit:
        args["commit"] = commit
    if notes:
        args["notes"] = notes
    if changes:
        args["changes"] = changes
    if verification:
        args["verification"] = verification
    if tests:
        args["tests"] = list(tests)
    if commands:
        args["commands"] = list(commands)
    if abandon_children:
        args["abandon_children"] = True
    if claim_next:
        args["claim_next"] = True
    if wait is not None:
        args["wait"] = wait
    if resolved_epoch is not None:
        args["claim_epoch"] = resolved_epoch

    async def _close():
        async with _get_client(api_url) as client:
            return await client.execute("task_close", args)

    result = _run(_close())
    emit(ctx, result)


@task.command("heartbeat")
@click.argument("task_id", required=False)
@claim_epoch_option
@click.pass_context
@_handle_errors
def task_heartbeat(ctx: click.Context, task_id, claim_epoch) -> None:
    """Refresh this task's agent lease so the stall ladder doesn't climb."""
    api_url = ctx.obj.get("api_url") if ctx.obj else None
    resolved_epoch = resolve_claim_epoch(claim_epoch)
    args: dict = {}
    if task_id:
        args["task_id"] = task_id
    if resolved_epoch is not None:
        args["claim_epoch"] = resolved_epoch

    async def _heartbeat():
        async with _get_client(api_url) as client:
            return await client.execute("task_heartbeat", args)

    result = _run(_heartbeat())
    emit(ctx, result)
