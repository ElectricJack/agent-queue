"""Hierarchy — the single writer for parent/child membership (spec Part I).

Truth is the ``parent-child`` edge; ``tasks.parent_task_id`` is a derived
cache that only :meth:`HierarchyQueryMixin.set_parent` writes, in the same
transaction as the edge, the blocked-state recompute and container
settlement.  Every mutation here takes ``conn`` and never opens its own
transaction.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from sqlalchemy import and_, case, delete, exists, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.queries.task_queries import TransitionResult
from src.database.tables import (
    agents,
    sessions,
    task_dependencies,
    task_metadata,
    tasks,
    workspaces,
)
from src.models import AgentState, DepType, Task, TaskStatus
from src.state_machine import CyclicDependencyError, validate_dag_with_new_edge
from src.task_names import MAX_STRUCTURAL_DEPTH, child_task_id

# Container statuses that withhold their children (work-graph §3.1) are
# enforced by BlockedStateMixin's satisfaction table
# (``_WITHHOLDING_PARENT_STATUSES`` in blocked_state.py) — this module only
# needs the terminal ``COMPLETED`` check for ``container_closed``.

CONTAINER_KEY = "container"
CONTAINER_VALUE = "true"  # json.dumps(True); matches set_task_meta's encoding


def container_flag_exists():
    """``EXISTS`` clause: the correlated ``tasks`` row carries the container flag.

    The §7 flag is the *only* thing that says a task is settle-only work
    (``creator.PARENT_STATUS``, recovery and the claim frontier all key off
    it, never off "has children"), so the predicate lives here once.
    """
    return exists(
        select(literal(1)).where(
            and_(
                task_metadata.c.task_id == tasks.c.id,
                task_metadata.c.key == CONTAINER_KEY,
                task_metadata.c.value == CONTAINER_VALUE,
            )
        )
    )

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

    async def get_children(
        self,
        parent_id: str,
        *,
        recursive: bool = False,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Task]:
        """Direct (or recursive) children, ordered by depth then id."""
        if recursive:
            cte = self._descendant_cte(parent_id)
            stmt = (
                select(tasks, cte.c.depth)
                .join(cte, cte.c.id == tasks.c.id)
                .where(cte.c.id != parent_id)
                .order_by(cte.c.depth, tasks.c.id)
            )
        else:
            stmt = select(tasks).where(tasks.c.parent_task_id == parent_id).order_by(tasks.c.id)
        if status:
            stmt = stmt.where(tasks.c.status == status)
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset:
            stmt = stmt.offset(offset)
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [self._row_to_task(r) for r in rows]

    async def get_children_summary(self, task_id: str) -> dict | None:
        """One aggregate over the direct children; ``None`` when there are none."""
        s = tasks.c.status
        stmt = select(
            func.count().label("total"),
            func.sum(case((s == TaskStatus.COMPLETED.value, 1), else_=0)).label("done"),
            func.sum(
                case((and_(s == TaskStatus.READY.value, tasks.c.is_blocked == 0), 1), else_=0)
            ).label("ready"),
            func.sum(case((tasks.c.is_blocked == 1, 1), else_=0)).label("blocked"),
            func.sum(
                case(
                    (s.in_((TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value)), 1),
                    else_=0,
                )
            ).label("in_progress"),
        ).where(tasks.c.parent_task_id == task_id)
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).mappings().fetchone()
        if not row or not row["total"]:
            return None
        return {k: int(row[k] or 0) for k in ("total", "done", "ready", "blocked", "in_progress")}

    async def get_task_tree(self, root_task_id: str, *, max_depth: int = 4) -> dict | None:
        """Nested ``{"task", "children"}`` from one recursive CTE (spec §8)."""
        cte = self._descendant_cte(root_task_id)
        stmt = (
            select(tasks, cte.c.depth)
            .join(cte, cte.c.id == tasks.c.id)
            .where(cte.c.depth <= max_depth + 1)
            .order_by(cte.c.depth, tasks.c.id)
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        if not rows:
            return None
        nodes: dict[str, dict] = {}
        root: dict | None = None
        for r in rows:
            task = self._row_to_task(r)
            node = {"task": task, "children": []}
            nodes[task.id] = node
            if task.id == root_task_id:
                root = node
            elif task.parent_task_id in nodes:
                nodes[task.parent_task_id]["children"].append(node)
        return root

    # -- the single writer ----------------------------------------------

    async def set_parent(
        self,
        task_id: str,
        parent_id: str | None,
        *,
        conn,
        description: str | None = None,
    ) -> TransitionResult:
        """Move *task_id* under *parent_id* (``None`` = root).  Spec §5.

        Same transaction: delete any existing parent-child edge, insert the
        new one, write ``tasks.parent_task_id``, recompute ``is_blocked``
        over the affected set, mark the new parent a container, settle both
        the old and the new container, and record any ``task.ready``
        frontier entries the reparent produced (spec §9) — the settlement
        recursion already recorded its own entries in-transaction, so this
        only notes ids in ``flipped`` that settlement did not already
        cover.  Returns a ``TransitionResult`` (``flipped``, ``settled``,
        ``ready``).
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

        # Marked after every validation has passed (a raising set_parent
        # writes nothing) and before the edge writes, while the previous
        # parent is still the one recorded on the row.
        await self.mark_layout_dirty(
            task_row.project_id,
            [task_id],
            f"parent.changed:{old_parent or '-'}",
            conn=conn,
        )

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
                    description=description,
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

        already_noted = {tid for tid, _ in settle_result.ready}
        own_ready_ids = await self._note_frontier_entry(
            conn, flipped - already_noted, reason="unblocked"
        )
        ready = list(settle_result.ready) + [(tid, "unblocked") for tid in own_ready_ids]

        return TransitionResult(
            flipped=flipped, settled=settle_result.settled, ready=ready
        )

    async def set_parent_bulk(
        self, child_ids: list[str], parent_id: str, *, conn
    ) -> tuple[set[str], list[str]]:
        """Link many freshly inserted leaves under one parent (spec §5, §15.2).

        The bulk twin of :meth:`set_parent` for the graph-creation path: the
        parent is validated **once**, the edges are written in two statements
        and the projection is recomputed and settled once, so a 200-node
        graph costs a constant handful of statements instead of ~23 per node.

        The shortcut is only sound for *freshly inserted leaves* — a childless
        node with no blocking out-edges cannot close a cycle and cannot make
        the subtree taller than one level.  Both preconditions are asserted
        here (one statement each) and raise ``cycle_check_skipped`` rather
        than silently skipping the DAG walk.  Use :meth:`set_parent` for
        anything that already sits in the graph.
        """
        ids = list(dict.fromkeys(child_ids))
        if not ids:
            return set(), []
        if parent_id in ids:
            raise HierarchyError("self_parent", parent_id)

        parent_row = (
            await conn.execute(
                select(tasks.c.id, tasks.c.project_id, tasks.c.status).where(
                    tasks.c.id == parent_id
                )
            )
        ).fetchone()
        if parent_row is None:
            raise HierarchyError("not_found", parent_id)
        if parent_row.status == TaskStatus.COMPLETED.value:
            raise HierarchyError("container_closed", parent_id)

        child_rows = (
            await conn.execute(
                select(tasks.c.id, tasks.c.project_id, tasks.c.parent_task_id).where(
                    tasks.c.id.in_(sorted(ids))
                )
            )
        ).fetchall()
        found = {r.id for r in child_rows}
        missing = [i for i in ids if i not in found]
        if missing:
            raise HierarchyError("not_found", missing[0])
        wrong = [r.id for r in child_rows if r.project_id != parent_row.project_id]
        if wrong:
            raise HierarchyError(
                "cross_project",
                f"{wrong[0]} is in another project than {parent_id}",
            )

        # Leaf preconditions — these are what make the skipped DAG walk honest.
        from src.models import BLOCKING_DEP_TYPES

        has_edge = (
            await conn.execute(
                select(task_dependencies.c.task_id)
                .where(
                    and_(
                        task_dependencies.c.task_id.in_(sorted(ids)),
                        task_dependencies.c.dep_type.in_(sorted(BLOCKING_DEP_TYPES)),
                    )
                )
                .limit(1)
            )
        ).fetchone()
        if has_edge is not None:
            raise HierarchyError(
                "cycle_check_skipped",
                f"{has_edge[0]} already has blocking edges; use set_parent",
            )
        has_child = (
            await conn.execute(
                select(tasks.c.id).where(tasks.c.parent_task_id.in_(sorted(ids))).limit(1)
            )
        ).fetchone()
        if has_child is not None:
            raise HierarchyError(
                "cycle_check_skipped",
                f"{has_child[0]}'s parent is one of the children; use set_parent",
            )

        # Subtree height is 1 for every child (asserted above), so one depth
        # read covers the whole batch.
        depth = await self.structural_depth(parent_id, conn=conn)
        if depth + 1 > MAX_STRUCTURAL_DEPTH:
            raise HierarchyError(
                "depth",
                f"parent depth {depth} + subtree height 1 > {MAX_STRUCTURAL_DEPTH}",
            )

        old_parents = {r.parent_task_id for r in child_rows if r.parent_task_id}
        # One mark per child, carrying that child's own previous parent —
        # same contract as set_parent, written before the edges move.
        for row in child_rows:
            await self.mark_layout_dirty(
                row.project_id,
                [row.id],
                f"parent.changed:{row.parent_task_id or '-'}",
                conn=conn,
            )
        now = time.time()
        await conn.execute(
            delete(task_dependencies).where(
                and_(
                    task_dependencies.c.task_id.in_(sorted(ids)),
                    task_dependencies.c.dep_type == DepType.PARENT_CHILD.value,
                )
            )
        )
        await conn.execute(
            insert(task_dependencies),
            [
                {
                    "task_id": cid,
                    "depends_on_task_id": parent_id,
                    "dep_type": DepType.PARENT_CHILD.value,
                }
                for cid in ids
            ],
        )
        await self.mark_container(parent_id, conn=conn)
        await conn.execute(
            update(tasks)
            .where(tasks.c.id.in_(sorted(ids)))
            .values(parent_task_id=parent_id, updated_at=now)
        )

        affected = await self._collect_affected(set(ids), conn)
        affected.add(parent_id)
        affected |= old_parents
        flipped = await self.recompute_blocked(affected, conn=conn)
        settle_result = await self.settle_containers({parent_id} | old_parents, conn=conn)
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
            # Report the container as settled only if the write actually
            # landed: ``_apply_transition`` can decline (a missing row, an
            # enforced invalid transition).  Announcing a completion that did
            # not happen would emit ``task.completed`` for a task still
            # IN_PROGRESS.  Recursion may also have settled it already, so
            # the id is only appended once.
            landed = (
                await conn.execute(select(tasks.c.status).where(tasks.c.id == cid))
            ).scalar()
            if landed == TaskStatus.COMPLETED.value and cid not in result.settled:
                result.settled.append(cid)
            for sid in res.settled:
                if sid not in result.settled:
                    result.settled.append(sid)
            result.flipped |= res.flipped
            result.ready.extend(res.ready)
        return result

    async def settle_candidates(self) -> list[str]:
        """Every container the §7 predicate would settle right now (backstop)."""
        child = tasks.alias("child")
        stmt = select(tasks.c.id).where(
            and_(
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
        async with self._engine.begin() as conn:
            return [r[0] for r in (await conn.execute(stmt)).fetchall()]

    # -- creation -------------------------------------------------------

    async def create_task_under(
        self,
        task: Task,
        parent_id: str,
        *,
        routing_policy: Callable[[Task], bool] | None = None,
        description: str | None = None,
    ) -> tuple[str, bool]:
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
                        description=description,
                    )
                )
            else:
                await self.set_parent(
                    task_id, parent_id, conn=conn, description=description
                )
            task.parent_task_id = None if capped else parent_id
            gated = routing_policy is not None and routing_policy(task)
            if gated:
                await self.create_gate(
                    task.project_id, "routing", "Route task",
                    question="Assign profile + intelligence class (+ workspace if profile needs one).",
                    waiter_task_ids=[task_id], conn=conn,
                )
                task.is_blocked = True
        if gated:
            await self.log_blocked_flips({task_id})
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

    async def live_descendant_sessions(
        self, task_id: str, *, conn, exclude_root: bool = False
    ) -> list[tuple[str, str]]:
        """Live sessions holding any task in *task_id*'s subtree.

        Lock order is sessions-before-tasks to match the claim path (spec §7):
        on Postgres the rows are taken ``FOR UPDATE`` so a session cannot
        start holding a descendant between this check and the abandonment.

        With ``exclude_root=True`` the root task's own sessions are left out,
        so the answer is about *descendants* only.  The ``task_close
        --abandon-children`` path needs that: the closing worker (or the
        container-root session driving the close) is itself live by
        definition, and counting it made every abandon refuse.
        """
        ids = await self.subtree_ids(task_id, conn=conn)
        if exclude_root:
            ids = [i for i in ids if i != task_id]
        if not ids:
            return []
        stmt = select(sessions.c.id, sessions.c.task_id).where(
            and_(sessions.c.task_id.in_(ids), sessions.c.state.in_(LIVE_SESSION_STATES))
        )
        if conn.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        rows = [(r[0], r[1]) for r in (await conn.execute(stmt)).fetchall()]
        # Deduplicate: a task may carry more than one session row, and the
        # caller reports one entry per (session, task) pair.
        return sorted(set(rows))

    async def manually_paused_descendants(self, task_id: str, *, conn) -> list[str]:
        """Descendants a human has paused by hand (``PAUSED``, no ``resume_after``).

        ``abandon_subtree`` cannot move these — the manual-pause guard in
        ``_apply_transition`` raises ``ManualPauseActive`` — so the close
        path checks them up front and refuses with the ids instead of
        letting the exception escape mid-transaction.
        """
        ids = await self.subtree_ids(task_id, conn=conn)
        ids = [i for i in ids if i != task_id]
        if not ids:
            return []
        stmt = select(tasks.c.id).where(
            and_(
                tasks.c.id.in_(ids),
                tasks.c.status == TaskStatus.PAUSED.value,
                tasks.c.resume_after.is_(None),
            )
        )
        return sorted(r[0] for r in (await conn.execute(stmt)).fetchall())

    async def abandon_subtree(self, task_id: str, *, conn) -> TransitionResult:
        """Close every non-terminal descendant as ``abandoned`` (spec §7).

        Administrative close: ``force=True`` on every transition, since a
        descendant may be sitting in a state (``PAUSED``, ``ASSIGNED``,
        ``WAITING_INPUT``, ...) with no ordinary edge to ``COMPLETED``.
        Accumulates ``.flipped`` / ``.settled`` across every descendant so
        the caller can run one post-commit ``log_blocked_flips`` /
        ``_notify_settled`` pass instead of dropping them.
        """
        ids = await self.subtree_ids(task_id, conn=conn)
        ids = [i for i in ids if i != task_id]
        if not ids:
            return TransitionResult()
        stmt = select(tasks.c.id, tasks.c.status).where(tasks.c.id.in_(ids))
        if conn.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        rows = (await conn.execute(stmt)).fetchall()
        terminal = (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
        # Deepest first so each container settles naturally after its children.
        depth = {tid: i for i, tid in enumerate(ids)}
        open_ids = sorted((r[0] for r in rows if r[1] not in terminal), key=lambda t: -depth[t])
        result = TransitionResult()
        for tid in open_ids:
            await self._upsert_meta(tid, "work_outcome", "abandoned", conn=conn)
            res = await self._apply_transition(
                conn,
                tid,
                TaskStatus.COMPLETED,
                context="abandoned_by_container",
                assigned_agent_id=None,
                force=True,
            )
            # An abandoned task holds nothing.  Same transaction as the
            # status write, mirroring ``_delete_one``'s release: a lock or an
            # agent pointer left behind would strand a workspace and keep an
            # agent BUSY on work nobody is doing.
            await conn.execute(
                update(workspaces)
                .where(workspaces.c.locked_by_task_id == tid)
                .values(
                    locked_by_task_id=None,
                    locked_by_agent_id=None,
                    locked_at=None,
                    lock_mode=None,
                )
            )
            await conn.execute(
                update(agents)
                .where(agents.c.current_task_id == tid)
                .values(current_task_id=None, state=AgentState.IDLE.value)
            )
            result.settled.extend(res.settled)
            result.flipped |= res.flipped
            # Abandoning a blocker unblocks its dependents; carry the
            # frontier entries up so the caller's ``_notify_ready`` wakes a
            # waiting ``task_claim`` long-poll (I2).
            result.ready.extend(res.ready)
            result.abandoned.append(tid)
        return result

    async def _upsert_meta(self, task_id: str, key: str, value, *, conn) -> None:
        """Set ``task_metadata[key] = value`` (JSON-encoded), insert-or-update."""
        encoded = json.dumps(value)
        dialect = conn.dialect.name
        ins = pg_insert if dialect == "postgresql" else sqlite_insert
        stmt = ins(task_metadata).values(task_id=task_id, key=key, value=encoded)
        stmt = stmt.on_conflict_do_update(
            index_elements=["task_id", "key"], set_={"value": encoded}
        )
        await conn.execute(stmt)

    async def _upsert_meta_many(self, task_id: str, items: dict, *, conn) -> None:
        """``_upsert_meta`` for several keys of one task in a single statement.

        The claim path writes ``claimed_by_session`` and ``work_dir``
        together; one multi-row upsert keeps that inside the spec §15
        transaction budget.  ``set_`` reads from ``excluded`` so each row
        updates with its own value.
        """
        if not items:
            return
        dialect = conn.dialect.name
        ins = pg_insert if dialect == "postgresql" else sqlite_insert
        stmt = ins(task_metadata).values(
            [
                {"task_id": task_id, "key": key, "value": json.dumps(value)}
                for key, value in items.items()
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["task_id", "key"], set_={"value": stmt.excluded.value}
        )
        await conn.execute(stmt)
