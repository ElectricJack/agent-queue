"""Promotion flow validation and PR-backed promotion requests."""

from __future__ import annotations

import json
from pathlib import Path

import click

from .app import _get_client, _handle_errors, _run, cli
from .envelope import emit


@cli.group()
def promote() -> None:
    """Validate promotion flows and manage pinned step PRs."""


def _render(data: dict) -> None:
    click.echo(json.dumps(data, indent=2))


@promote.command("schema")
@click.pass_context
@_handle_errors
def promote_schema(ctx: click.Context) -> None:
    """Print the published promotion-flow JSON schema."""

    async def run():
        async with _get_client() as client:
            return await client.execute("promote_schema", {})

    emit(ctx, _run(run()), render=lambda data: _render(data["schema"]))


@promote.command("validate")
@click.option(
    "--project", "project_id", required=True, help="Project whose repository and trust to use."
)
@click.option(
    "--file",
    "flow_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="YAML or JSON flow; omit to validate the stored flow.",
)
@click.option("--remote", is_flag=True, help="Read remote rulesets and workflow triggers.")
@click.pass_context
@_handle_errors
def promote_validate(
    ctx: click.Context, project_id: str, flow_file: Path | None, remote: bool
) -> None:
    """Validate schema, chain and trust, with JSON pointers for every problem."""
    args = {"project_id": project_id, "remote": remote, "use_stored": flow_file is None}
    if flow_file is not None:
        import yaml

        try:
            args["flow"] = yaml.safe_load(flow_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            from .envelope import emit_error

            emit_error("flow_schema_invalid", str(exc), {"pointer": "", "layer": 1})
            raise SystemExit(1) from exc

    async def run():
        async with _get_client() as client:
            return await client.execute("promote_validate", args)

    data = _run(run())
    emit(ctx, data, render=_render)
    if not data.get("valid"):
        raise SystemExit(1)


# Intent commands register separately from schema/remote-policy tooling.
def _execute_intent(ctx: click.Context, command: str, args: dict) -> None:
    async def run():
        async with _get_client() as client:
            return await client.execute(command, args)

    data = _run(run())
    emit(ctx, data, render=_render)
    if not data.get("success"):
        raise SystemExit(1)


@promote.command("prepare")
@click.option("--project", "project_id", envvar="AQ_PROJECT_ID", required=True)
@click.option("--step", "step_id", required=True)
@click.option("--bump", type=click.Choice(["minor", "patch"]))
@click.option("--version", help="Explicit version to prepare.")
@click.option("--from-task", help="Task motivating this preparation.")
@click.pass_context
@_handle_errors
def promote_prepare(ctx, project_id, step_id, bump, version, from_task):
    """File a version bump and notes draft as ordinary work on the default branch."""
    if (version is None) == (bump is None):
        raise click.UsageError("Choose exactly one of --version or --bump.")
    _execute_intent(ctx, "promote_prepare", {
        "project_id": project_id, "step_id": step_id, "bump": bump,
        "version": version, "from_task": from_task,
    })


@promote.command("request")
@click.option("--project", "project_id", envvar="AQ_PROJECT_ID", required=True)
@click.option("--step", "step_id", required=True)
@click.option(
    "--from", "source_sha", help="Exact source commit; defaults to the source branch tip."
)
@click.option("--version", help="Version at the pinned source commit.")
@click.option("--notes-reviewed", is_flag=True)
@click.option("--from-task", help="Completed hotfix task whose exact source to promote.")
@click.pass_context
@_handle_errors
def promote_request(ctx, project_id, step_id, source_sha, version, notes_reviewed, from_task):
    """Open a pinned step PR and an idempotent promotion intent."""
    _execute_intent(
        ctx,
        "promote_request",
        {
            "project_id": project_id,
            "step_id": step_id,
            "source_sha": source_sha,
            "version": version,
            "notes_reviewed": notes_reviewed,
            **({"from_task": from_task} if from_task else {}),
        },
    )


@promote.command("hotfix")
@click.option("--project", "project_id", envvar="AQ_PROJECT_ID", required=True)
@click.option("--step", "step_id", required=True)
@click.option("--title", required=True)
@click.option("--description")
@click.option("--from-task", help="Bug task that prompted this hotfix.")
@click.option("--version", help="Patch version the hotfix must prepare.")
@click.pass_context
@_handle_errors
def promote_hotfix(ctx, project_id, step_id, title, description, from_task, version):
    """File a fix on the step target; request its promotion after completion."""
    _execute_intent(ctx, "promote_hotfix", {
        "project_id": project_id, "step_id": step_id, "title": title,
        "description": description, "from_task": from_task, "version": version,
    })


@promote.command("approve")
@click.argument("request_id")
@click.option("--project", "project_id", envvar="AQ_PROJECT_ID", required=True)
@click.pass_context
@_handle_errors
def promote_approve(ctx, request_id, project_id):
    """Post a GitHub approval as the operator's authenticated gh user."""
    _execute_intent(ctx, "promote_approve", {"project_id": project_id, "request_id": request_id})


@promote.command("cancel")
@click.argument("request_id")
@click.option("--project", "project_id", envvar="AQ_PROJECT_ID", required=True)
@click.pass_context
@_handle_errors
def promote_cancel(ctx, request_id, project_id):
    """Close an unpublished step PR and abort its intent."""
    _execute_intent(ctx, "promote_cancel", {"project_id": project_id, "request_id": request_id})


@promote.command("status")
@click.option("--project", "project_id", envvar="AQ_PROJECT_ID", required=True)
@click.option("--step", "step_id")
@click.pass_context
@_handle_errors
def promote_status(ctx, project_id, step_id):
    """Read step intents and cached check/review evidence."""
    _execute_intent(ctx, "promote_status", {"project_id": project_id, "step_id": step_id})


@promote.command("list")
@click.option("--project", "project_id", envvar="AQ_PROJECT_ID", required=True)
@click.option("--step", "step_id")
@click.option("--limit", type=click.IntRange(1, 100), default=20)
@click.pass_context
@_handle_errors
def promote_list(ctx, project_id, step_id, limit):
    """List recorded promotions using the local evidence cache."""
    _execute_intent(
        ctx,
        "promote_list",
        {
            "project_id": project_id,
            "step_id": step_id,
            "limit": limit,
        },
    )
