"""Harness-independent recurring prompts through the daemon."""

from __future__ import annotations

import click

from .app import _get_client, _handle_errors, _run, cli
from .claim_epoch import claim_epoch_option, resolve_claim_epoch
from .envelope import emit


def _execute(ctx, command, params):
    async def run():
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.execute(command, {k: v for k, v in params.items() if v is not None})

    return _run(run())


@cli.group("cron")
def cron():
    """Session-owned recurring prompts. Registration waits for a future tick.

    Daemon restarts retain schedules; owner-session replacement expires them.
    Missed ticks coalesce. Delivery uses idle-session messages with bounded retries.
    """


@cron.command("register")
@click.option("--prompt", required=True, help="Prompt to queue; does not execute on registration.")
@click.option("--idempotency-key", required=True, help="Stable key per owner session instance.")
@click.option(
    "--every", type=click.FloatRange(60, 604800), help="Interval in seconds, epoch-aligned."
)
@click.option(
    "--offset",
    type=click.FloatRange(min=0),
    default=0.0,
    help="Seconds from interval boundary, less than --every (900/120: :02/:17/:32/:47).",
)
@click.option(
    "--cron", "expression", help="MINUTE HOUR * * *; minute 0–59, * or */N; hour 0–23 or *."
)
@click.option(
    "--timezone", default="UTC", show_default=True, help="IANA zone for local cron times."
)
@click.option("--session-id", default=None, help="Local operator only: live owner session ID.")
@claim_epoch_option
@click.pass_context
@_handle_errors
def cron_register(
    ctx, prompt, idempotency_key, every, offset, expression, timezone, session_id, claim_epoch
):
    """Create once or recover the identical schedule; choose --every or --cron.

    Example: aq cron register --every 900 --offset 120 --idempotency-key patrol-v1
    --prompt 'Run the authorized patrol and handle findings.'
    """
    if (every is None) == (expression is None):
        raise click.UsageError("choose exactly one of --every or --cron")
    emit(
        ctx,
        _execute(
            ctx,
            "cron_register",
            dict(
                prompt=prompt,
                idempotency_key=idempotency_key,
                every=every,
                offset=offset,
                cron=expression,
                timezone=timezone,
                session_id=session_id,
                claim_epoch=resolve_claim_epoch(claim_epoch),
            ),
        ),
    )


@cron.command("show")
@click.argument("schedule_id")
@click.option("--consume", is_flag=True, help="Consume only this schedule's pending notification.")
@click.option("--session-id", default=None, help="Local operator only: live owner session ID.")
@claim_epoch_option
@click.pass_context
@_handle_errors
def cron_show(ctx, schedule_id, consume, session_id, claim_epoch):
    """Read prompt, next-fire, state, coalescing and last-delivery diagnostics."""
    emit(
        ctx,
        _execute(
            ctx,
            "cron_get",
            dict(
                schedule_id=schedule_id,
                consume=consume,
                session_id=session_id,
                claim_epoch=resolve_claim_epoch(claim_epoch),
            ),
        ),
    )


@cron.command("list")
@click.option("--session-id", default=None, help="Local operator only: live owner session ID.")
@click.option("--limit", type=click.IntRange(1, 100), default=100)
@click.option("--offset", type=click.IntRange(min=0), default=0)
@click.pass_context
@_handle_errors
def cron_list(ctx, session_id, limit, offset):
    """List this owner instance's schedules after reconnect; no project is required."""
    emit(ctx, _execute(ctx, "cron_list", dict(session_id=session_id, limit=limit, offset=offset)))


@cron.command("cancel")
@click.argument("schedule_id")
@click.option("--session-id", default=None, help="Local operator only: live owner session ID.")
@claim_epoch_option
@click.pass_context
@_handle_errors
def cron_cancel(ctx, schedule_id, session_id, claim_epoch):
    """Cancel future ticks and pending wake-up delivery, preserving history."""
    emit(
        ctx,
        _execute(
            ctx,
            "cron_cancel",
            dict(
                schedule_id=schedule_id,
                session_id=session_id,
                claim_epoch=resolve_claim_epoch(claim_epoch),
            ),
        ),
    )
