"""Task dependency operations.

Edges are typed (``task_dependencies.dep_type``, work-graph design §3).  Only
the four *blocking* kinds gate readiness; the rest are provenance.  Every
mutation here recomputes ``tasks.is_blocked`` inside its own transaction so
the projection can never lag the graph.
"""

from __future__ import annotations

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import task_dependencies, tasks
from src.models import BLOCKING_DEP_TYPES, DepType, Task, TaskStatus


def _dep_type_filter(dep_types: frozenset[str] | set[str] | None):
    """Build the ``dep_type IN (...)`` clause, defaulting to blocking edges."""
    effective = BLOCKING_DEP_TYPES if dep_types is None else dep_types
    return task_dependencies.c.dep_type.in_(sorted(effective))


class DependencyQueryMixin:
    """Query mixin for task dependency operations.  Expects ``self._engine``."""

    async def add_dependency(
        self,
        task_id: str,
        depends_on: str,
        dep_type: str = DepType.BLOCKS.value,
    ) -> None:
        """Add a typed dependency edge between two tasks.

        Insert + blocked-state recompute in one transaction (design §4.2).
        Idempotent: adding an edge that already exists (same
        ``(task_id, depends_on_task_id, dep_type)`` — the composite PK on
        ``task_dependencies``) is a no-op that does not raise.  Callers
        can safely retry (e.g. pipeline reruns) without workarounds.

        ``parent-child`` edges are delegated to :meth:`HierarchyQueryMixin.set_parent`,
        the single writer that keeps the edge and the ``tasks.parent_task_id``
        cache in sync (spec Part I §5).
        """
        if dep_type == DepType.PARENT_CHILD.value:
            async with self._engine.begin() as conn:
                flipped, settled = await self.set_parent(task_id, depends_on, conn=conn)
            await self.log_blocked_flips(flipped)
            await self._notify_settled(settled)
            return

        _insert = pg_insert if self._engine.dialect.name == "postgresql" else sqlite_insert
        async with self._engine.begin() as conn:
            await conn.execute(
                _insert(task_dependencies)
                .values(
                    task_id=task_id,
                    depends_on_task_id=depends_on,
                    dep_type=dep_type,
                )
                .on_conflict_do_nothing()
            )
            flipped = await self.recompute_blocked({task_id, depends_on}, conn=conn)
        await self.log_blocked_flips(flipped)

    async def get_dependencies(
        self,
        task_id: str,
        dep_types: frozenset[str] | None = None,
    ) -> set[str]:
        """Return IDs of tasks that *task_id* depends on.

        Defaults to the *blocking* edge kinds so existing callers keep their
        "these hold me back" semantics once provenance edges are present.
        Pass an explicit set to widen.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_dependencies.c.depends_on_task_id).where(
                    and_(
                        task_dependencies.c.task_id == task_id,
                        _dep_type_filter(dep_types),
                    )
                )
            )
            return {r[0] for r in result.fetchall()}

    async def get_typed_dependencies(self, task_id: str) -> list[tuple[str, str]]:
        """Return ``(depends_on_task_id, dep_type)`` for every outgoing edge."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(
                    task_dependencies.c.depends_on_task_id,
                    task_dependencies.c.dep_type,
                )
                .where(task_dependencies.c.task_id == task_id)
                .order_by(
                    task_dependencies.c.dep_type.asc(),
                    task_dependencies.c.depends_on_task_id.asc(),
                )
            )
            return [(r[0], r[1]) for r in result.fetchall()]

    async def get_all_dependencies(
        self,
        dep_types: frozenset[str] | None = None,
    ) -> dict[str, set[str]]:
        """Return the dependency graph as ``{task_id: {dep_ids}}``.

        Blocking edges only by default — cycle validation runs over exactly
        the edges that can deadlock (design §11).
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(
                    task_dependencies.c.task_id,
                    task_dependencies.c.depends_on_task_id,
                ).where(_dep_type_filter(dep_types))
            )
            deps: dict[str, set[str]] = {}
            for tid, dep_id in result.fetchall():
                deps.setdefault(tid, set()).add(dep_id)
            return deps

    async def get_parent_child_edges(self) -> dict[str, set[str]]:
        """Return ``{child_id: {container_ids}}`` for ``parent-child`` edges.

        Feeds the ``waits-for`` deadlock rule (design §11): a waiter may not
        fan in over a container it is itself a descendant of.
        """
        return await self.get_all_dependencies(dep_types=frozenset({DepType.PARENT_CHILD.value}))

    async def are_dependencies_met(self, task_id: str) -> bool:
        """Check whether all upstream *blocking* dependencies are COMPLETED.

        Legacy readiness scan — retained deliberately through the shadow-mode
        window so ``_check_defined_tasks`` has an independent oracle to
        compare the ``is_blocked`` projection against.  It collapses into a
        shim over ``is_blocked`` once the projection becomes authoritative
        (implementation spec §4.2).
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_dependencies.c.depends_on_task_id, tasks.c.status)
                .select_from(
                    task_dependencies.join(
                        tasks, tasks.c.id == task_dependencies.c.depends_on_task_id
                    )
                )
                .where(
                    and_(
                        task_dependencies.c.task_id == task_id,
                        _dep_type_filter(None),
                    )
                )
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
                        _dep_type_filter(None),
                        dep_tasks.c.status.in_([TaskStatus.BLOCKED.value, TaskStatus.FAILED.value]),
                    )
                )
                .order_by(tasks.c.created_at.asc())
            )
            return [self._row_to_task(r) for r in result.mappings().fetchall()]

    async def get_blocking_dependencies(
        self,
        task_id: str,
    ) -> list[tuple[str, str, str, str, str]]:
        """Return unmet blocking dependencies for *task_id*.

        Each entry is ``(dep_task_id, dep_title, dep_status, dep_type,
        dep_project_id)``.  The project id lets renderers name the other
        project on cross-project edges (design §3.3).
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(
                    tasks.c.id,
                    tasks.c.title,
                    tasks.c.status,
                    task_dependencies.c.dep_type,
                    tasks.c.project_id,
                )
                .select_from(
                    task_dependencies.join(
                        tasks, tasks.c.id == task_dependencies.c.depends_on_task_id
                    )
                )
                .where(
                    and_(
                        task_dependencies.c.task_id == task_id,
                        _dep_type_filter(None),
                        tasks.c.status != TaskStatus.COMPLETED.value,
                    )
                )
            )
            return [(r[0], r[1], r[2], r[3], r[4]) for r in result.fetchall()]

    async def get_dependents(
        self,
        task_id: str,
        dep_types: frozenset[str] | None = None,
    ) -> set[str]:
        """Return task IDs that directly depend on *task_id* (reverse lookup)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(task_dependencies.c.task_id).where(
                    and_(
                        task_dependencies.c.depends_on_task_id == task_id,
                        _dep_type_filter(dep_types),
                    )
                )
            )
            return {r[0] for r in result.fetchall()}

    async def get_dependency_map_for_tasks(
        self,
        task_ids: list[str],
    ) -> dict[str, dict]:
        """Batch-fetch dependency data for multiple tasks in two queries.

        Returns a mapping of task_id -> {"depends_on": [...], "blocks": [...]}.
        Every edge kind is included — this feeds graph views, which show
        provenance edges too; each ``depends_on`` entry carries its
        ``dep_type``.
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
                    task_dependencies.c.dep_type,
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
                            "dep_type": row["dep_type"],
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
            entry["blocks"] = sorted(set(entry["blocks"]))

        return result_map

    async def remove_dependency(
        self,
        task_id: str,
        depends_on: str,
        dep_type: str | None = None,
    ) -> None:
        """Remove a dependency edge.

        ``dep_type=None`` removes every edge kind between the pair.
        Delete + recompute in one transaction.

        ``parent-child`` edges are delegated to :meth:`HierarchyQueryMixin.set_parent`
        so the ``tasks.parent_task_id`` cache never lags the edge.
        """
        if dep_type == DepType.PARENT_CHILD.value:
            async with self._engine.begin() as conn:
                current = (
                    await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == task_id))
                ).fetchone()
                if current is None or current[0] != depends_on:
                    return
                flipped, settled = await self.set_parent(task_id, None, conn=conn)
            await self.log_blocked_flips(flipped)
            await self._notify_settled(settled)
            return

        conditions = [
            task_dependencies.c.task_id == task_id,
            task_dependencies.c.depends_on_task_id == depends_on,
        ]
        if dep_type is not None:
            conditions.append(task_dependencies.c.dep_type == dep_type)
        parent_flipped: set[str] = set()
        parent_settled: list[str] = []
        async with self._engine.begin() as conn:
            if dep_type is None:
                current = (
                    await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id == task_id))
                ).fetchone()
                if current is not None and current[0] == depends_on:
                    parent_flipped, parent_settled = await self.set_parent(task_id, None, conn=conn)
                    conditions.append(task_dependencies.c.dep_type != DepType.PARENT_CHILD.value)
            await conn.execute(delete(task_dependencies).where(and_(*conditions)))
            flipped = await self.recompute_blocked({task_id, depends_on}, conn=conn)
            flipped |= parent_flipped
        await self.log_blocked_flips(flipped)
        await self._notify_settled(parent_settled)

    async def get_transitive_dependents(
        self, task_id: str, edge_types: tuple[str, ...]
    ) -> list[str]:
        """Return all task ids reachable by walking dependents over ``edge_types``.

        BFS. Every hop follows edges where ``depends_on_task_id == cursor`` and
        ``dep_type`` is in the whitelist. The seed itself is *not* in the
        result. Terminates on cycles because visited ids are tracked.
        """
        from sqlalchemy import and_, select
        from src.database.tables import task_dependencies

        found: set[str] = set()
        frontier: list[str] = [task_id]
        while frontier:
            async with self._engine.begin() as conn:
                rows = (
                    await conn.execute(
                        select(task_dependencies.c.task_id).where(
                            and_(
                                task_dependencies.c.depends_on_task_id.in_(frontier),
                                task_dependencies.c.dep_type.in_(edge_types),
                            )
                        )
                    )
                ).fetchall()
            next_frontier = [r[0] for r in rows if r[0] not in found and r[0] != task_id]
            found.update(next_frontier)
            frontier = next_frontier
        return sorted(found)

    async def remove_all_dependencies_on(self, depends_on_task_id: str) -> None:
        """Remove all dependency edges pointing to a given task."""
        async with self._engine.begin() as conn:
            # Snapshot the former dependents *before* the delete — afterwards
            # the edges that identify them are gone.
            rows = await conn.execute(
                select(task_dependencies.c.task_id).where(
                    task_dependencies.c.depends_on_task_id == depends_on_task_id
                )
            )
            former = {r[0] for r in rows.fetchall()}
            await conn.execute(
                delete(task_dependencies).where(
                    task_dependencies.c.depends_on_task_id == depends_on_task_id
                )
            )
            flipped = await self.recompute_blocked(former | {depends_on_task_id}, conn=conn)
        await self.log_blocked_flips(flipped)
