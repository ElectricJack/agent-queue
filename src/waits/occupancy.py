"""Wait occupancy and queued continuation demand for fleet capacity observations."""

from sqlalchemy import literal, select
from src.database.tables import sessions, tasks, wait_records


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
    resuming, pending, _ = await continuation_capacity(db, conn=conn)
    return resuming, pending


async def continuation_capacity(db, *, conn=None, pool_profiles=None, ready_projects=()):
    """Observe occupied seats and queued resume demand in one database snapshot.

    Pool sizing supplies compatible routes to include queued demand. Other seat
    observers retain occupancy-only semantics. A single statement prevents a
    queued-to-reserved transition from appearing in both demand and occupancy.
    """
    from src.database.queries.claim_queries import _pool_profiles_predicate, route_claimable_in
    from src.database.tables import admission_reservations, continuations

    if conn is None:
        async with db._engine.connect() as owned:
            return await continuation_capacity(
                db, conn=owned, pool_profiles=pool_profiles, ready_projects=ready_projects,
            )
    statement = select(
        tasks.c.project_id, admission_reservations.c.scope["profile_id"].as_string().label("profile_id"),
        continuations.c.new_session_id, sessions.c.state, literal(False).label("queued"),
    ).select_from(continuations.join(
        tasks, tasks.c.id == continuations.c.task_id,
    ).join(admission_reservations,
           admission_reservations.c.continuation_id == continuations.c.id).outerjoin(
        sessions, sessions.c.id == continuations.c.new_session_id,
    )).where(
        continuations.c.resume_state.in_(("reserved", "launching", "delivered", "acknowledged")),
        admission_reservations.c.state.in_(("pending", "overdue", "granted", "consumed")),
    )
    if pool_profiles:
        statement = statement.union_all(select(
            tasks.c.project_id, tasks.c.profile_id, literal(None), literal(None), literal(True),
        ).select_from(tasks.join(continuations, continuations.c.task_id == tasks.c.id)).where(
            tasks.c.status == "RESUME_PENDING",
            continuations.c.resume_state == "queued", continuations.c.hold_reason.is_(None),
            route_claimable_in(ready_projects), _pool_profiles_predicate(pool_profiles),
        ))
    rows = (await conn.execute(statement)).mappings().all()
    resuming, pending, demand = set(), {}, {}
    for row in rows:
        key = (row["project_id"], row["profile_id"])
        if row["queued"]:
            demand[key] = demand.get(key, 0) + 1
        elif row["new_session_id"] and row["state"] in {"starting", "running", "draining"}:
            resuming.add(row["new_session_id"])
        elif row["state"] not in {"stopped", "failed"}:
            pending[key] = pending.get(key, 0) + 1
    return frozenset(resuming), pending, demand
