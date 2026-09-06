"""Emit ``task.route_needed`` for work that lacks the fields a worker needs.

This is the whole of the orchestrator's involvement in assignment routing.
It decides nothing: it notices a task with no ``intelligence_class`` or no
``profile_id`` that is otherwise eligible to be picked up, and tells the
playbook layer.  ``default-assignment-routing`` (or a project-scope copy)
does the rest through ``task_route_options`` and ``task_route``.
"""

from __future__ import annotations

import logging
import time
from enum import Enum

from sqlalchemy import and_, literal, or_, select

from src.database.queries.blocked_state import apply_label_filters
from src.database.tables import (
    gates as gates_table,
    task_gates as task_gates_table,
    tasks as tasks_table,
)
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: Re-emit for the same task at most this often — a playbook run needs time
#: to read, decide and write before the cascade asks again.
ROUTE_NEEDED_INTERVAL_SECONDS = 120.0


def _value(value):
    return value.value if isinstance(value, Enum) else value


class RouteNeededMixin:
    _route_needed_emitted: dict[str, float]

    async def _route_needed_candidates(self):
        """Tasks nothing can pick up until they carry a class and a profile.

        READY / BLOCKED tasks, plus DEFINED tasks that are unblocked or whose
        only blocker is their own open ``routing`` gate (a worker-filed root
        is born that way, swarm work model §12).  Unassigned, not a plan
        subtask, and missing ``intelligence_class`` or ``profile_id``.
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
        statement = select(tasks_table).where(
            or_(
                tasks_table.c.status.in_([TaskStatus.READY.value, TaskStatus.BLOCKED.value]),
                and_(
                    tasks_table.c.status == TaskStatus.DEFINED.value,
                    or_(tasks_table.c.is_blocked == 0, open_routing_gate),
                ),
            ),
            tasks_table.c.assigned_agent_id.is_(None),
            tasks_table.c.is_plan_subtask == 0,
            or_(
                tasks_table.c.intelligence_class.is_(None),
                tasks_table.c.intelligence_class == "",
                tasks_table.c.profile_id.is_(None),
                tasks_table.c.profile_id == "",
            ),
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
        candidates = await self._route_needed_candidates()
        live = {task.id for task in candidates}
        for task_id in [t for t in emitted_at if t not in live]:
            emitted_at.pop(task_id, None)
        count = 0
        for task in candidates:
            last = emitted_at.get(task.id, 0.0)
            if now - last < ROUTE_NEEDED_INTERVAL_SECONDS:
                continue
            emitted_at[task.id] = now
            await self._emit_task_event(
                "task.route_needed",
                task,
                description=task.description or "",
                priority=task.priority,
                task_type=str(_value(task.task_type) or ""),
                intelligence_class=(task.intelligence_class or "").strip() or None,
                profile_id=task.profile_id or None,
            )
            count += 1
        if count:
            logger.debug("route_needed: emitted for %d task(s)", count)
        return count
