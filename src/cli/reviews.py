"""Hand-written document-review submission CLI (the file stays local)."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import click

from .app import _get_client, _handle_errors, _run, cli
from .envelope import emit


def _execute(ctx: click.Context, command: str, params: dict) -> dict:
    async def run():
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.execute(command, params)

    return _run(run())


def _read_text(file_path: Path) -> str:
    try:
        return file_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise click.UsageError(f"not_utf8: {file_path} is not valid UTF-8 ({exc.reason})") from None
    except OSError as exc:
        raise click.UsageError(str(exc)) from None


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
@click.option(
    "--playbook-id",
    default=None,
    help="Make this a playbook review: the playbook whose vault source the daemon compiles.",
)
@click.option(
    "--playbook-body",
    "playbook_body",
    type=click.Path(path_type=Path),
    default=None,
    help="Local JSON file with the proposal's rules and steps (a revision may omit it).",
)
@click.option(
    "--activate-on-approval/--no-activate-on-approval",
    default=None,
    help="Activate the pinned artifact when approved (default: store it only).",
)
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
    playbook_id: str | None,
    playbook_body: Path | None,
    activate_on_approval: bool | None,
) -> None:
    """Submit FILE as a new review or a new revision of --review-id.

    With --playbook-id the review is a playbook review (kind other): the daemon
    compiles the playbook's vault source with the --playbook-body rules and
    steps, refuses the submission unless the artifact is activatable, and pins
    its hash so approval stores exactly that artifact.
    """
    content = _read_text(file_path)
    params = {
        "content": content,
        "task_id": task_id,
        "project_id": project_id,
        "review_id": review_id,
        "kind": kind,
        "title": title,
        "changes": changes,
        "resolves": list(resolves),
        "playbook_id": playbook_id,
        "semantic_body": _read_text(playbook_body) if playbook_body is not None else None,
        "activate_on_approval": activate_on_approval,
    }
    emit(ctx, _execute(ctx, "review_submit", {key: value for key, value in params.items() if value is not None}))


@review.command("attach")
@click.option("--review-id", required=True)
@click.option("--revision", type=click.IntRange(min=1), required=True)
@click.option("--file", "file_path", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--content-type", default=None, help="Image MIME type; inferred from the extension when omitted.")
@click.option("--caption", required=True)
@click.option("--view-id", required=True)
@click.option("--candidate-id", required=True)
@click.pass_context
@_handle_errors
def review_attach(
    ctx: click.Context, review_id: str, revision: int, file_path: Path,
    content_type: str | None, caption: str, view_id: str, candidate_id: str,
) -> None:
    """Append a verified screenshot to an open review revision."""
    from src.api.task_attachments import MAX_ATTACHMENT_BYTES

    if file_path.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise click.UsageError("image exceeds the 10 MiB cap")
    media_type = content_type or mimetypes.guess_type(file_path.name)[0]
    emit(ctx, _execute(ctx, "review_attachment_add", {
        "review_id": review_id, "revision": revision,
        "data_base64": base64.b64encode(file_path.read_bytes()).decode("ascii"),
        "content_type": media_type, "caption": caption,
        "view_id": view_id, "candidate_id": candidate_id,
    }))


@review.command("attachments")
@click.option("--review-id", required=True)
@click.option("--revision", type=click.IntRange(min=1), required=True)
@click.pass_context
@_handle_errors
def review_attachments(ctx: click.Context, review_id: str, revision: int) -> None:
    """List screenshots belonging to one review revision."""
    emit(ctx, _execute(ctx, "review_attachment_list", {
        "review_id": review_id, "revision": revision,
    }))


@review.command("download-attachment")
@click.option("--review-id", required=True)
@click.option("--revision", type=click.IntRange(min=1), required=True)
@click.option("--attachment-id", required=True)
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.pass_context
@_handle_errors
def review_download_attachment(
    ctx: click.Context, review_id: str, revision: int, attachment_id: str, output: Path,
) -> None:
    """Download a revision-pinned image after the server's access check."""
    async def run():
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.download_review_attachment(review_id, revision, attachment_id)

    data = _run(run())
    output.write_bytes(data)
    emit(ctx, {"success": True, "output": str(output), "size": len(data)})


@review.command("dispatch")
@click.option("--review-id", required=True)
@click.option("--count", type=click.IntRange(min=1, max=10), default=1, show_default=True,
              help="Number of adversarial reviewer tasks.")
@click.option("--class", "intelligence_class", default="deep-high", show_default=True,
              help="Intelligence-class hint for the reviewers.")
@click.option("--revision", type=click.IntRange(min=1), default=None)
@click.option("--with-comments/--no-comments", default=True)
@click.option("--focus", default=None)
@click.option("--force", is_flag=True, default=False)
@click.pass_context
@_handle_errors
def review_dispatch(
    ctx: click.Context,
    review_id: str,
    count: int,
    intelligence_class: str,
    revision: int | None,
    with_comments: bool,
    focus: str | None,
    force: bool,
) -> None:
    """Send a pinned review revision to COUNT adversarial reviewers.

    Each reviewer task carries the --class hint and excludes the provider the
    author revision ran on; the project's router picks its profile, so a
    dispatch lands on another model family. No reviewer profile is named.
    """
    params = {
        "review_id": review_id,
        "count": count,
        "intelligence_class": intelligence_class,
        "with_comments": with_comments,
        "force": force,
    }
    if revision is not None:
        params["revision"] = revision
    if focus is not None:
        params["focus"] = focus
    emit(ctx, _execute(ctx, "review_dispatch", params))
