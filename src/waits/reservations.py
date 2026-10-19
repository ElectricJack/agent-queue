"""Reservation-first selection, with every ordinary claim predicate retained."""

from sqlalchemy import Text, cast, select, update

from src.database.tables import admission_reservations as reservations, projects, tasks
from src.waits.scheduler import enabled


def reserved_statement(candidate, project_id, profile_id):
    scope = reservations.c.scope
    repository = select(projects.c.integration_repository_id).where(
        projects.c.id == project_id,
    ).scalar_subquery()
    return candidate.with_only_columns(
        tasks.c.id.label("task_id"), reservations.c.id.label("reservation_id"),
    ).join(reservations, reservations.c.task_id == tasks.c.id).where(
        reservations.c.project_id == project_id,
        reservations.c.state.in_(("pending", "overdue")),
        scope["profile_id"].astext == profile_id,
        scope["route_source"].astext == tasks.c.route_source,
        scope["intelligence_class"].astext.is_not_distinct_from(tasks.c.intelligence_class),
        scope["repository_id"].astext.is_not_distinct_from(repository),
        scope["claim_epoch"].astext == cast(tasks.c.claim_epoch, Text),
    ).order_by(None).order_by(
        reservations.c.effective_priority, reservations.c.fifo_seq,
    )


async def claim_candidate(handler, conn, **kwargs):
    active = enabled(handler.config)
    if active and kwargs.get("task_id") is None:
        reserved = await handler.db.select_reserved_for_profile(conn, **kwargs)
        if reserved:
            return reserved["task_id"], reserved["reservation_id"]
    return await handler.db.select_ready_for_profile(
        conn, **kwargs, **({"effective_priority": True} if active else {}),
    ), None


async def consume_reservation(conn, reservation_id, session_id, *, now):
    if reservation_id is None:
        return
    # The ordinary READY transition cancelled outstanding reservations in the
    # same transaction. Only the exact reservation selected under its task lock
    # may become consumed; a rollback restores both the task and reservation.
    result = await conn.execute(update(reservations).where(
        reservations.c.id == reservation_id, reservations.c.state == "cancelled",
    ).values(state="consumed", consumed_by_session_id=session_id, consumed_at=now))
    if result.rowcount != 1:
        raise RuntimeError("selected reservation changed during claim")
