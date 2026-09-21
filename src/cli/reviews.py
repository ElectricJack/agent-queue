"""Hand-written document-review submission CLI (the file stays local)."""

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


@cli.group("review")
def review() -> None:
    """Document reviews: submit specs and plans for Jack's approval."""


@review.command("submit")
@click.option("--file", "file_path", type=click.Path(path_type=Path), required=True)
@click.option("--task-id", default=None)
@click.option("--project-id", default=None)
@click.option("--review-id", default=None)
@click.option("--kind", type=click.Choice(["spec", "plan", "other"]), default=None)
@click.option("--title", default=None)
@click.option("--changes", default=None)
@click.option("--resolves", multiple=True)
@click.pass_context
@_handle_errors
def review_submit(
    ctx: click.Context,
    file_path: Path,
    task_id: str | None,
    project_id: str | None,
    review_id: str | None,
    kind: str | None,
    title: str | None,
    changes: str | None,
    resolves: tuple[str, ...],
) -> None:
    """Submit FILE as a new review or a new revision of --review-id."""
    try:
        content = file_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise click.UsageError(f"not_utf8: {file_path} is not valid UTF-8 ({exc.reason})") from None
    except OSError as exc:
        raise click.UsageError(str(exc)) from None
    params = {
        "content": content,
        "task_id": task_id,
        "project_id": project_id,
        "review_id": review_id,
        "kind": kind,
        "title": title,
        "changes": changes,
        "resolves": list(resolves),
    }
    emit(ctx, _execute(ctx, "review_submit", {key: value for key, value in params.items() if value is not None}))
