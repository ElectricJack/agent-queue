"""Reads and the route writes of the router (mandatory-routing spec §6.4-§7, §10).

``count_routed_backlog_by_profile`` and ``count_busy_sessions_by_profile`` are
the load half of the planner's snapshot.  ``routing_apply_lock`` and
``write_router_route`` are ``task_route_apply``'s critical section: the lock
serialises every apply fleet-wide, so each apply's fresh snapshot sees the
routes the applies before it committed.  ``write_override_route`` is the
audited emergency override (§7), and ``latest_router_run_for_task`` is what
``aq task explain`` reads to say why the router has not routed a task (§10).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import exists, func, literal, or_, select, update

from src.database.tables import playbook_v2_runs, sessions, tasks
from src.models import Task, TaskStatus
from src.routing.sources import LEGACY, OVERRIDE, ROLE, ROUTER, UNROUTED

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
#: The statuses an override may route: queued work, and a paused task, which
#: runs on the override once it is resumed.
OVERRIDABLE_STATUSES: tuple[str, ...] = (*ROUTABLE_STATUSES, TaskStatus.PAUSED.value)
#: How many recent router runs whose snapshot mentions a task are decoded
#: before ``latest_router_run_for_task`` gives up.
_ROUTER_RUN_SCAN_LIMIT = 50
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

    async def list_queued_router_routes(self, project_id: str) -> list[dict[str, Any]]:
        """Queued, unassigned tasks in *project_id* whose route the router wrote.

        What ``aq pool provider apply`` returns to the router when a project
        prefers a provider (spec §6.8): only the routing columns are read.
        """
        statement = (
            select(
                tasks.c.id,
                tasks.c.project_id,
                tasks.c.profile_id,
                tasks.c.status,
                tasks.c.provider_intent,
                tasks.c.route_source,
            )
            .where(
                tasks.c.project_id == project_id,
                tasks.c.route_source == ROUTER,
                tasks.c.status.in_(ROUTABLE_STATUSES),
                tasks.c.assigned_agent_id.is_(None),
                ~_active_session(),
            )
            .order_by(tasks.c.priority, tasks.c.created_at, tasks.c.id)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().fetchall()
        return [dict(row) for row in rows]

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

    async def write_override_route(
        self,
        task_id: str,
        *,
        profile_id: str,
        intelligence_class: str,
        route: Mapping[str, Any],
    ) -> bool:
        """Write an audited emergency override (spec §7) under the claim guard.

        ``route_source='override'`` and ``provider_intent='pinned'``: failover
        holds the task rather than moving it, and *route*'s single candidate
        keeps spill and reroute on it too (§6.8).  Any route but a role's may
        be overridden; a task that is claimed, assigned, running, finished or
        in a live session is left alone and ``False`` is returned.
        """
        values: dict[str, Any] = {
            "profile_id": profile_id,
            "intelligence_class": intelligence_class,
            "provider_intent": "pinned",
            "route_source": OVERRIDE,
            "route": dict(route),
            "updated_at": time.time(),
        }
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(tasks)
                .where(
                    tasks.c.id == task_id,
                    tasks.c.status.in_(OVERRIDABLE_STATUSES),
                    tasks.c.assigned_agent_id.is_(None),
                    tasks.c.route_source != ROLE,
                    ~_active_session(),
                )
                .values(**values)
            )
        return result.rowcount == 1

    async def task_is_container(self, task_id: str) -> bool:
        """Whether the router never routes *task_id*: it is a container (§6.1).

        The same two rules as route-needed emission and the claim frontier:
        the ``container`` flag, or a child.  A container is never leased
        (work-graph §13a), so it needs no route.
        """
        from src.database.queries.hierarchy_queries import container_flag_exists

        child = tasks.alias("routing_child")
        statement = select(
            or_(
                container_flag_exists(),
                exists(select(literal(1)).where(child.c.parent_task_id == tasks.c.id)),
            )
        ).where(tasks.c.id == task_id)
        async with self._engine.connect() as connection:
            return bool((await connection.execute(statement)).scalar())

    async def latest_router_run_for_task(
        self, playbook_id: str, task_id: str, *, since: float
    ) -> dict[str, Any] | None:
        """The newest ``task.route_needed`` run of *playbook_id* for *task_id*, or ``None``.

        Runs carry no task column: the task is the triggering event's
        ``task_id`` inside the run snapshot.  A substring filter on the
        snapshot text narrows the scan to runs that mention the task, started
        at or after *since*, and the event is decoded to confirm it.  Returns
        the run's id, lifecycle, error, times and step ``bindings``.
        """
        escaped = task_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = (
            select(
                playbook_v2_runs.c.run_id,
                playbook_v2_runs.c.lifecycle,
                playbook_v2_runs.c.error,
                playbook_v2_runs.c.error_code,
                playbook_v2_runs.c.started_at,
                playbook_v2_runs.c.completed_at,
                playbook_v2_runs.c.snapshot,
            )
            .where(
                playbook_v2_runs.c.playbook_id == playbook_id,
                playbook_v2_runs.c.event_type == "task.route_needed",
                playbook_v2_runs.c.started_at >= since,
                playbook_v2_runs.c.snapshot.like(f"%{escaped}%", escape="\\"),
            )
            .order_by(playbook_v2_runs.c.started_at.desc())
            .limit(_ROUTER_RUN_SCAN_LIMIT)
        )
        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().fetchall()
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot"] or "{}")
            except (TypeError, ValueError):
                continue
            event = snapshot.get("event") if isinstance(snapshot, dict) else None
            if not isinstance(event, dict) or event.get("task_id") != task_id:
                continue
            bindings = snapshot.get("bindings")
            return {
                "run_id": row["run_id"],
                "lifecycle": row["lifecycle"],
                "error": row["error"] or snapshot.get("error"),
                "error_code": row["error_code"] or snapshot.get("error_code"),
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "bindings": bindings if isinstance(bindings, dict) else {},
            }
        return None


__all__ = [
    "OVERRIDABLE_STATUSES",
    "ROUTABLE_STATUSES",
    "ROUTED_BACKLOG_STATUSES",
    "ROUTING_LOCK_BUDGET_SECONDS",
    "RoutingBusyError",
    "RoutingQueryMixin",
]
