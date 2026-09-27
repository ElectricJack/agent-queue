"""Bounded long-poll attached to the durable collaboration message wait."""

from __future__ import annotations

import click

from .app import _get_client, _handle_errors, _run
from .claim_epoch import claim_epoch_option, resolve_claim_epoch
from .envelope import emit
from .messages import message


@message.command("wait")
@click.option("--thread", "thread_id", required=True, help="Collaboration thread ID")
@click.option("--after", "after_seq", required=True, type=click.IntRange(min=0))
@click.option("--timeout", default=60, type=click.IntRange(1, 60), show_default=True)
@click.option("--idempotency-key", default=None, help="Defaults to a thread and cursor retry key")
@claim_epoch_option
@click.pass_context
@_handle_errors
def message_wait(ctx, thread_id, after_seq, timeout, idempotency_key, claim_epoch):
    """Wait for messages; on timeout end the turn with the durable wait still active."""
    params = {"thread_id": thread_id, "after_seq": after_seq, "timeout": timeout}
    if idempotency_key is not None:
        params["idempotency_key"] = idempotency_key
    epoch = resolve_claim_epoch(claim_epoch)
    if epoch is not None:
        params["claim_epoch"] = epoch

    async def run():
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.execute("message_wait", params)

    emit(ctx, _run(run()))
