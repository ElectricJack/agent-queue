"""Hierarchy — the single writer for parent/child membership (spec Part I).

Truth is the ``parent-child`` edge; ``tasks.parent_task_id`` is a derived
cache that only :meth:`HierarchyQueryMixin.set_parent` writes, in the same
transaction as the edge, the blocked-state recompute and container
settlement.  Every mutation here takes ``conn`` and never opens its own
transaction.
"""

from __future__ import annotations

import time

from sqlalchemy import and_, delete, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import task_dependencies, task_metadata, tasks
from src.models import DepType, Task, TaskStatus
from src.state_machine import CyclicDependencyError, validate_dag_with_new_edge
from src.task_names import MAX_STRUCTURAL_DEPTH, child_task_id

# Container statuses that withhold their children (work-graph §3.1) are
# enforced by BlockedStateMixin's satisfaction table
# (``_WITHHOLDING_PARENT_STATUSES`` in blocked_state.py) — this module only
# needs the terminal ``COMPLETED`` check for ``container_closed``.

CONTAINER_KEY = "container"
CONTAINER_VALUE = "true"  # json.dumps(True); matches set_task_meta's encoding


class HierarchyError(Exception):
    """A rejected hierarchy mutation.  ``code`` is the stable machine string."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class HierarchyQueryMixin:
    """Expects ``self._engine`` plus BlockedStateMixin and TaskQueryMixin."""

    # -- container flag -------------------------------------------------

    async def mark_container(self, task_id: str, *, conn) -> None:
        """Set ``task_metadata.container = true`` (idempotent).  Never cleared."""
        dialect = conn.dialect.name
        ins = pg_insert if dialect == "postgresql" else sqlite_insert
        await conn.execute(
            ins(task_metadata)
            .values(task_id=task_id, key=CONTAINER_KEY, value=CONTAINER_VALUE)
            .on_conflict_do_nothing()
        )

    async def is_container(self, task_id: str, *, conn) -> bool:
        row = (
            await conn.execute(
                select(literal(1)).where(
                    and_(
                        task_metadata.c.task_id == task_id,
                        task_metadata.c.key == CONTAINER_KEY,
                        task_metadata.c.value == CONTAINER_VALUE,
                    )
                )
            )
        ).fetchone()
        return row is not None

    # -- structure reads (CTE) -------------------------------------------

    def _ancestor_cte(self, task_id: str):
        base = (
            select(tasks.c.id, tasks.c.parent_task_id, literal(1).label("depth"))
            .where(tasks.c.id == task_id)
            .cte("ancestors", recursive=True)
        )
        parent = tasks.alias("parent")
        rec = select(parent.c.id, parent.c.parent_task_id, (base.c.depth + 1).label("depth")).where(
            parent.c.id == base.c.parent_task_id
        )
        return base.union_all(rec)

    def _descendant_cte(self, root_id: str):
        base = (
            select(tasks.c.id, tasks.c.parent_task_id, literal(1).label("depth"))
            .where(tasks.c.id == root_id)
            .cte("descendants", recursive=True)
        )
        child = tasks.alias("child")
        rec = select(child.c.id, child.c.parent_task_id, (base.c.depth + 1).label("depth")).where(
            child.c.parent_task_id == base.c.id
        )
        return base.union_all(rec)

    async def structural_depth(self, task_id: str, *, conn) -> int:
        """Live parent-child chain length from *task_id* to its root (root = 1)."""
        cte = self._ancestor_cte(task_id)
        row = (
            await conn.execute(select(cte.c.depth).order_by(cte.c.depth.desc()).limit(1))
        ).fetchone()
        return int(row[0]) if row else 0

    async def subtree_height(self, task_id: str, *, conn) -> int:
        """Height of the subtree rooted at *task_id* (leaf = 1)."""
        cte = self._descendant_cte(task_id)
        row = (
            await conn.execute(select(cte.c.depth).order_by(cte.c.depth.desc()).limit(1))
        ).fetchone()
        return int(row[0]) if row else 0

    async def subtree_ids(self, root_id: str, *, conn) -> list[str]:
        """Every id in the subtree, root first, shallow before deep."""
        cte = self._descendant_cte(root_id)
        rows = (await conn.execute(select(cte.c.id).order_by(cte.c.depth, cte.c.id))).fetchall()
        return [r[0] for r in rows]

    # -- the single writer ----------------------------------------------

    async def set_parent(
        self, task_id: str, parent_id: str | None, *, conn
    ) -> tuple[set[str], list[str]]:
        """Move *task_id* under *parent_id* (``None`` = root).  Spec §5.

        Same transaction: delete any existing parent-child edge, insert the
        new one, write ``tasks.parent_task_id``, recompute ``is_blocked``
        over the affected set, mark the new parent a container, settle both
        the old and the new container.  Returns the blocked-state flips.
        """
        task_row = (
            await conn.execute(
                select(tasks.c.id, tasks.c.project_id, tasks.c.parent_task_id).where(
                    tasks.c.id == task_id
                )
            )
        ).fetchone()
        if task_row is None:
            raise HierarchyError("not_found", task_id)
        old_parent = task_row.parent_task_id

        if parent_id is not None:
            if parent_id == task_id:
                raise HierarchyError("self_parent", task_id)
            parent_row = (
                await conn.execute(
                    select(tasks.c.id, tasks.c.project_id, tasks.c.status).where(
                        tasks.c.id == parent_id
                    )
                )
            ).fetchone()
            if parent_row is None:
                raise HierarchyError("not_found", parent_id)
            if parent_row.project_id != task_row.project_id:
                raise HierarchyError(
                    "cross_project",
                    f"{task_id} is in {task_row.project_id}, "
                    f"{parent_id} in {parent_row.project_id}",
                )
            if parent_row.status == TaskStatus.COMPLETED.value:
                raise HierarchyError("container_closed", parent_id)
            # Cycle: the new parent must not be inside task_id's subtree.
            if parent_id in await self.subtree_ids(task_id, conn=conn):
                raise HierarchyError("cycle", f"{parent_id} is a descendant of {task_id}")
            # Blocking-edge DAG check (waits-for / blocks edges could loop
            # through the new parent-child edge).  Runs before the depth
            # check so a cyclic request reports ``cycle``, not ``depth``
            # (spec order: self_parent, not_found, cross_project,
            # container_closed, cycle, depth).
            deps = await self._blocking_edges(conn)
            try:
                validate_dag_with_new_edge(deps, task_id, parent_id, DepType.PARENT_CHILD.value)
            except CyclicDependencyError as exc:
                raise HierarchyError("cycle", str(exc)) from exc
            depth = await self.structural_depth(parent_id, conn=conn)
            height = await self.subtree_height(task_id, conn=conn)
            if depth + height > MAX_STRUCTURAL_DEPTH:
                raise HierarchyError(
                    "depth",
                    f"parent depth {depth} + subtree height {height} > {MAX_STRUCTURAL_DEPTH}",
                )

        affected = await self._collect_affected({task_id}, conn)
        if old_parent:
            affected.add(old_parent)
        if parent_id:
            affected.add(parent_id)

        await conn.execute(
            delete(task_dependencies).where(
                and_(
                    task_dependencies.c.task_id == task_id,
                    task_dependencies.c.dep_type == DepType.PARENT_CHILD.value,
                )
            )
        )
        if parent_id is not None:
            await conn.execute(
                insert(task_dependencies).values(
                    task_id=task_id,
                    depends_on_task_id=parent_id,
                    dep_type=DepType.PARENT_CHILD.value,
                )
            )
            await self.mark_container(parent_id, conn=conn)
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id)
            .values(parent_task_id=parent_id, updated_at=time.time())
        )
        affected |= await self._collect_affected({task_id}, conn)
        flipped = await self.recompute_blocked(affected, conn=conn)
        settled = await self.settle_containers({p for p in (old_parent, parent_id) if p}, conn=conn)
        return flipped, settled

    async def _blocking_edges(self, conn) -> dict[str, set[str]]:
        from src.models import BLOCKING_DEP_TYPES

        rows = (
            await conn.execute(
                select(task_dependencies.c.task_id, task_dependencies.c.depends_on_task_id).where(
                    task_dependencies.c.dep_type.in_(sorted(BLOCKING_DEP_TYPES))
                )
            )
        ).fetchall()
        deps: dict[str, set[str]] = {}
        for tid, dep in rows:
            deps.setdefault(tid, set()).add(dep)
        return deps

    # -- settlement (filled in by Task 5) ---------------------------------

    async def settle_containers(self, seeds: set[str], *, conn) -> list[str]:
        """Complete every seeded container whose children are all done (spec §7)."""
        return []  # replaced in Task 5

    # -- creation -------------------------------------------------------

    async def create_task_under(self, task: Task, parent_id: str) -> tuple[str, bool]:
        """Insert *task* as a child of *parent_id* in one transaction (spec §6).

        Reserves the dotted id, inserts the row, links it via
        :meth:`set_parent` — or, at the naming cap, gives it a root id and a
        ``discovered-from`` edge.  Returns ``(task_id, capped)``.
        """
        async with self._engine.begin() as conn:
            task_id, capped = await child_task_id(conn, parent_id)
            task.id = task_id
            task.parent_task_id = None  # set_parent owns the pointer
            await self._insert_task_row(task, conn=conn)
            if capped:
                await conn.execute(
                    insert(task_dependencies).values(
                        task_id=task_id,
                        depends_on_task_id=parent_id,
                        dep_type=DepType.DISCOVERED_FROM.value,
                    )
                )
            else:
                await self.set_parent(task_id, parent_id, conn=conn)
        return task_id, capped
