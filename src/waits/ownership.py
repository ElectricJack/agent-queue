"""Current-process and confirmed-hibernation fences shared by wait mutations."""

from sqlalchemy import select

from src.database.queries.wait_queries import verify_owner
from src.database.tables import continuations, sessions
from src.waits.conditions import Scope, WaitError


async def fence_owner(conn, wait, task, *, parked_required=False):
    """Prove the live claim or the exact old process stopped by hibernation."""
    delivery = (wait["decision_inputs"] or {}).get("delivery", {})
    scope = Scope(wait["project_id"], wait["owner_kind"], wait["owner_id"],
                  wait["session_id"], delivery.get("instance_token"), wait["claim_epoch"],
                  principal="agent" if wait["session_id"] else "service",
                  elevated=wait["owner_kind"] == "session")
    continuation = None
    if task and task["status"] == "HIBERNATED" and wait["sleep_decision"] == "hibernate":
        stopped = await conn.scalar(select(sessions.c.id).where(
            sessions.c.id == wait["session_id"],
            sessions.c.instance_token == delivery.get("instance_token"),
            sessions.c.state == "stopped", sessions.c.desired_state == "stopped",
            sessions.c.end_reason == "hibernated",
        ))
        continuation = (await conn.execute(select(continuations).where(
            continuations.c.wait_id == wait["id"], continuations.c.task_id == task["id"],
            continuations.c.resume_state == "sleeping", continuations.c.stopped_at.is_not(None),
            continuations.c.hold_reason.is_(None),
        ).with_for_update())).mappings().first()
        if (not stopped or task["assigned_agent_id"] is not None
                or task["claim_epoch"] <= wait["claim_epoch"] or not continuation
                or task["project_id"] != wait["project_id"]):
            raise WaitError("wait.out_of_scope", "stopped continuation ownership changed")
    else:
        scope = await verify_owner(conn, scope)
        if parked_required and wait["sleep_decision"] == "hibernate":
            continuation = (await conn.execute(select(continuations).where(
                continuations.c.wait_id == wait["id"],
                continuations.c.task_id == wait["owner_id"],
                continuations.c.resume_state == "sleeping",
                continuations.c.stopped_at.is_(None),
            ).with_for_update())).mappings().first()
            if continuation is None:
                raise WaitError("wait.unavailable", "hibernation intent is not persisted")
    return scope, continuation
