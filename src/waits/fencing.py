"""Command-boundary suspension and fresh-resume acknowledgement fences."""

from sqlalchemy import or_, select

from src.database.tables import continuations, sessions, wait_records


async def mutation_fence(conn, name, principal):
    row = (await conn.execute(select(
        continuations, wait_records.c.session_id.label("old_session_id"),
    ).join(wait_records, wait_records.c.id == continuations.c.wait_id).where(
        or_(wait_records.c.session_id == principal.session_id,
            continuations.c.new_session_id == principal.session_id),
        continuations.c.resume_state.not_in(("acknowledged", "failed", "cancelled")),
    ))).mappings().first()
    if row is None:
        return None
    instance = await conn.scalar(select(sessions.c.instance_token).where(
        sessions.c.id == principal.session_id,
    ))
    if instance != principal.session_instance_token:
        return {"success": False, "error_code": "continuation.stale_generation",
                "error": "session instance changed"}
    if row["new_session_id"] == principal.session_id:
        if name in {"wait_ack", "task_comment", "wait_get"}:
            return None
        code, message = "wait.ack_required", "acknowledge the delivered continuation outcome first"
    else:
        if name == "wait_checkpoint" and row["stopped_at"] is None:
            return None
        code, message = "wait.suspending", "checkpoint and end your turn; suspension is in progress"
    return {"success": False, "error_code": code, "error": message,
            "wait_id": row["wait_id"], "continuation_id": row["id"],
            "generation": row["suspension_generation"]}
