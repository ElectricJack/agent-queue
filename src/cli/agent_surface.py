"""Agent-facing surface commands: ``aq schema`` (Phase S0).

``aq prime``, ``aq handoff``, and ``aq inbox --inject`` (design §5, §6) join
this module in Phase S1 — see docs/specs/implementation/aq-surface.md §5.3.
Registered in ``app.py`` before ``register_auto_commands`` so the hand-crafted
``schema`` command wins over any future auto-generated one (§5.3 ordering
rule).
"""

from __future__ import annotations

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
