"""`aq collaboration` — bounded ordered threads between 2 to 4 held tasks."""

from __future__ import annotations

import time

import click

from .app import _get_client, _handle_errors, _run, cli
from .claim_epoch import claim_epoch_option, resolve_claim_epoch
from .envelope import emit


def _execute(ctx, command, params):
    async def run():
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.execute(command, params)

    return _run(run())


def _params(**values):
    return {key: value for key, value in values.items() if value is not None}


def _render_thread(thread: dict) -> None:
    state = thread.get("state", "?")
    if state == "active":
        remaining = int(thread.get("remaining_seconds") or 0)
        deadline = f"{remaining // 60}m{remaining % 60:02d}s left"
    else:
        deadline = f"{state}: {thread.get('close_reason') or 'deadline passed'}"
    click.echo(
        f"{thread.get('id')}  [{state}]  {deadline}  "
        f"budget {thread.get('message_count', 0)}/{thread.get('message_budget', 0)}  "
        f"last seq {thread.get('last_seq', 0)}"
    )
    if thread.get("goal"):
        click.echo(f"  goal: {thread['goal']}")
    for member in thread.get("members") or []:
        joined = "needs accept" if member.get("needs_accept") else member.get("state")
        running = "running" if member.get("running") else "not running"
        click.echo(f"  - {member.get('task_id')}  {member.get('task_status')}  {running}  {joined}")
    if state != "active" and thread.get("final_result"):
        click.echo(f"  final: {thread['final_result']}")


def _render_result(data: dict) -> None:
    if data.get("thread"):
        _render_thread(data["thread"])
    for thread in data.get("threads") or []:
        _render_thread(thread)
    if "threads" in data and not data["threads"]:
        click.echo("No collaboration threads.")
    if data.get("thread_ids") is not None:
        click.echo(f"Closed {data.get('closed_count', 0)} active thread(s).")
    for message in data.get("messages") or []:
        stamp = time.strftime("%H:%M:%S", time.localtime(message.get("created_at") or 0))
        body = message.get("body")
        click.echo(f"\n#{message.get('seq')}  {message.get('sender_task_id')}  {stamp}")
        click.echo(body if body is not None else "(content expired)")
    if data.get("capacity_hold"):
        click.echo("\ncapacity hold: no partner is running")
    if data.get("next_step"):
        click.echo(f"\nNext: {data['next_step']}")


@cli.group("collaboration")
def collaboration():
    """Bounded ordered threads between 2 to 4 held tasks, each on its own branch."""


@collaboration.command("create")
@click.option("--project", "project_id", required=True, help="Project owning every member task.")
@click.option(
    "--task", "task_ids", multiple=True, required=True, help="Member task id; repeat 2 to 4 times."
)
@click.option("--goal", default=None, help="What the members should settle (<= 1000 chars).")
@click.option(
    "--deadline-minutes",
    type=click.IntRange(1, 120),
    default=None,
    help="Deadline from now in minutes (default and maximum 120).",
)
@click.option("--budget", type=click.IntRange(1, 40), default=None, help="Message budget (<= 40).")
@click.option("--idempotency-key", required=True, help="Replays return the same thread.")
@click.pass_context
@_handle_errors
def collaboration_create(ctx, project_id, task_ids, goal, deadline_minutes, budget, idempotency_key):
    """Create a thread and invite each member once (operator or supervisor)."""
    params = _params(
        project_id=project_id,
        task_ids=list(task_ids),
        goal=goal,
        deadline_seconds=deadline_minutes * 60 if deadline_minutes is not None else None,
        message_budget=budget,
        idempotency_key=idempotency_key,
    )
    emit(ctx, _execute(ctx, "collaboration_create", params), render=_render_result)


@collaboration.command("accept")
@click.argument("thread_id")
@claim_epoch_option
@click.pass_context
@_handle_errors
def collaboration_accept(ctx, thread_id, claim_epoch):
    """Join THREAD_ID for the task this session holds, under its live claim."""
    params = _params(thread_id=thread_id, claim_epoch=resolve_claim_epoch(claim_epoch))
    emit(ctx, _execute(ctx, "collaboration_accept", params), render=_render_result)


@collaboration.command("show")
@click.argument("thread_id")
@click.option(
    "--after", "after_seq", type=click.IntRange(min=0), default=None, help="Messages after SEQ."
)
@click.pass_context
@_handle_errors
def collaboration_show(ctx, thread_id, after_seq):
    """Read members, deadline, capacity hold and up to 20 ordered messages."""
    params = _params(thread_id=thread_id, after_seq=after_seq)
    emit(ctx, _execute(ctx, "collaboration_get", params), render=_render_result)


@collaboration.command("list")
@click.option("--project", "project_id", default=None, help="Project (operator reads).")
@click.option("--task-id", default=None, help="Threads this task belongs to.")
@click.option("--state", type=click.Choice(["active", "closed", "expired"]), default=None)
@click.option("--limit", type=click.IntRange(1, 50), default=20)
@click.pass_context
@_handle_errors
def collaboration_list(ctx, project_id, task_id, state, limit):
    """List threads for the held task, or a project's threads."""
    params = _params(project_id=project_id, task_id=task_id, state=state, limit=limit)
    emit(ctx, _execute(ctx, "collaboration_list", params), render=_render_result)


@collaboration.command("close")
@click.argument("thread_id", required=False)
@click.option("--note", default=None, help="Recorded in the thread's final result.")
@click.option("--remove-task", "remove_task_id", default=None, help="Remove one member (elevated).")
@click.option(
    "--all-active",
    is_flag=True,
    default=False,
    help="Close every active thread in the project (elevated rollback lever).",
)
@click.option("--project", "project_id", default=None, help="Project for --all-active.")
@claim_epoch_option
@click.pass_context
@_handle_errors
def collaboration_close(ctx, thread_id, note, remove_task_id, all_active, project_id, claim_epoch):
    """Close THREAD_ID; member tasks keep their status."""
    if bool(thread_id) == all_active:
        raise click.UsageError("Pass exactly one of THREAD_ID or --all-active.")
    params = _params(
        thread_id=thread_id,
        note=note,
        remove_task_id=remove_task_id,
        all_active=True if all_active else None,
        project_id=project_id,
        claim_epoch=resolve_claim_epoch(claim_epoch),
    )
    emit(ctx, _execute(ctx, "collaboration_close", params), render=_render_result)
