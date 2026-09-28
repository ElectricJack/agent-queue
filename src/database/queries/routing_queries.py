"""Reads and the one write of the router (mandatory-routing spec §6.4-§6.6).

``count_routed_backlog_by_profile`` and ``count_busy_sessions_by_profile`` are
the load half of the planner's snapshot.  ``routing_apply_lock`` and
``write_router_route`` are ``task_route_apply``'s critical section: the lock
serialises every apply fleet-wide, so each apply's fresh snapshot sees the
routes the applies before it committed.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import func, select, update

from src.database.tables import sessions, tasks
from src.models import Task, TaskStatus
from src.routing.sources import LEGACY, ROUTER, UNROUTED

#: The statuses the routed backlog counts (§6.4 step 5): work routed to a
#: profile that is waiting for, or has been handed to, a worker.
ROUTED_BACKLOG_STATUSES: tuple[str, ...] = (
    TaskStatus.READY.value,
    TaskStatus.ASSIGNED.value,
)
#: The statuses a router may route (spec I5): queued, not started.
ROUTABLE_STATUSES: tuple[str, ...] = (
    TaskStatus.DEFINED.value,
    TaskStatus.READY.value,
    TaskStatus.BLOCKED.value,
)
_LIVE_SESSION_STATES = ("starting", "running", "draining")
#: ``pg_try_advisory_xact_lock(namespace, key)`` for the fleet-wide routing
#: key: one number per lock family in this codebase ("ROUT").
ROUTING_LOCK_NAMESPACE = 0x524F5554
ROUTING_LOCK_KEY = 1
#: How long an apply waits for the routing lock before giving up.
ROUTING_LOCK_BUDGET_SECONDS = 30.0


class RoutingBusyError(RuntimeError):
    """The routing lock stayed held for the whole wait budget."""


def _active_session():
    return (
        select(sessions.c.id)
        .where(sessions.c.task_id == tasks.c.id, sessions.c.state.in_(_LIVE_SESSION_STATES))
        .exists()
    )


class RoutingQueryMixin:
    async def count_routed_backlog_by_profile(self, *, conn=None) -> dict[str, int]:
        """Fleet-wide routed backlog: READY and ASSIGNED tasks per profile not yet started.

        Unlike ``count_ready_by_profile``, which counts one project's READY
        frontier, this spans every project: pools are global, so the load on
        a profile is too.  A task with a live session is load through
        ``count_busy_sessions_by_profile`` instead, never twice.
        """
        statement = (
            select(tasks.c.profile_id, func.count())
            .where(
                tasks.c.status.in_(ROUTED_BACKLOG_STATUSES),
                tasks.c.profile_id.is_not(None),
                ~_active_session(),
            )
            .group_by(tasks.c.profile_id)
        )
        if conn is not None:
            rows = (await conn.execute(statement)).fetchall()
        else:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(statement)).fetchall()
        return {str(profile_id): int(count) for profile_id, count in rows}

    async def count_busy_sessions_by_profile(self, *, conn=None) -> dict[str, int]:
        """Live sessions holding a task, per profile, fleet-wide.

        The same rule as the pool measurement's ``running_busy`` (a live
        session with ``task_id`` set), read in one grouped query so an apply
        can take it inside its lock.
        """
        statement = (
            select(sessions.c.profile_id, func.count())
            .where(
                sessions.c.state.in_(_LIVE_SESSION_STATES),
                sessions.c.task_id.is_not(None),
            )
            .group_by(sessions.c.profile_id)
        )
        if conn is not None:
            rows = (await conn.execute(statement)).fetchall()
        else:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(statement)).fetchall()
        return {str(profile_id): int(count) for profile_id, count in rows}

    @asynccontextmanager
    async def routing_apply_lock(
        self, *, budget_seconds: float = ROUTING_LOCK_BUDGET_SECONDS
    ) -> AsyncIterator[Any]:
        """A transaction holding the fleet-wide routing lock; yields its connection.

        ``pg_try_advisory_xact_lock`` in a bounded retry loop that closes the
        connection between attempts, as ``_standing_parent_lock`` does:
        blocking in ``pg_advisory_xact_lock`` would hold a pooled connection
        per waiting apply, and a 20-node graph routes 20 tasks at once.  The
        lock is released when the transaction ends, after the route commits.

        Raises :class:`RoutingBusyError` when the budget is spent.
        """
        deadline = time.monotonic() + budget_seconds
        while True:
            async with self._engine.begin() as conn:
                held = bool(
                    (
                        await conn.execute(
                            select(
                                func.pg_try_advisory_xact_lock(
                                    ROUTING_LOCK_NAMESPACE, ROUTING_LOCK_KEY
                                )
                            )
                        )
                    ).scalar()
                )
                if held:
                    yield conn
                    return
            if time.monotonic() >= deadline:
                raise RoutingBusyError("the routing lock is busy")
            await asyncio.sleep(0.02 + random.random() * 0.08)

    async def get_task_on(self, conn, task_id: str) -> Task | None:
        """Read one task row on *conn* (inside the caller's transaction)."""
        row = (
            await conn.execute(select(tasks).where(tasks.c.id == task_id))
        ).mappings().fetchone()
        return self._row_to_task(row) if row is not None else None

    async def task_has_live_session(self, conn, task_id: str) -> bool:
        statement = select(
            select(sessions.c.id)
            .where(sessions.c.task_id == task_id, sessions.c.state.in_(_LIVE_SESSION_STATES))
            .exists()
        )
        return bool((await conn.execute(statement)).scalar())

    async def write_router_route(
        self,
        conn,
        task_id: str,
        *,
        profile_id: str,
        intelligence_class: str,
        provider_intent: str,
        route: Mapping[str, Any],
        task_type: str | None = None,
    ) -> bool:
        """Write a router route in one update, under the "no worker holds it" guard.

        The predicate repeats every re-check ``task_route_apply`` makes, so a
        claim or a route that lands between the read and this write turns the
        write into a no-op (``False``) rather than retargeting held work.
        """
        values: dict[str, Any] = {
            "profile_id": profile_id,
            "intelligence_class": intelligence_class,
            "provider_intent": provider_intent,
            "route_source": ROUTER,
            "route": dict(route),
            "updated_at": time.time(),
        }
        if task_type:
            values["task_type"] = task_type
        result = await conn.execute(
            update(tasks)
            .where(
                tasks.c.id == task_id,
                tasks.c.status.in_(ROUTABLE_STATUSES),
                tasks.c.assigned_agent_id.is_(None),
                tasks.c.route_source.in_((UNROUTED, LEGACY)),
                ~_active_session(),
            )
            .values(**values)
        )
        return result.rowcount == 1


__all__ = [
    "ROUTABLE_STATUSES",
    "ROUTED_BACKLOG_STATUSES",
    "ROUTING_LOCK_BUDGET_SECONDS",
    "RoutingBusyError",
    "RoutingQueryMixin",
]
