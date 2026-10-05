"""Durable artifact identities: resolve a URI and re-hash the bytes it names.

``aq job retain`` mints these; this group is the other half, the check.  An
object-loop receipt names URIs and digests, and something has to prove the bytes
behind them still exist and still hash true — otherwise a receipt outlives its
evidence without anyone noticing.
"""

from __future__ import annotations

import click

from .app import _get_client, _handle_errors, _run, cli
from .envelope import emit


@cli.group("artifact")
def artifact():
    """Resolve durable artifact identities minted by `aq job retain`."""


@artifact.command("verify")
@click.argument("uri")
@click.option("--sha256", default=None, help="The digest the receipt claims for it.")
@click.pass_context
@_handle_errors
def artifact_verify(ctx, uri, sha256):
    """Re-hash a retained artifact and prove it is what its URI names."""

    async def run():
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            params = {"uri": uri}
            if sha256:
                params["sha256"] = sha256
            return await client.execute("artifact_verify", params)

    emit(ctx, _run(run()))