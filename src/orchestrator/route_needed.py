"""Emit ``task.route_needed`` for queued work the router still owes a route.

This is the whole of the orchestrator's involvement in assignment routing.
It decides nothing: it notices a task that is otherwise eligible to be
picked up and whose route no router wrote -- ``route_source='unrouted'``, or
``legacy`` in a project whose router is ready (mandatory routing §6.1, I5)
-- and tells the playbook layer.  The project's bound router
(``default-assignment-routing`` by default) plans and applies the route
through ``task_route_plan`` / ``task_route_apply``.  A container is never
routed: it is never leased (work-graph §13a).
"""

from __future__ import annotations

import logging
import time
from enum import Enum

from sqlalchemy import and_, exists, literal, or_, select

from src.database.queries.blocked_state import apply_label_filters
from src.database.queries.hierarchy_queries import container_flag_exists
from src.database.tables import (
    gates as gates_table,
    projects as projects_table,
    task_gates as task_gates_table,
    tasks as tasks_table,
)
from src.models import ProjectStatus, TaskStatus
from src.routing.sources import LEGACY, UNROUTED

logger = logging.getLogger(__name__)

#: Re-emit for the same task at most this often — a playbook run needs time
#: to read, decide and write before the cascade asks again.
ROUTE_NEEDED_INTERVAL_SECONDS = 120.0


_child = tasks_table.alias("route_needed_child")


def _value(value):
    return value.value if isinstance(value, Enum) else value


class RouteNeededMixin:
    _route_needed_emitted: dict[str, float]

    async def _route_needed_candidates(self, ready_projects: frozenset[str] = frozenset()):
        """Queued tasks nothing can pick up until the router routes them (I5).

        READY / BLOCKED tasks, plus DEFINED tasks that are unblocked or whose
        only blocker is their own open ``routing`` gate (a worker-filed root
        is born that way, swarm work model §12).  Unassigned, not a plan
        subtask, not a container (the ``container`` flag or a child, the
        claim frontier's two rules), in an active project, and either
        ``unrouted`` or ``legacy`` in a project in *ready_projects*.  Exclude
        inactive backlogs in SQL so they cannot spawn routing playbooks or
        delay other work on the event loop.
        """
        open_routing_gate = (
            select(literal(1))
            .select_from(
                task_gates_table.join(gates_table, gates_table.c.id == task_gates_table.c.gate_id)
            )
            .where(
                task_gates_table.c.task_id == tasks_table.c.id,
                gates_table.c.status == "open",
                gates_table.c.gate_type == "routing",
            )
            .exists()
        )
        needs_route = tasks_table.c.route_source == UNROUTED
        if ready_projects:
            needs_route = or_(
                needs_route,
                and_(
                    tasks_table.c.route_source == LEGACY,
                    tasks_table.c.project_id.in_(sorted(ready_projects)),
                ),
            )
        statement = select(tasks_table).join(
            projects_table, projects_table.c.id == tasks_table.c.project_id,
        ).where(
            projects_table.c.status == ProjectStatus.ACTIVE.value,
            or_(
                tasks_table.c.status.in_([TaskStatus.READY.value, TaskStatus.BLOCKED.value]),
                and_(
                    tasks_table.c.status == TaskStatus.DEFINED.value,
                    or_(tasks_table.c.is_blocked == 0, open_routing_gate),
                ),
            ),
            tasks_table.c.assigned_agent_id.is_(None),
            tasks_table.c.is_plan_subtask == 0,
            needs_route,
            ~container_flag_exists(),
            ~exists(select(literal(1)).where(_child.c.parent_task_id == tasks_table.c.id)),
        )
        statement = apply_label_filters(statement, exclude_hold=True)
        async with self.db._engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().fetchall()
        candidates = []
        for row in rows:
            task = self.db._row_to_task(row)
            if task.is_blocked:
                if await self.db.get_blocking_dependencies(task.id):
                    continue
                gates = [
                    gate for gate in await self.db.get_gates_for_task(task.id)
                    if gate["status"] != "resolved"
                ]
                if not gates or any(
                    gate["status"] != "open" or gate["gate_type"] != "routing" for gate in gates
                ):
                    continue
            candidates.append(task)
        return candidates

    async def _emit_route_needed_events(self) -> int:
        """Cascade step 3a.  Returns how many events were emitted."""
        emitted_at = self.__dict__.setdefault("_route_needed_emitted", {})
        now = time.time()
        from src.routing.readiness import orchestrator_ready_projects

        candidates = await self._route_needed_candidates(
            await orchestrator_ready_projects(self)
        )
        live = {task.id for task in candidates}
        for task_id in [t for t in emitted_at if t not in live]:
            emitted_at.pop(task_id, None)
        count = 0
        routers: dict[str, str | None] = {}
        for task in candidates:
            last = emitted_at.get(task.id, 0.0)
            if now - last < ROUTE_NEEDED_INTERVAL_SECONDS:
                continue
            emitted_at[task.id] = now
            if task.project_id not in routers:
                project = await self.db.get_project(task.project_id)
                routers[task.project_id] = (
                    getattr(project, "assignment_playbook_id", None) if project else None
                )
            await self._emit_task_event(
                "task.route_needed",
                task,
                description=task.description or "",
                priority=task.priority,
                task_type=str(_value(task.task_type) or ""),
                intelligence_class=(task.intelligence_class or "").strip() or None,
                profile_id=task.profile_id or None,
                # Mandatory routing §6.1 (additive): the project's bound
                # router, which the router's guard compares with its own id,
                # and the filer's class hint.
                router=routers[task.project_id],
                class_hint=(getattr(task, "class_hint", None) or "").strip() or None,
            )
            count += 1
        if count:
            logger.debug("route_needed: emitted for %d task(s)", count)
        return count

    def _clear_route_needed_throttle(self, task_id: str) -> None:
        """Let the next cascade emit for *task_id* at once (spec §6.1, ``aq task route``)."""
        self.__dict__.setdefault("_route_needed_emitted", {}).pop(task_id, None)
