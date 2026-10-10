"""Interactive policy export/import; selection UI also used by project create."""

from __future__ import annotations

from pathlib import Path

import click

from src.cli.app import _get_client, _handle_errors, _run, cli, console
from src.cli.envelope import emit, reject_json_mode


def pairs(entries: tuple[str, ...], *, scopes: bool = False) -> dict[str, str]:
    result = {}
    for entry in entries:
        key, separator, value = entry.partition("=")
        if not separator or not key or not value:
            raise click.UsageError("Use KEY=VALUE")
        if scopes and value not in {"project", "global", "skip"}:
            raise click.UsageError("Scope must be project, global or skip")
        if key in result:
            raise click.UsageError(f"Duplicate key: {key}")
        result[key] = value
    return result


def execute(ctx: click.Context, command: str, args: dict) -> dict:
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def call():
        async with _get_client(api_url) as client:
            return await client.execute(command, args)

    result = _run(call())
    if result.get("success") is False or result.get("error"):
        raise click.ClickException(str(result.get("error", "Policy command failed")))
    return result


def render_diff(data: dict) -> None:
    for policy_type in sorted({row["type"] for row in data["items"]}):
        console.print(f"[bold]{policy_type}[/]")
        for row in data["items"]:
            if row["type"] != policy_type:
                continue
            console.print(
                f"  {row['id']} — {row['status']} → {row['scope']} ({row['state']})", markup=False
            )
            if row["diff"]:
                console.print(row["diff"], markup=False)


def select_and_apply(
    ctx: click.Context,
    path: str,
    project_id: str,
    *,
    only=(),
    skip=(),
    scope=(),
    values=(),
    no_overwrite=False,
    yes=False,
) -> dict:
    reject_json_mode(ctx, "policy apply", "the selection workflow displays multiple previews")
    args = {
        "project_id": project_id,
        "path": str(Path(path).resolve()),
        "values": pairs(values),
        "selections": {key: {"scope": value} for key, value in pairs(scope, scopes=True).items()},
        "only": list(only),
        "skip": list(skip),
        "no_overwrite": no_overwrite,
    }
    preview = execute(ctx, "policy_diff", args)
    for placeholder in preview["placeholders"]:
        key = placeholder["name"]
        if key not in args["values"]:
            default = preview["values"].get(key, "")
            if yes and not default:
                raise click.UsageError(f"Provide --set {key}=VALUE")
            args["values"][key] = (
                default
                if yes
                else click.prompt(placeholder["description"], default=default or None, type=str)
            )
    preview = execute(ctx, "policy_diff", args)
    render_diff(preview)
    for row in preview["items"]:
        identifier = row["id"]
        if identifier in skip or (only and identifier not in only):
            args["selections"][identifier] = {"scope": "skip"}
            continue
        selection = args["selections"].get(identifier, {})
        if "scope" not in selection:
            selection["scope"] = (
                ("skip" if row["requires_scope_choice"] else "project")
                if yes
                else click.prompt(
                    f"{identifier}: placement",
                    type=click.Choice(["project", "global", "skip"]),
                    default="project",
                )
            )
        if selection["scope"] == "skip":
            args["selections"][identifier] = selection
            continue
        # Changing scope changes the destination and its overwrite status.
        args["selections"][identifier] = selection
        scoped = execute(ctx, "policy_diff", args)
        current = next(entry for entry in scoped["items"] if entry["id"] == identifier)
        overwrite = False
        if current["status"] == "will overwrite" and not no_overwrite and not yes:
            console.print(current["diff"], markup=False)
            overwrite = click.confirm(f"Overwrite {identifier}?", default=False)
        selection.update(overwrite=overwrite, expected_checksum=current["current_checksum"])
    final_preview = execute(ctx, "policy_diff", args)
    render_diff(final_preview)
    if not yes:
        click.confirm("Import the selected policy items?", default=False, abort=True)
    result = execute(ctx, "policy_apply", args)
    emit(
        ctx,
        result,
        render=lambda data: console.print(
            f"Imported {len(data['applied'])} item(s); {len(data['reviews'])} pending review, "
            f"{len(data['pending_configuration'])} pending configuration.",
            markup=False,
        ),
    )
    return result


@cli.group()
def policy() -> None:
    """Export and selectively import a portable policy profile."""


@policy.command("export")
@click.option("--project", "project_id", required=True)
@click.option("--out", "path", required=True, type=click.Path())
@click.option("--name")
@click.option("--yes", is_flag=True, help="Accept the displayed export preview.")
@click.pass_context
@_handle_errors
def policy_export(ctx, project_id, path, name, yes):
    reject_json_mode(ctx, "policy export", "exact file preview precedes the acknowledged write")
    args = {"project_id": project_id, "path": str(Path(path).resolve())}
    if name:
        args["name"] = name
    preview = execute(ctx, "policy_export", args)
    for entry in preview["files"]:
        console.print(f"{entry['path']}\n{entry['content']}", markup=False)
    if not yes:
        click.confirm("Write these files?", default=False, abort=True)
    args["expected_checksum"] = preview["checksum"]
    result = execute(ctx, "policy_export", args)
    emit(
        ctx,
        result,
        render=lambda data: console.print(
            f"Wrote {len(data['written'])} files to {path}", markup=False
        ),
    )


def selection_options(function):
    for decorator in (
        click.argument("path", type=click.Path(exists=True)),
        click.option("--project", "project_id", required=True),
        click.option("--only", multiple=True, help="Item id to include; repeat to select more."),
        click.option("--skip", multiple=True, help="Item id to omit; repeat to omit more."),
        click.option("--scope", multiple=True, help="ITEM=project|global|skip"),
        click.option("--set", "values", multiple=True, help="PLACEHOLDER=VALUE"),
        click.option("--no-overwrite", is_flag=True),
    ):
        function = decorator(function)
    return function


@policy.command("diff")
@selection_options
@click.pass_context
@_handle_errors
def policy_diff(ctx, path, project_id, only, skip, scope, values, no_overwrite):
    args = {
        "project_id": project_id,
        "path": str(Path(path).resolve()),
        "values": pairs(values),
        "only": list(only),
        "skip": list(skip),
        "no_overwrite": no_overwrite,
        "selections": {key: {"scope": value} for key, value in pairs(scope, scopes=True).items()},
    }
    result = execute(ctx, "policy_diff", args)
    emit(ctx, result, render=render_diff)


@policy.command("apply")
@selection_options
@click.option("--yes", is_flag=True, help="Accept defaults; existing items are never overwritten.")
@click.pass_context
@_handle_errors
def policy_apply(ctx, path, project_id, only, skip, scope, values, no_overwrite, yes):
    select_and_apply(
        ctx,
        path,
        project_id,
        only=only,
        skip=skip,
        scope=scope,
        values=values,
        no_overwrite=no_overwrite,
        yes=yes,
    )
