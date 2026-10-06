"""Read-only promotion-flow schema and file validation."""

from __future__ import annotations

import json
from pathlib import Path

import click

from .app import _get_client, _handle_errors, _run, cli
from .envelope import emit


@cli.group()
def promote() -> None:
    """Validate composable branch promotion flows."""


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
@click.option("--remote", is_flag=True, help="Request remote checks (deferred to phase 2).")
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
