"""``aq batch`` — held spec-ingestion batches (held batches §5, rollout #6).

``show`` reads one batch through the REST detail route
(``GET /api/projects/{project_id}/batches/{batch_id}``). The mutators are the
schema-generated commands mounted under batch-shaped names, so their flags
follow the command contracts. Authority stays with the daemon: validate,
update and review-record answer only the live assigned fidelity reviewer (or
the supervisor/operator), release and retire only ingestion policy, the
project supervisor or the operator. Nothing here offers a bypass.
"""

from __future__ import annotations

import click

from .app import _handle_errors, cli, console
from .auto_commands import _ALL_TOOL_DEFINITIONS, _make_auto_command
from .envelope import emit

#: ``aq batch <verb>`` → the CommandHandler command it runs.
BATCH_COMMANDS: dict[str, str] = {
    "validate": "task_batch_validate",
    "update": "task_batch_update",
    "review-record": "task_batch_review_record",
    "release": "task_batch_release",
    "retire": "task_batch_retire",
}


@cli.group()
def batch() -> None:
    """Held spec-ingestion batches — show, validate, update, review, release, retire."""


for _verb, _backend in BATCH_COMMANDS.items():
    _definition = next(tool for tool in _ALL_TOOL_DEFINITIONS if tool["name"] == _backend)
    batch.add_command(_make_auto_command(_backend, _verb, _definition, console))


def _render_batch(data: dict) -> None:
    from rich.table import Table

    console.print(
        f"[bold]{data.get('batch_id')}[/bold]  state=[cyan]{data.get('state')}[/cyan]  "
        f"revision={data.get('structure_revision')}  digest={data.get('structure_digest')}"
    )
    console.print(f"source: {data.get('source')}")
    if data.get("reviewer_task_id"):
        console.print(f"reviewer: {data['reviewer_task_id']}")
    membership = data.get("membership") or []
    if membership:
        table = Table("member", "task", "state")
        for member in membership:
            table.add_row(
                str(member.get("member_key")), str(member.get("task_id")),
                str(member.get("state")),
            )
        console.print(table)
    verdicts = data.get("section_verdicts") or {}
    if verdicts:
        console.print("section verdicts: " + ", ".join(
            f"{section}={verdict}" for section, verdict in sorted(verdicts.items())
        ))
    for key in ("reverse_coverage_verdict", "walkthrough_verdict"):
        if data.get(key):
            console.print(f"{key.removesuffix('_verdict').replace('_', ' ')}: {data[key]}")
    console.print(f"receipts: {len(data.get('receipts') or [])}")


@batch.command("show")
@click.argument("batch_id")
@click.option("--project", "project_id", envvar="AQ_PROJECT_ID", required=True,
              help="Project the batch belongs to (default: $AQ_PROJECT_ID).")
@click.pass_context
@_handle_errors
def show(ctx: click.Context, batch_id: str, project_id: str) -> None:
    """Show a held batch: revision, digest, membership, verdicts and receipts.

    Read-only; one repeatable-read snapshot of the batch and its evidence.
    Use the revision and digest it prints as --expected-revision and
    --expected-digest for review-record and release.
    """
    from . import app

    async def execute():
        async with app._get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.get_ingest_batch(project_id, batch_id)

    emit(ctx, app._run(execute()), render=_render_batch)
