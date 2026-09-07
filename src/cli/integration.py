"""Hand-crafted operational controls for hierarchical integration trains.

The CLI owns presentation only.  Every command delegates to the daemon's
existing generic execute endpoint, where project scope and LOCAL-operator
authority are enforced.
"""

from __future__ import annotations

from typing import Any

import click

from .app import _get_client, _handle_errors, _run, cli
from .envelope import emit


def _execute(ctx: click.Context, command: str, args: dict[str, Any]) -> None:
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _request():
        async with _get_client(api_url) as client:
            return await client.execute(command, args)

    emit(ctx, _run(_request()), entity="integration")


@cli.group("integration")
def integration() -> None:
    """Inspect and control hierarchical integration trains."""


@integration.command("status")
@click.argument("project_id")
@click.pass_context
@_handle_errors
def integration_status(ctx: click.Context, project_id: str) -> None:
    """Show rollout, readiness, active work, and cleanup for PROJECT_ID."""
    _execute(ctx, "integration_status", {"project_id": project_id})


@integration.command("flush")
@click.argument("project_id")
@click.pass_context
@_handle_errors
def integration_flush(ctx: click.Context, project_id: str) -> None:
    """Request an immediate eligibility pass or train sweep for PROJECT_ID."""
    _execute(ctx, "integration_flush", {"project_id": project_id})


@integration.command("enable")
@click.argument("project_id")
@click.option(
    "--mode",
    type=click.Choice(["disabled", "observe", "hierarchy", "train"]),
    required=True,
)
@click.option("--expected-generation", type=click.IntRange(min=0), required=True)
@click.option("--reason", required=True)
@click.option("--waiver-id")
@click.option("--interval-seconds", type=click.IntRange(min=1))
@click.pass_context
@_handle_errors
def integration_enable(
    ctx: click.Context,
    project_id: str,
    mode: str,
    expected_generation: int,
    reason: str,
    waiver_id: str | None,
    interval_seconds: int | None,
) -> None:
    """CAS PROJECT_ID to MODE using the generation reported by status."""
    args: dict[str, Any] = {
        "project_id": project_id,
        "mode": mode,
        "expected_generation": expected_generation,
        "reason": reason,
    }
    if waiver_id is not None:
        args["waiver_id"] = waiver_id
    if interval_seconds is not None:
        if mode != "train":
            raise click.UsageError("--interval-seconds is only valid with --mode train")
        args["interval_seconds"] = interval_seconds
    _execute(ctx, "integration_enable", args)


@integration.command("waive-history")
@click.argument("project_id")
@click.option("--reason", required=True)
@click.option("--blocker-digest", required=True)
@click.pass_context
@_handle_errors
def integration_waive_history(
    ctx: click.Context,
    project_id: str,
    reason: str,
    blocker_digest: str,
) -> None:
    """Waive only the exact historical blockers reported for PROJECT_ID."""
    _execute(
        ctx,
        "integration_waive_history",
        {
            "project_id": project_id,
            "reason": reason,
            "blocker_digest": blocker_digest,
        },
    )


@integration.command("resume")
@click.argument("operation_id")
@click.pass_context
@_handle_errors
def integration_resume(ctx: click.Context, operation_id: str) -> None:
    """Resume a safe, human-required integration OPERATION_ID."""
    _execute(ctx, "integration_resume", {"operation_id": operation_id})


@integration.command("abort")
@click.argument("operation_id")
@click.option("--reason", required=True)
@click.pass_context
@_handle_errors
def integration_abort(ctx: click.Context, operation_id: str, reason: str) -> None:
    """Abort a safe, human-required integration OPERATION_ID."""
    _execute(ctx, "integration_abort", {"operation_id": operation_id, "reason": reason})


@integration.command("retry-cleanup")
@click.argument("batch_id")
@click.pass_context
@_handle_errors
def integration_retry_cleanup(ctx: click.Context, batch_id: str) -> None:
    """Requeue the exact safe cleanup items for BATCH_ID."""
    _execute(ctx, "integration_retry_cleanup", {"batch_id": batch_id})
