"""``aq playbook`` — CLI subcommands for playbooks.

Currently ships ``aq playbook validate <path>`` (Phase 6).  Additional
playbook commands are auto-generated from the CommandHandler tool
registry via ``register_auto_commands``; this module hosts the
hand-crafted ones that need custom exit-code / output shaping.
"""
from __future__ import annotations

import json
import sys

import click

from .app import _get_client, _run, cli


@cli.group("playbook")
def playbook_group():
    """Playbook commands."""


@playbook_group.command("validate")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def validate(ctx: click.Context, path: str) -> None:
    """Validate a playbook markdown source or compiled JSON artifact.

    Exit 0 on success, 1 on validation failure.  Prints the raw
    ``playbook_validate`` response as JSON.
    """
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _do():
        async with _get_client(api_url) as client:
            return await client.execute("playbook_validate", {"path": path})

    resp = _run(_do())
    click.echo(json.dumps(resp, indent=2, default=str))
    sys.exit(0 if (isinstance(resp, dict) and resp.get("success")) else 1)
