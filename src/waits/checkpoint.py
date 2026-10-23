"""Command callback for summary capture; lifecycle activation stays with its owner."""

from __future__ import annotations

from src.database.queries.continuation_queries import checkpoint_continuation
from src.handoffs import collect_facts
from src.waits.conditions import WaitError


async def collect_checkpoint_facts(db, task, session):
    facts = await collect_facts(db, task, session, session.work_dir)
    page = await db.list_task_comments(task.id, project_id=task.project_id, limit=1)
    facts["last_comment"] = next(iter(page["comments"]), None)
    facts["open_subtasks"] = [
        row
        for row in await db.list_task_subtasks(task.id)
        if row["status"] in {"pending", "in_progress"}
    ]
    return facts


async def wait_checkpoint(args, *, principal, scope, handler):
    """Authenticated scope is supplied by WaitV2CommandsMixin, repeated in SQL."""
    if scope is None or scope.owner_kind != "task" or scope.principal != "agent":
        raise WaitError("wait.out_of_scope", "checkpoint requires a task holder")
    task = await handler.db.get_task(scope.owner_id)
    session = await handler.db.get_session(scope.session_id)
    if task is None or session is None:
        raise WaitError("continuation.stale_generation", "checkpoint holder disappeared")
    facts = await collect_checkpoint_facts(handler.db, task, session)
    async with handler.db._engine.begin() as conn:
        return await checkpoint_continuation(
            conn,
            scope=scope,
            wait_id=args["wait_id"],
            summary=args["summary"],
            facts=facts,
            continuation_id=args.get("continuation_id"),
            generation=args.get("generation"),
        )
