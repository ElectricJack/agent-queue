"""Task dependency operations."""

from __future__ import annotations

from sqlalchemy import and_, delete, insert, or_, select

from src.database.tables import task_dependencies, tasks
from src.models import Task, TaskStatus


class DependencyQueryMixin:
    """Query mixin for task dependency operations.  Expects ``self._engine``."""

    async def add_dependency(self, task_id: str, depends_on: str) -> None:
        """Add a dependency edge between two tasks."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(task_dependencies).values(task_id=task_id, depends_on_task_id=depends_on)
            )

    async def get_dependencies(self, task_id: str) -> set[str]:
        """Return IDs of tasks that *task_id* depends on."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_dependencies.c.depends_on_task_id).where(
                    task_dependencies.c.task_id == task_id
                )
            )
            return {r[0] for r in result.fetchall()}

    async def get_all_dependencies(self) -> dict[str, set[str]]:
        """Return the full dependency graph as {task_id: {dep_ids}}."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(task_dependencies))
            deps: dict[str, set[str]] = {}
            for r in result.mappings().fetchall():
                deps.setdefault(r["task_id"], set()).add(r["depends_on_task_id"])
            return deps

    async def are_dependencies_met(self, task_id: str) -> bool:
        """Check whether all upstream dependencies are COMPLETED."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_dependencies.c.depends_on_task_id, tasks.c.status)
                .select_from(
                    task_dependencies.join(
                        tasks, tasks.c.id == task_dependencies.c.depends_on_task_id
                    )
                )
                .where(task_dependencies.c.task_id == task_id)
            )
            rows = result.mappings().fetchall()
            return all(r["status"] == TaskStatus.COMPLETED.value for r in rows)

    async def get_stuck_active_tasks(
        self,
        assigned_threshold_seconds: int,
        in_progress_threshold_seconds: int,
        now: float,
        project_id: str | None = None,
    ) -> list[Task]:
        """Return tasks stuck in ASSIGNED or IN_PROGRESS beyond their
        per-status threshold.

        A task is "stuck" when its ``updated_at`` timestamp is older than
        ``now - threshold`` for its current status. ``updated_at``
        advances on every state transition, so it is the correct
        "time-in-current-state" proxy — ``created_at`` is not.

        Parameters
        ----------
        assigned_threshold_seconds:
            Max time (seconds) a task may stay ASSIGNED before being
            considered stuck.
        in_progress_threshold_seconds:
            Max time (seconds) a task may stay IN_PROGRESS before being
            considered stuck.
        now:
            Reference timestamp (seconds since epoch). Callers pass the
            trigger event's ``tick_time`` so repeated invocations are
            deterministic.
        project_id:
            Optional filter — when provided, only tasks in the given
            project are considered.

        Returns
        -------
        list[Task]
            Stuck tasks ordered by ``updated_at`` ascending (oldest
            first), so the most-stuck task surfaces first in the result.
        """
        async with self._engine.begin() as conn:
            condition = or_(
                and_(
                    tasks.c.status == TaskStatus.ASSIGNED.value,
                    tasks.c.updated_at < (now - assigned_threshold_seconds),
                ),
                and_(
                    tasks.c.status == TaskStatus.IN_PROGRESS.value,
                    tasks.c.updated_at < (now - in_progress_threshold_seconds),
                ),
            )
            stmt = select(tasks).where(condition)
            if project_id is not None:
                stmt = stmt.where(tasks.c.project_id == project_id)
            stmt = stmt.order_by(tasks.c.updated_at.asc())
            result = await conn.execute(stmt)
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def get_stuck_defined_tasks(self, threshold_seconds: int) -> list[Task]:
        """Return DEFINED tasks blocked by a BLOCKED or FAILED dependency."""
        async with self._engine.begin() as conn:
            dep_tasks = tasks.alias("dep")
            result = await conn.execute(
                select(tasks)
                .distinct()
                .select_from(
                    tasks.join(task_dependencies, task_dependencies.c.task_id == tasks.c.id).join(
                        dep_tasks, dep_tasks.c.id == task_dependencies.c.depends_on_task_id
                    )
                )
                .where(
                    and_(
                        tasks.c.status == TaskStatus.DEFINED.value,
                        dep_tasks.c.status.in_([TaskStatus.BLOCKED.value, TaskStatus.FAILED.value]),
                    )
                )
                .order_by(tasks.c.created_at.asc())
            )
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def get_blocking_dependencies(
        self,
        task_id: str,
    ) -> list[tuple[str, str, str]]:
        """Return (dep_task_id, dep_title, dep_status) for unmet dependencies."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(tasks.c.id, tasks.c.title, tasks.c.status)
                .select_from(
                    task_dependencies.join(
                        tasks, tasks.c.id == task_dependencies.c.depends_on_task_id
                    )
                )
                .where(
                    and_(
                        task_dependencies.c.task_id == task_id,
                        tasks.c.status != TaskStatus.COMPLETED.value,
                    )
                )
            )
            return [(r[0], r[1], r[2]) for r in result.fetchall()]

    async def get_dependents(self, task_id: str) -> set[str]:
        """Return task IDs that directly depend on *task_id* (reverse lookup)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_dependencies.c.task_id).where(
                    task_dependencies.c.depends_on_task_id == task_id
                )
            )
            return {r[0] for r in result.fetchall()}

    async def get_dependency_map_for_tasks(
        self,
        task_ids: list[str],
    ) -> dict[str, dict]:
        """Batch-fetch dependency data for multiple tasks in two queries.

        Returns a mapping of task_id -> {"depends_on": [...], "blocks": [...]}.
        """
        if not task_ids:
            return {}

        result_map: dict[str, dict] = {tid: {"depends_on": [], "blocks": []} for tid in task_ids}

        async with self._engine.begin() as conn:
            # Forward dependencies
            result = await conn.execute(
                select(
                    task_dependencies.c.task_id,
                    task_dependencies.c.depends_on_task_id,
                    tasks.c.status,
                )
                .select_from(
                    task_dependencies.join(
                        tasks, tasks.c.id == task_dependencies.c.depends_on_task_id
                    )
                )
                .where(task_dependencies.c.task_id.in_(task_ids))
            )
            for row in result.mappings().fetchall():
                tid = row["task_id"]
                if tid in result_map:
                    result_map[tid]["depends_on"].append(
                        {
                            "id": row["depends_on_task_id"],
                            "status": row["status"],
                        }
                    )

            # Reverse dependencies (blocks)
            result = await conn.execute(
                select(
                    task_dependencies.c.depends_on_task_id,
                    task_dependencies.c.task_id,
                ).where(task_dependencies.c.depends_on_task_id.in_(task_ids))
            )
            for row in result.mappings().fetchall():
                blocked_by = row["depends_on_task_id"]
                if blocked_by in result_map:
                    result_map[blocked_by]["blocks"].append(row["task_id"])

        for entry in result_map.values():
            entry["blocks"] = sorted(entry["blocks"])

        return result_map

    async def remove_dependency(self, task_id: str, depends_on: str) -> None:
        """Remove a single dependency edge."""
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(task_dependencies).where(
                    and_(
                        task_dependencies.c.task_id == task_id,
                        task_dependencies.c.depends_on_task_id == depends_on,
                    )
                )
            )

    async def remove_all_dependencies_on(self, depends_on_task_id: str) -> None:
        """Remove all dependency edges pointing to a given task."""
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(task_dependencies).where(
                    task_dependencies.c.depends_on_task_id == depends_on_task_id
                )
            )
