"""Narrow wait delegation for an author's current close candidate."""
from sqlalchemy import select

from src.database.tables import integration_close_attempts as attempts
from src.database.tables import sessions, tasks


async def author_candidate(conn, scope, *, operation_id=None, repository_id=None, head=None):
    if scope.owner_kind != "task" or not scope.session_id or scope.claim_epoch is None:
        return None
    query = select(attempts).join(tasks, tasks.c.id == attempts.c.task_id).join(
        sessions, sessions.c.id == attempts.c.session_id).where(
        attempts.c.project_id == scope.project_id,
        attempts.c.task_id == scope.owner_id,
        attempts.c.session_id == scope.session_id,
        attempts.c.claim_epoch == scope.claim_epoch,
        tasks.c.claim_epoch == scope.claim_epoch,
        tasks.c.status == "IN_PROGRESS",
        tasks.c.assigned_agent_id == sessions.c.agent_id,
        sessions.c.task_id == tasks.c.id,
        sessions.c.desired_state == "running",
        attempts.c.state.in_(["checking", "needs_author", "publish_uncertain"]),
    )
    if operation_id:
        query = query.where(attempts.c.id == operation_id)
    if repository_id:
        query = query.where(attempts.c.repository_id == repository_id)
    if head:
        query = query.where(attempts.c.candidate_oid == head)
    return (await conn.execute(query.limit(1))).mappings().first()
