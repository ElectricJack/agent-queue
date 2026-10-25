"""Wait occupancy and queued continuation demand for fleet capacity observations."""

from sqlalchemy import func, select
from src.database.tables import sessions, tasks, wait_records


async def queued_continuation_demand(db, *, pool_profiles, ready_projects):
    """Unfunded fresh resumes, grouped by their current project and pool route.

    Reserved/launching intents belong to ``continuation_occupancy`` instead.
    Keep this separate from the READY frontier: demand never permits an ordinary
    pool claim to take ownership of a suspended task.
    """
    from src.database.queries.claim_queries import _pool_profiles_predicate, route_claimable_in
    from src.database.tables import continuations

    async with db._engine.connect() as conn:
        rows = (await conn.execute(select(
            tasks.c.project_id, tasks.c.profile_id, func.count(),
        ).select_from(tasks.join(continuations, continuations.c.task_id == tasks.c.id)).where(
            tasks.c.status == "RESUME_PENDING",
            continuations.c.resume_state == "queued", continuations.c.hold_reason.is_(None),
            route_claimable_in(ready_projects), _pool_profiles_predicate(pool_profiles),
        ).group_by(tasks.c.project_id, tasks.c.profile_id))).all()
    return {(project_id, profile_id): count for project_id, profile_id, count in rows}


async def evictable_sessions(db, config):
    from src.waits.holding import execution_waits

    async with db._engine.connect() as conn:
        return frozenset((await conn.execute(execution_waits().with_only_columns(
            sessions.c.id, maintain_column_froms=True,
        ).where(
            tasks.c.status == "WAITING", wait_records.c.owner_kind == "task",
            wait_records.c.outcome == "pending",
            wait_records.c.sleep_decision == "in_place",
        ))).scalars())


async def continuation_occupancy(db, config, *, conn=None):
    """Count a reserved launch before it has a session, and fence its new session.

    Resume owns the reservation scope and new_session_id. Never count the stopped
    origin session. Once a live new session exists its ordinary pool row owns the
    seat, so the synthetic reservation seat disappears in the same observation.
    """
    from src.database.tables import admission_reservations, continuations

    if conn is None:
        async with db._engine.connect() as owned:
            return await continuation_occupancy(db, config, conn=owned)
    rows = (await conn.execute(select(
            tasks.c.project_id, admission_reservations.c.scope,
            continuations.c.new_session_id, sessions.c.state,
        ).select_from(continuations.join(
            tasks, tasks.c.id == continuations.c.task_id,
        ).join(admission_reservations,
               admission_reservations.c.continuation_id == continuations.c.id).outerjoin(
            sessions, sessions.c.id == continuations.c.new_session_id,
        )).where(
            continuations.c.resume_state.in_(("reserved", "launching", "delivered", "acknowledged")),
            admission_reservations.c.state.in_(("pending", "overdue", "granted", "consumed")),
    ))).mappings().all()
    resuming, pending = set(), {}
    for row in rows:
        if row["new_session_id"] and row["state"] in {"starting", "running", "draining"}:
            resuming.add(row["new_session_id"])
        elif row["state"] not in {"stopped", "failed"}:
            key = (row["project_id"], row["scope"]["profile_id"])
            pending[key] = pending.get(key, 0) + 1
    return frozenset(resuming), pending
