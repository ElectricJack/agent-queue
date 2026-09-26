"""Finite preset submission and durable job result pointers."""

from __future__ import annotations

import uuid
import click

from .app import _get_client, _handle_errors, _run, cli
from .claim_epoch import claim_epoch_option, resolve_claim_epoch
from .envelope import emit


def _execute(ctx, command, params):
    async def run():
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.execute(command, params)

    return _run(run())


def submit(
    ctx,
    *,
    preset,
    argv,
    wait=False,
    idempotency_key=None,
    claim_epoch=None,
    project_id=None,
    task_id=None,
    session_id=None,
):
    # Mint before contacting the daemon; no local fallback on an ambiguous response.
    key = idempotency_key or str(uuid.uuid4())
    params = dict(
        preset=preset,
        argv=list(argv),
        wait=wait,
        idempotency_key=key,
        claim_epoch=resolve_claim_epoch(claim_epoch),
        project_id=project_id,
        task_id=task_id,
        session_id=session_id,
    )
    click.echo(f"Job submission key: {key}", err=True)
    return _execute(ctx, "job_submit", {k: v for k, v in params.items() if v is not None})


@cli.group("job")
def job():
    """Submit finite presets and read their durable results."""


@job.command("submit")
@click.option("--preset", required=True)
@click.option("--idempotency-key", default=None)
@click.option("--wait", is_flag=True, help="Atomically register a wait and end the turn.")
@click.option("--project-id", default=None)
@click.option("--task-id", default=None)
@click.option("--session-id", default=None, help="Owner session for local operator waits.")
@claim_epoch_option
@click.argument("argv", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
@_handle_errors
def job_submit(ctx, **kwargs):
    """Submit a server-validated preset; arguments follow --."""
    emit(ctx, submit(ctx, **kwargs))


@cli.command("run")
@click.option("--preset", required=True)
@click.option("--idempotency-key", default=None)
@click.option("--wait", is_flag=True)
@claim_epoch_option
@click.argument("argv", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
@_handle_errors
def run(ctx, **kwargs):
    """Alias for job submit using the same finite presets and identity."""
    emit(ctx, submit(ctx, **kwargs))


@job.command("show")
@click.argument("job_id")
@click.pass_context
@_handle_errors
def job_show(ctx, job_id):
    emit(ctx, _execute(ctx, "job_get", {"job_id": job_id}))


@job.command("list")
@click.option("--project-id", default=None)
@click.option("--task", "task_id", default=None)
@click.option("--limit", type=click.IntRange(1, 100), default=50)
@click.pass_context
@_handle_errors
def job_list(ctx, **params):
    emit(ctx, _execute(ctx, "job_list", {k: v for k, v in params.items() if v is not None}))


@job.command("cancel")
@click.argument("job_id")
@click.pass_context
@_handle_errors
def job_cancel(ctx, job_id):
    emit(ctx, _execute(ctx, "job_cancel", {"job_id": job_id}))


@job.command("result")
@click.argument("job_id")
@click.option("--max-bytes", type=click.IntRange(0, 8192), default=8192)
@click.pass_context
@_handle_errors
def job_result(ctx, **params):
    emit(ctx, _execute(ctx, "job_result", params))


@job.command("logs")
@click.argument("job_id")
@click.option("--after", type=click.IntRange(min=0), default=0)
@click.option("--limit", type=click.IntRange(1, 1048576), default=65536)
@click.pass_context
@_handle_errors
def job_logs(ctx, **params):
    emit(ctx, _execute(ctx, "job_logs", params))
