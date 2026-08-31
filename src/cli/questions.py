"""List, answer, and escalate worker questions through the scoped daemon API."""
from __future__ import annotations

import click

from .app import _get_client, _handle_errors, _run, cli
from .envelope import emit


def _execute(ctx: click.Context, command: str, params: dict) -> dict:
    async def run():
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.execute(command, params)
    return _run(run())


def _render_questions(data: dict) -> None:
    rows = data.get("questions") or []
    if not rows:
        click.echo("No pending agent questions.")
    for row in rows:
        click.echo(
            f"{row['id']} · {row['state']} · agent {row.get('agent_id') or '—'}"
            f" · task {row.get('task_id') or '—'}"
        )
        # Worker text is literal, not Rich markup or a terminal instruction.
        click.echo(f"  {row.get('question') or ''}")


@cli.group("question")
def question() -> None:
    """Pending worker questions; replies target the original live session."""


@question.command("list")
@click.option("-p", "--project", "project_id", default=None, help="Filter by project ID")
@click.pass_context
@_handle_errors
def question_list(ctx: click.Context, project_id: str | None) -> None:
    """List questions waiting for a supervisor, human, or safe answer delivery."""
    params = {"project_id": project_id} if project_id else {}
    emit(ctx, _execute(ctx, "question_list", params), render=_render_questions)


@question.command("answer")
@click.argument("question_id")
@click.option("-b", "--body", required=True, help="Answer text for the waiting agent")
@click.pass_context
@_handle_errors
def question_answer(ctx: click.Context, question_id: str, body: str) -> None:
    """Answer without restarting or reassigning the worker."""
    emit(ctx, _execute(ctx, "question_answer", {"question_id": question_id, "body": body}))


@question.command("escalate")
@click.argument("question_id")
@click.option("--reason", required=True, help="Why a human decision is needed")
@click.pass_context
@_handle_errors
def question_escalate(ctx: click.Context, question_id: str, reason: str) -> None:
    """Ask a human to decide a question that the supervisor cannot answer."""
    emit(ctx, _execute(ctx, "question_escalate", {"question_id": question_id, "reason": reason}))
