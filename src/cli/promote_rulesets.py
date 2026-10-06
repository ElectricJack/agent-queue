"""Read-only ruleset and workflow configuration for a promotion chain."""

from __future__ import annotations

import json
from pathlib import Path

import click

from .app import _get_client, _handle_errors, _run
from .envelope import emit
from .promote import promote


@promote.command("rulesets")
@click.option("--project", "project_id", required=True, help="Project whose flow to use.")
@click.option(
    "--file",
    "flow_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="YAML or JSON flow; omit to use the stored flow.",
)
@click.pass_context
@_handle_errors
def promote_rulesets(ctx: click.Context, project_id: str, flow_file: Path | None) -> None:
    """Print branch/tag rulesets and workflow triggers for the repository admin."""
    args = {"project_id": project_id, "use_stored": flow_file is None}
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
            return await client.execute("promote_rulesets", args)

    data = _run(run())
    emit(ctx, data, render=lambda value: click.echo(json.dumps(value, indent=2)))
    if not data.get("success"):
        raise SystemExit(1)
