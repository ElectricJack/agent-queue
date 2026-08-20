"""``aq doctor`` and ``aq costs`` — operational health and spend.

Both delegate to the daemon through the REST client (like ``src/cli/tasks.py``);
no business logic lives here.  ``aq doctor`` exits with the runner's exit code
so CI can gate on it (design §5.6):

===== ===========================================
 code  meaning
===== ===========================================
  0    all checks ok or info
  1    at least one warn, no errors
  2    at least one error
  3    doctor itself failed to run (transport, daemon down, no registry)
===== ===========================================
"""

from __future__ import annotations

import sys
from typing import Any

import click

from .app import _get_client, _handle_errors, _run, cli, console

_SEVERITY_STYLE = {
    "ok": "green",
    "info": "cyan",
    "warn": "yellow",
    "error": "bold red",
}


def _getval(obj: Any, key: str, default: Any = None) -> Any:
    """Read a key from a typed response or a dict, normalising Unset."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    val = getattr(obj, key, default)
    if type(val).__name__ == "Unset":
        return default
    return val


def _as_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(obj or {})


@cli.command("doctor")
@click.option("--fix", is_flag=True, help="Apply fixes for failing fixable checks, then re-run.")
@click.option("--json", "as_json", is_flag=True, help="Emit the raw command result.")
@click.option(
    "--check",
    "checks",
    multiple=True,
    help="Run only this check id (repeatable, e.g. --check db.migrations).",
)
@click.pass_context
@_handle_errors
def doctor(ctx: click.Context, fix: bool, as_json: bool, checks: tuple[str, ...]) -> None:
    """Check whether this install is healthy, and what to do about it."""
    from rich.table import Table

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _fetch():
        async with _get_client(api_url) as client:
            return await client.execute(
                "doctor", {"fix": fix, "checks": list(checks) or None}
            )

    try:
        result = _run(_fetch())
    except Exception as exc:  # transport / daemon failure → exit 3
        console.print(f"[bold red]doctor failed to run:[/] {exc}")
        sys.exit(3)

    if as_json or (ctx.obj or {}).get("json"):
        console.print_json(data=_as_dict(result))
        sys.exit(int(_getval(result, "exit_code", 3) or 0))

    if _getval(result, "success") is False:
        console.print(f"[bold red]doctor failed to run:[/] {_getval(result, 'error', '')}")
        sys.exit(3)

    rows = _getval(result, "checks", []) or []
    table = Table(title="aq doctor", show_lines=False)
    table.add_column("check", style="bold")
    table.add_column("severity")
    table.add_column("detail", overflow="fold")
    table.add_column("ms", justify="right")
    for row in rows:
        row = _as_dict(row)
        severity = str(row.get("severity", "?"))
        marker = " [dim](fixed)[/]" if row.get("fix_applied") else ""
        table.add_row(
            str(row.get("id", "?")),
            f"[{_SEVERITY_STYLE.get(severity, 'white')}]{severity}[/]",
            str(row.get("detail", "")) + marker,
            str(row.get("duration_ms", 0)),
        )
    console.print(table)

    summary = _as_dict(_getval(result, "summary", {}))
    console.print(
        "[green]{ok} ok[/] · [cyan]{info} info[/] · [yellow]{warn} warn[/] · "
        "[bold red]{error} error[/] · {fixes_applied} fix(es) applied".format(
            ok=summary.get("ok", 0),
            info=summary.get("info", 0),
            warn=summary.get("warn", 0),
            error=summary.get("error", 0),
            fixes_applied=summary.get("fixes_applied", 0),
        )
    )
    if not fix and any(
        _as_dict(r).get("fixable") and _as_dict(r).get("severity") in ("warn", "error")
        for r in rows
    ):
        console.print("[dim]Some findings are fixable — re-run with --fix.[/]")

    sys.exit(int(_getval(result, "exit_code", 0) or 0))


@cli.command("costs")
@click.option("--project", "project_id", default=None, help="Restrict to one project.")
@click.option("--since", default=None, help="'7d', '12h', or 'YYYY-MM-DD'. Omit for all time.")
@click.option(
    "--group-by",
    "group_by",
    type=click.Choice(["project", "profile", "day"]),
    default="project",
    show_default=True,
)
@click.option("--json", "as_json", is_flag=True, help="Emit the raw command result.")
@click.pass_context
@_handle_errors
def costs(
    ctx: click.Context,
    project_id: str | None,
    since: str | None,
    group_by: str,
    as_json: bool,
) -> None:
    """Show token spend rolled up into USD.

    Rows without a model or without an input/output split are never priced at
    a guessed rate — their tokens are reported as "unpriced" instead.
    """
    from rich.table import Table

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _fetch():
        async with _get_client(api_url) as client:
            return await client.execute(
                "get_costs",
                {"project_id": project_id, "since": since, "group_by": group_by},
            )

    result = _run(_fetch())

    if as_json or (ctx.obj or {}).get("json"):
        console.print_json(data=_as_dict(result))
        return

    error = _getval(result, "error")
    if error:
        console.print(f"[bold red]{error}[/]")
        return

    rows = _getval(result, "rows", []) or []
    table = Table(title=f"aq costs (by {group_by})")
    table.add_column(group_by, style="bold")
    table.add_column("model")
    table.add_column("input", justify="right")
    table.add_column("output", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("cost (USD)", justify="right")
    for row in rows:
        row = _as_dict(row)
        cost = row.get("cost_usd")
        table.add_row(
            str(row.get("group", "?")),
            str(row.get("model") or "[dim]unknown[/]"),
            f"{row.get('input_tokens', 0):,}",
            f"{row.get('output_tokens', 0):,}",
            f"{row.get('tokens_used', 0):,}",
            f"${cost:,.4f}" if cost is not None else "[dim]unpriced[/]",
        )
    console.print(table)

    total = _getval(result, "total_cost_usd", 0.0) or 0.0
    unpriced = _getval(result, "unpriced_tokens", 0) or 0
    console.print(f"Total: [bold]${total:,.4f}[/]")
    if unpriced:
        console.print(
            f"[yellow]{unpriced:,} token(s) unpriced[/] "
            "[dim](no model recorded, no input/output split, or no matching "
            "pricing entry — never estimated)[/]"
        )
    if not _getval(result, "pricing_models", []):
        console.print(
            "[dim]No 'pricing:' entries configured — add them to config.yaml to see costs.[/]"
        )
