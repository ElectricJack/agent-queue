"""Report request reads and file-backed supervisor submissions."""

from __future__ import annotations

from pathlib import Path

import click

from .app import _get_client, _handle_errors, _run, cli
from .envelope import emit


def _execute(ctx: click.Context, command: str, params: dict) -> dict:
    async def run():
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.execute(command, params)

    return _run(run())


@cli.group("report")
def report() -> None:
    """Supervisor-authored reports and their bounded briefs."""


@report.command("morning")
@click.option(
    "--dry-run", is_flag=True, required=True, help="Read evidence without writes or sends."
)
@click.option("--since", type=float, help="UTC start as epoch seconds (defaults to 24 hours).")
@click.option(
    "--until", type=float, help="UTC end as epoch seconds (defaults to last zoned 07:00)."
)
@click.option("--project-id", "project_ids", multiple=True)
@click.option("--max-lookback-hours", type=click.IntRange(1, 72), default=72)
@click.pass_context
@_handle_errors
def morning_report(
    ctx: click.Context,
    dry_run: bool,
    since: float | None,
    until: float | None,
    project_ids: tuple[str, ...],
    max_lookback_hours: int,
) -> None:
    """Preview overnight changes and source coverage; no author turn is requested."""
    params = {"max_lookback_hours": max_lookback_hours}
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until
    if project_ids:
        params["project_ids"] = list(project_ids)
    emit(ctx, _execute(ctx, "morning_report_preview", params))


@report.command("request")
@click.argument("request_id")
@click.pass_context
@_handle_errors
def report_request(ctx: click.Context, request_id: str) -> None:
    """Queue the author turn for a reserved request (service only)."""
    emit(ctx, _execute(ctx, "report_request", {"request_id": request_id}))


@report.command("brief")
@click.argument("request_id")
@click.option("--offset", type=int, default=0)
@click.option("--limit", type=int, default=20)
@click.pass_context
@_handle_errors
def report_brief(ctx: click.Context, request_id: str, offset: int, limit: int) -> None:
    """Read the fact and active-work page for one report request."""
    emit(
        ctx,
        _execute(ctx, "report_brief", {"request_id": request_id, "offset": offset, "limit": limit}),
    )


@report.command("submit")
@click.argument("request_id")
@click.option("--file", "file_path", type=click.Path(path_type=Path), required=True)
@click.option("--brief-hash", required=True)
@click.option("--expected-version", type=int, required=True)
@click.option("--evidence-ref", "evidence_refs", multiple=True)
@click.pass_context
@_handle_errors
def report_submit(
    ctx: click.Context,
    request_id: str,
    file_path: Path,
    brief_hash: str,
    expected_version: int,
    evidence_refs: tuple[str, ...],
) -> None:
    """Submit UTF-8 prose from FILE for the exact brief/version shown."""
    try:
        with file_path.open("rb") as stream:
            content = stream.read(8193)
        if len(content) > 8192:
            raise click.UsageError("report file exceeds 8 KiB")
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise click.UsageError(f"not_utf8: {file_path} is not valid UTF-8") from None
    except OSError as exc:
        raise click.UsageError(str(exc)) from None
    emit(
        ctx,
        _execute(
            ctx,
            "report_submit",
            {
                "request_id": request_id,
                "brief_hash": brief_hash,
                "expected_version": expected_version,
                "text": text,
                "evidence_refs": list(evidence_refs),
            },
        ),
    )
