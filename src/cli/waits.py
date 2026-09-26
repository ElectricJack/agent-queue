"""Register a typed wait, then read its durable result pointer."""

from __future__ import annotations

import click

from .app import _get_client, _handle_errors, _run, cli
from .claim_epoch import claim_epoch_option, resolve_claim_epoch
from .envelope import emit


def _execute(ctx, command, params):
    async def run():
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.execute(command, params)

    return _run(run())


@cli.group("wait")
def wait():
    """Durable task, message and timer waits with bounded deadlines."""


@wait.command("register")
@click.option("--kind", type=click.Choice(["job", "task", "message", "timer"]), required=True)
@click.option("--ref", default=None, help="Task ID or authorized message thread ID.")
@click.option("--after-seq", type=click.IntRange(min=0), default=None)
@click.option("--due-at", type=float, default=None, help="Timer due instant in UTC epoch seconds.")
@click.option("--timeout", type=click.FloatRange(min=0, min_open=True, max=86400), default=None)
@click.option("--idempotency-key", required=True)
@click.option("--project-id", default=None)
@claim_epoch_option
@click.pass_context
@_handle_errors
def wait_register(
    ctx, kind, ref, after_seq, due_at, timeout, idempotency_key, project_id, claim_epoch
):
    """Register one condition and end the turn until its result pointer arrives."""
    params = dict(
        kind=kind,
        ref=ref,
        after_seq=after_seq,
        due_at=due_at,
        timeout=timeout,
        idempotency_key=idempotency_key,
        project_id=project_id,
        claim_epoch=resolve_claim_epoch(claim_epoch),
    )
    emit(ctx, _execute(ctx, "wait_register", {k: v for k, v in params.items() if v is not None}))


@wait.command("show")
@click.argument("wait_id")
@click.pass_context
@_handle_errors
def wait_show(ctx, wait_id):
    """Read the wait's state, bounded digest and result reference."""
    emit(ctx, _execute(ctx, "wait_get", {"wait_id": wait_id}))


@wait.command("list")
@click.option("--project-id", default=None)
@click.option("--limit", type=click.IntRange(1, 100), default=100)
@click.option("--offset", type=click.IntRange(min=0), default=0)
@click.pass_context
@_handle_errors
def wait_list(ctx, project_id, limit, offset):
    """Read current task history, including waits from earlier claims."""
    params = {"limit": limit, "offset": offset}
    if project_id is not None:
        params["project_id"] = project_id
    emit(ctx, _execute(ctx, "wait_list", params))


@wait.command("cancel")
@click.argument("wait_id")
@claim_epoch_option
@click.pass_context
@_handle_errors
def wait_cancel(ctx, wait_id, claim_epoch):
    """Cancel a wait held by the current claim, preserving the result."""
    params = {"wait_id": wait_id}
    epoch = resolve_claim_epoch(claim_epoch)
    if epoch is not None:
        params["claim_epoch"] = epoch
    emit(ctx, _execute(ctx, "wait_cancel", params))
