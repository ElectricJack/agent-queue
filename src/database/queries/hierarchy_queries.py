"""Hierarchy — the single writer for parent/child membership (spec Part I).

Truth is the ``parent-child`` edge; ``tasks.parent_task_id`` is a derived
cache that only :meth:`HierarchyQueryMixin.set_parent` writes, in the same
transaction as the edge, the blocked-state recompute and container
settlement.  Every mutation here takes ``conn`` and never opens its own
transaction.
"""

from __future__ import annotations

import time

from sqlalchemy import and_, delete, exists, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.queries.task_queries import TransitionResult
from src.database.tables import sessions, task_dependencies, task_metadata, tasks
from src.models import DepType, Task, TaskStatus
from src.state_machine import CyclicDependencyError, validate_dag_with_new_edge
from src.task_names import MAX_STRUCTURAL_DEPTH, child_task_id

# Container statuses that withhold their children (work-graph §3.1) are
# enforced by BlockedStateMixin's satisfaction table
# (``_WITHHOLDING_PARENT_STATUSES`` in blocked_state.py) — this module only
# needs the terminal ``COMPLETED`` check for ``container_closed``.

CONTAINER_KEY = "container"
CONTAINER_VALUE = "true"  # json.dumps(True); matches set_task_meta's encoding

#: Session states that still hold their task — a container in one of these
#: cannot be settled out from under a live worker (spec §7).
LIVE_SESSION_STATES = ("starting", "running", "draining")


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
        settle_result = await self.settle_containers(
            {p for p in (old_parent, parent_id) if p}, conn=conn
        )
        flipped |= settle_result.flipped
        return flipped, settle_result.settled

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

    # -- settlement -------------------------------------------------------

    async def settle_containers(self, seeds: set[str], *, conn, depth: int = 0) -> TransitionResult:
        """Complete every seeded container whose children are all done (spec §7).

        Predicate: container flag ∧ status = IN_PROGRESS ∧ no live session holds
        it ∧ no non-COMPLETED child (vacuously true when empty).  Each hit goes
        through ``_apply_transition``, which — via its ``_settle_depth``
        keyword — seeds its own parent back into this method one level
        deeper; the climb is bounded by ``MAX_STRUCTURAL_DEPTH`` levels of
        recursion, enforced by the ``depth`` guard below.  ``depth`` counts
        the settlement hop about to run (the first hop, off the task that
        actually transitioned, is ``depth=1``), so the guard is ``depth >
        MAX_STRUCTURAL_DEPTH`` — not ``>=`` — or a 3-level cap would only
        ever let 2 ancestors settle before blocking (see
        ``test_settles_exactly_max_structural_depth_levels``).
        """
        result = TransitionResult()
        pending = {s for s in seeds if s}
        if not pending or depth > MAX_STRUCTURAL_DEPTH:
            return result

        child = tasks.alias("child")
        stmt = select(tasks.c.id).where(
            and_(
                tasks.c.id.in_(sorted(pending)),
                tasks.c.status == TaskStatus.IN_PROGRESS.value,
                exists(
                    select(literal(1)).where(
                        and_(
                            task_metadata.c.task_id == tasks.c.id,
                            task_metadata.c.key == CONTAINER_KEY,
                            task_metadata.c.value == CONTAINER_VALUE,
                        )
                    )
                ),
                ~exists(
                    select(literal(1)).where(
                        and_(
                            sessions.c.task_id == tasks.c.id,
                            sessions.c.state.in_(LIVE_SESSION_STATES),
                        )
                    )
                ),
                ~exists(
                    select(literal(1)).where(
                        and_(
                            child.c.parent_task_id == tasks.c.id,
                            child.c.status != TaskStatus.COMPLETED.value,
                        )
                    )
                ),
            )
        )
        hits = [r[0] for r in (await conn.execute(stmt)).fetchall()]
        for cid in hits:
            # _apply_transition seeds the container's own parent back into
            # this method at depth + 1, so grandparents are handled by
            # recursion; merge everything it settled and flipped.
            res = await self._apply_transition(
                conn,
                cid,
                TaskStatus.COMPLETED,
                context="subtasks_completed",
                _settle_depth=depth,
            )
            result.settled.append(cid)
            result.settled.extend(res.settled)
            result.flipped |= res.flipped
        return result

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

    # -- container-close semantics ---------------------------------------

    async def open_children(self, task_id: str, *, conn=None) -> list[str]:
        """Direct children not yet terminal (spec §7 close rule)."""
        terminal = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
        stmt = (
            select(tasks.c.id)
            .where(and_(tasks.c.parent_task_id == task_id, tasks.c.status.notin_(terminal)))
            .order_by(tasks.c.id)
        )
        if conn is not None:
            return [r[0] for r in (await conn.execute(stmt)).fetchall()]
        async with self._engine.begin() as c:
            return [r[0] for r in (await c.execute(stmt)).fetchall()]

    async def live_descendant_sessions(self, task_id: str, *, conn) -> list[tuple[str, str]]:
        """Live sessions holding any task in *task_id*'s subtree.

        Lock order is sessions-before-tasks to match the claim path (spec §7):
        on Postgres the rows are taken ``FOR UPDATE`` so a session cannot
        start holding a descendant between this check and the abandonment.
        """
        ids = await self.subtree_ids(task_id, conn=conn)
        stmt = select(sessions.c.id, sessions.c.task_id).where(
            and_(sessions.c.task_id.in_(ids), sessions.c.state.in_(LIVE_SESSION_STATES))
        )
        if conn.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        return [(r[0], r[1]) for r in (await conn.execute(stmt)).fetchall()]

    async def abandon_subtree(self, task_id: str, *, conn) -> list[str]:
        """Close every non-terminal descendant as ``abandoned`` (spec §7)."""
        ids = await self.subtree_ids(task_id, conn=conn)
        ids = [i for i in ids if i != task_id]
        if not ids:
            return []
        stmt = select(tasks.c.id, tasks.c.status).where(tasks.c.id.in_(ids))
        if conn.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        rows = (await conn.execute(stmt)).fetchall()
        terminal = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
        # Deepest first so each container settles naturally after its children.
        depth = {tid: i for i, tid in enumerate(ids)}
        open_ids = sorted((r[0] for r in rows if r[1] not in terminal), key=lambda t: -depth[t])
        abandoned: list[str] = []
        for tid in open_ids:
            await self._upsert_meta(tid, "work_outcome", "abandoned", conn=conn)
            await self._apply_transition(
                conn,
                tid,
                TaskStatus.COMPLETED,
                context="abandoned_by_container",
                assigned_agent_id=None,
            )
            abandoned.append(tid)
        return abandoned

    async def _upsert_meta(self, task_id: str, key: str, value, *, conn) -> None:
        import json

        encoded = json.dumps(value)
        res = await conn.execute(
            update(task_metadata)
            .where(and_(task_metadata.c.task_id == task_id, task_metadata.c.key == key))
            .values(value=encoded)
        )
        if res.rowcount == 0:
            await conn.execute(
                insert(task_metadata).values(task_id=task_id, key=key, value=encoded)
            )
