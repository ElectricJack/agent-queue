"""Live in-place waits are occupied seats with an eviction candidate marker."""

from sqlalchemy import select
from src.database.tables import sessions, tasks, wait_records


async def evictable_sessions(db, config):
    if getattr(getattr(config, "wait_policy", None), "v2_enabled", False) is not True:
        return frozenset()
    async with db._engine.connect() as conn:
        return frozenset((await conn.execute(select(sessions.c.id).join(
            tasks, tasks.c.id == sessions.c.task_id,
        ).join(wait_records, wait_records.c.session_id == sessions.c.id).where(
            sessions.c.state == "running", sessions.c.desired_state == "running",
            sessions.c.claim_phase == "active", tasks.c.status == "WAITING",
            wait_records.c.owner_kind == "task", wait_records.c.owner_id == tasks.c.id,
            wait_records.c.claim_epoch == tasks.c.claim_epoch,
            sessions.c.last_claim_epoch == tasks.c.claim_epoch,
            wait_records.c.outcome == "pending", wait_records.c.verification_state == "verified",
            wait_records.c.sleep_decision == "in_place",
        ))).scalars())


async def continuation_occupancy(db, config):
    """Count a reserved launch before it has a session, and fence its new session.

    Resume owns the reservation scope and new_session_id. Never count the stopped
    origin session. Once a live new session exists its ordinary pool row owns the
    seat, so the synthetic reservation seat disappears in the same observation.
    """
    from src.database.tables import admission_reservations, continuations

    if getattr(getattr(config, "wait_policy", None), "v2_enabled", False) is not True:
        return frozenset(), {}
    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(
            tasks.c.project_id, admission_reservations.c.scope,
            continuations.c.new_session_id, sessions.c.state,
        ).select_from(continuations.join(
            tasks, tasks.c.id == continuations.c.task_id,
        ).join(admission_reservations,
               admission_reservations.c.continuation_id == continuations.c.id).outerjoin(
            sessions, sessions.c.id == continuations.c.new_session_id,
        )).where(
            continuations.c.resume_state.in_(("reserved", "launching", "delivered")),
            admission_reservations.c.state.in_(("pending", "overdue", "granted", "consumed")),
        ))).mappings().all()
    resuming, pending = set(), {}
    for row in rows:
        if row["new_session_id"] and row["state"] in {"starting", "running", "draining"}:
            resuming.add(row["new_session_id"])
        else:
            key = (row["project_id"], row["scope"]["profile_id"])
            pending[key] = pending.get(key, 0) + 1
    return frozenset(resuming), pending
