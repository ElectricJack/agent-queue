"""Provider allocation under ``aq pool provider``.

Status and apply use the backend schemas unchanged. Preview translates the
operator's lifecycle, bounds and routing flags into the structured request.
"""

from __future__ import annotations

import click

from .app import cli, console, _handle_errors
from .auto_commands import _ALL_TOOL_DEFINITIONS, _make_auto_command
from .envelope import emit


@cli.group()
def pool() -> None:
    """Worker pools — inspect, scale and allocate workers by provider."""


@pool.group()
def provider() -> None:
    """Provider allocation — status, preview a change, then apply its token."""


for _verb in ("status", "apply"):
    _backend = f"provider_allocation_{_verb}"
    _definition = next(tool for tool in _ALL_TOOL_DEFINITIONS if tool["name"] == _backend)
    provider.add_command(_make_auto_command(_backend, _verb, _definition, console))


def _maximum(_ctx: click.Context, param: click.Parameter, value: str | None) -> int | str | None:
    if value is None or value == "unbounded":
        return value
    return click.IntRange(min=0).convert(value, param, _ctx)


@provider.command("preview")
@click.option("--provider", "provider_key", required=True, help="Provider key or vendor.")
@click.option("--profiles", help="Comma-separated profile ids; omit for all eligible workers.")
@click.option("--lifecycle", type=click.Choice(["pool", "task"]))
@click.option("--min", "minimum", type=click.IntRange(min=0), help="Floor per pool profile.")
@click.option(
    "--max",
    "maximum",
    callback=_maximum,
    metavar="N|unbounded",
    help="Ceiling per pool profile; unbounded removes it.",
)
@click.option("--receive-new-work", metavar="PROJECT", help="Prefer this provider for a project.")
@click.option("--clear-new-work", metavar="PROJECT", help="Clear a project's provider preference.")
@click.option(
    "--drain",
    type=click.Choice(["graceful", "idle-now", "interrupt-busy"]),
    help="Default: graceful. Busy interruption requires operator scope.",
)
@click.option(
    "--allow-pinned-wait",
    is_flag=True,
    help="Acknowledge pinned READY work left on profiles leaving the pool.",
)
@click.pass_context
@_handle_errors
def preview(
    ctx: click.Context,
    provider_key: str,
    profiles: str | None,
    lifecycle: str | None,
    minimum: int | None,
    maximum: int | str | None,
    receive_new_work: str | None,
    clear_new_work: str | None,
    drain: str | None,
    allow_pinned_wait: bool,
) -> None:
    """Review profile changes, sessions, pins and the aggregate provider ceiling.

    Read-only. Apply accepts the returned preview token and authorizations;
    it refuses a stale preview instead of changing the reviewed selection.
    """
    from . import app
    from .formatter_registry import apply_formatter

    if receive_new_work is not None and clear_new_work is not None:
        raise click.UsageError("Use either --receive-new-work or --clear-new-work.")
    if isinstance(maximum, int) and minimum is not None and minimum > maximum:
        raise click.UsageError("--min must be no greater than --max.")
    params: dict = {"provider": provider_key}
    if profiles is not None:
        ids = [item.strip() for item in profiles.split(",")]
        if not all(ids):
            raise click.BadParameter(
                "Supply non-empty comma-separated ids.", param_hint="--profiles"
            )
        params["profile_ids"] = ids
    if lifecycle is not None:
        params["participation"] = lifecycle
    bounds = {}
    if minimum is not None:
        bounds["min"] = minimum
    if maximum is not None:
        bounds["max"] = None if maximum == "unbounded" else maximum
    if bounds:
        params["bounds"] = bounds
    if receive_new_work is not None or clear_new_work is not None:
        params["receive_new_work"] = {
            "project_id": receive_new_work if receive_new_work is not None else clear_new_work,
            "mode": "prefer" if receive_new_work is not None else "clear",
        }
    if drain is not None:
        params["drain"] = drain
    if allow_pinned_wait:
        params["allow_pinned_wait"] = True

    async def execute():
        async with app._get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.execute("provider_allocation_preview", params)

    result = app._run(execute())
    emit(
        ctx,
        result,
        render=lambda data: apply_formatter("provider_allocation_preview", data, console),
    )
