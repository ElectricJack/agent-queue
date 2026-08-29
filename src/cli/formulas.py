"""Hand-crafted ``aq formula show`` / ``aq formula cook`` (swarm-work-model
§13). ``aq formula list`` is auto-generated (see ``auto_commands.py`` —
``HANDCRAFTED_COVERAGE`` excludes it) since it takes no interesting
arguments and a Rich table formatter already renders it via
``formatter_registry.py``.

``show`` and ``cook`` are hand-crafted because both need a repeatable
``--var k=v`` option collected into a dict — Click has no built-in support
for that shape, and ``create_task_graph``'s ``--graph``/``--dry-run``/
``--parent`` CLI (``tasks.py``) is the template this file follows.

Registered in ``app.py`` before ``register_auto_commands()`` so this group
exists first and auto-generation's ``formula list`` command merges into it
rather than creating a second ``formula`` group.
"""

from __future__ import annotations

from typing import Any

import click

from .app import cli, console, _run, _get_client, _handle_errors
from .envelope import emit


def _parse_vars(pairs: tuple[str, ...]) -> dict[str, str]:
    """Turn repeated ``--var k=v`` options into a dict.

    Entries without an ``=`` are rejected — silently dropping a malformed
    ``--var`` would leave a required formula var looking "supplied" from
    the CLI's point of view when it never made it into the request.
    """
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.UsageError(f"--var must be 'key=value', got {pair!r}")
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key:
            raise click.UsageError(f"--var must be 'key=value', got {pair!r}")
        result[key] = value
    return result


@cli.group()
def formula() -> None:
    """Reusable task-graph templates — list, show (resolve, read-only), cook."""
    pass


def _render_show(data: dict) -> None:
    verb = "as-cooked snapshot of" if data.get("as_cooked") else "formula"
    console.print(
        f"[bold bright_cyan]{data.get('name', '?')}[/] "
        f"({verb} — scope={data.get('scope')}, path={data.get('path')})"
    )
    chain = data.get("chain")
    if chain:
        console.print(f"  chain: {' -> '.join(chain)}")
    if data.get("chain_sha"):
        console.print(f"  chain_sha: {data['chain_sha']}")
    var_info = data.get("vars") or {}
    effective = var_info.get("effective") or {}
    if effective:
        console.print("  vars:")
        for key, val in effective.items():
            console.print(f"    {key} = {val!r}")
    errors = data.get("errors") or []
    for err in errors:
        where = f" ({err['node']})" if isinstance(err, dict) and err.get("node") else ""
        detail = err.get("detail") if isinstance(err, dict) else err
        rule = err.get("rule") if isinstance(err, dict) else ""
        console.print(f"  [bold red]error[/] {rule}{where}: {detail}")
    for warning in data.get("warnings") or []:
        where = f" ({warning['node']})" if isinstance(warning, dict) and warning.get("node") else ""
        detail = warning.get("detail") if isinstance(warning, dict) else warning
        rule = warning.get("rule") if isinstance(warning, dict) else ""
        console.print(f"  [yellow]warning[/] {rule}{where}: {detail}")

    graph = data.get("graph") or {}
    nodes = graph.get("nodes") or []
    if nodes:
        from rich.table import Table

        table = Table(border_style="bright_black")
        table.add_column("Key", style="bold bright_cyan")
        table.add_column("Title")
        table.add_column("Needs")
        for node in nodes:
            needs = node.get("needs") or []
            needs_str = ", ".join(n.get("on", "") if isinstance(n, dict) else str(n) for n in needs)
            table.add_row(node.get("key", ""), node.get("title", ""), needs_str or "-")
        console.print(table)


@formula.command("show")
@click.argument("name", required=False)
@click.option("-p", "--project-id", "project_id", default=None, help="Project scope")
@click.option(
    "--var",
    "var_pairs",
    multiple=True,
    metavar="KEY=VALUE",
    help="Supplied var value (repeatable, e.g. --var branch=feat/x).",
)
@click.option(
    "--as-cooked",
    "as_cooked",
    default=None,
    metavar="CONTAINER_ID",
    help="Render the formula_snapshot a previous cook wrote for this container.",
)
@click.pass_context
@_handle_errors
def formula_show(
    ctx: click.Context,
    name: str | None,
    project_id: str | None,
    var_pairs: tuple[str, ...],
    as_cooked: str | None,
) -> None:
    """Resolve a formula's extends chain, substitute its vars, and validate
    — read-only, never writes. With --as-cooked, render back the snapshot a
    previous 'aq formula cook' actually wrote for that container instead.
    """
    if not name and not as_cooked:
        raise click.UsageError("NAME or --as-cooked is required")

    api_url = ctx.obj.get("api_url") if ctx.obj else None
    params: dict[str, Any] = {}
    if as_cooked:
        params["as_cooked"] = as_cooked
    else:
        params["name"] = name
        if project_id:
            params["project_id"] = project_id
        var_map = _parse_vars(var_pairs)
        if var_map:
            params["vars"] = var_map

    async def _show():
        async with _get_client(api_url) as client:
            return await client.execute("formula_show", params)

    result = _run(_show())
    emit(ctx, result, render=_render_show)


def _render_cook(data: dict) -> None:
    verb = "Would cook" if data.get("dry_run") else "Cooked"
    console.print(
        f"[bold green]{verb}[/] into container [bold bright_cyan]{data.get('container_id')}[/]"
    )
    node_ids = [n.get("task_id", "") for n in data.get("nodes", [])]
    if node_ids:
        console.print(f"  nodes: {', '.join(node_ids)}")
    for warning in data.get("warnings") or []:
        where = f" ({warning['node']})" if isinstance(warning, dict) and warning.get("node") else ""
        detail = warning.get("detail") if isinstance(warning, dict) else warning
        rule = warning.get("rule") if isinstance(warning, dict) else ""
        console.print(f"  [yellow]warning[/] {rule}{where}: {detail}")


@formula.command("cook")
@click.argument("name")
@click.option("-p", "--project-id", "project_id", required=True, help="Owning project")
@click.option(
    "--var",
    "var_pairs",
    multiple=True,
    metavar="KEY=VALUE",
    help="Supplied var value (repeatable, e.g. --var branch=feat/x).",
)
@click.option(
    "--parent",
    "parent_id",
    default=None,
    help="Cook the graph under an existing container instead of a new one.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Validate and report what would be created without writing.",
)
@click.pass_context
@_handle_errors
def formula_cook(
    ctx: click.Context,
    name: str,
    project_id: str,
    var_pairs: tuple[str, ...],
    parent_id: str | None,
    dry_run: bool,
) -> None:
    """Resolve a formula, validate it, and create the resulting task graph
    in one transaction. Not available to agent sessions.
    """
    api_url = ctx.obj.get("api_url") if ctx.obj else None
    params: dict[str, Any] = {"name": name, "project_id": project_id, "dry_run": dry_run}
    var_map = _parse_vars(var_pairs)
    if var_map:
        params["vars"] = var_map
    if parent_id:
        params["parent_id"] = parent_id

    async def _cook():
        async with _get_client(api_url) as client:
            return await client.execute("formula_cook", params)

    result = _run(_cook())
    emit(ctx, result, render=_render_cook)
