"""Layout storage queries (spatial-layout design §4.6, §4.10). Expects ``self._engine``."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable

from sqlalchemy import delete, func, insert, select, update

from src.database.tables import layout_dirty, layout_jobs, project_layout_meta


def _chunks(seq: list, size: int = 900) -> list[list]:
    """Split *seq* into consecutive slices of at most *size* items.

    Keeps ``IN (...)`` lists under SQLite's older bound-parameter cap
    (~32k; current builds allow far more) with headroom to spare.
    """
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def like_prefix(prefix: str) -> str:
    """A LIKE pattern matching everything under ``prefix``.

    Task ids are user-supplied and may contain LIKE wildcards, so ``%``,
    ``_`` and the escape character itself are escaped. Always pair with
    ``.like(..., escape="\\")`` — SQLite and PostgreSQL both honour it.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


class LayoutQueryMixin:
    # ── dirty marks ─────────────────────────────────────────────────────
    async def mark_layout_dirty(
        self, project_id: str, task_ids: Iterable[str], reason: str, *, conn
    ) -> None:
        rows = [
            {"project_id": project_id, "task_id": t, "reason": reason, "created_at": time.time()}
            for t in dict.fromkeys(task_ids)
        ]
        if rows:
            await conn.execute(insert(layout_dirty), rows)

    async def _layout_parent_ids(self, task_ids: Iterable[str], *, conn) -> list[str]:
        """Parents of *task_ids* that are not themselves in *task_ids*.

        Used by delete/archive: those paths drop the task's layout rows in
        the same transaction, so the driver cannot recover the former
        container from a stored row. Marking the surviving parent makes the
        container re-flow (closing the gap) and refreshes its ancestors'
        aggregates.
        """
        from src.database.tables import tasks

        ids = list(dict.fromkeys(task_ids))
        if not ids:
            return []
        rows = (
            await conn.execute(select(tasks.c.parent_task_id).where(tasks.c.id.in_(ids)))
        ).fetchall()
        gone = set(ids)
        return sorted({r[0] for r in rows if r[0] is not None} - gone)

    async def dirty_layout_projects(self) -> list[str]:
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(layout_dirty.c.project_id).distinct().order_by(layout_dirty.c.project_id)
            )
            return [r[0] for r in res.fetchall()]

    async def pop_layout_dirty(
        self, project_id: str, *, min_age_seconds: float, limit: int = 1000
    ) -> tuple[int, list[tuple[str, str]]]:
        """Read (not delete) the project's oldest dirty rows, newest-old-enough.

        At most *limit* rows are returned, in ``seq`` order, and the
        returned sequence number is the largest ``seq`` among **the rows
        actually returned** — so it stays a correct ``consumed_seq`` for
        ``clear_layout_dirty`` and anything past the limit survives for the
        next batch. Without the cap a project that accumulated marks while
        the feature was off would try to fold the whole backlog into one
        batch.
        """
        async with self._engine.begin() as conn:
            newest = (
                await conn.execute(
                    select(func.max(layout_dirty.c.created_at)).where(
                        layout_dirty.c.project_id == project_id
                    )
                )
            ).scalar_one_or_none()
            if newest is None or time.time() - newest < min_age_seconds:
                return 0, []
            res = await conn.execute(
                select(layout_dirty.c.seq, layout_dirty.c.task_id, layout_dirty.c.reason)
                .where(layout_dirty.c.project_id == project_id)
                .order_by(layout_dirty.c.seq)
                .limit(limit)
            )
            rows = res.fetchall()
        return (max(r[0] for r in rows), [(r[1], r[2]) for r in rows]) if rows else (0, [])

    async def trim_layout_dirty(self, *, older_than: float | None = None) -> int:
        """Discard dirty marks across every project; return the row count.

        ``older_than`` is an absolute wall-clock timestamp; ``None`` (the
        default) deletes everything. Used by the orchestrator step while the
        layout feature is disabled, where marks would otherwise accumulate
        forever with no consumer — enabling the feature later starts from a
        full layout, so discarded marks are harmless.
        """
        async with self._engine.begin() as conn:
            stmt = delete(layout_dirty)
            if older_than is not None:
                stmt = stmt.where(layout_dirty.c.created_at < older_than)
            return (await conn.execute(stmt)).rowcount or 0

    # ── FK-holder cleanup ───────────────────────────────────────────────
    async def delete_layout_rows_for_tasks(self, task_ids: Iterable[str], *, conn) -> None:
        """Drop every layout row for *task_ids*, on the caller's connection.

        ``task_layouts.task_id`` is a plain FK onto ``tasks`` (no cascade)
        and FK enforcement is on for both dialects, so these rows must go
        in the same transaction and *before* the task row leaves ``tasks``
        — whether it is being deleted or moved into ``archived_tasks``.
        The cells table is keyed by the same task id and is cleaned with it.
        """
        from src.database.tables import task_layout_cells as cells, task_layouts

        ids = list(dict.fromkeys(task_ids))
        if not ids:
            return
        await conn.execute(delete(task_layouts).where(task_layouts.c.task_id.in_(ids)))
        await conn.execute(delete(cells).where(cells.c.task_id.in_(ids)))

    async def delete_layout_rows_for_project(self, project_id: str, *, conn) -> None:
        """Drop all layout state for a project, on the caller's connection.

        Covers the two FK holders on ``projects`` (``task_layouts``,
        ``project_layout_meta``) plus the project-scoped bookkeeping
        tables, so ``delete_project`` leaves nothing behind.
        """
        from src.database.tables import task_layout_cells as cells, task_layouts

        for table, col in (
            (task_layouts, task_layouts.c.project_id),
            (cells, cells.c.project_id),
            (project_layout_meta, project_layout_meta.c.project_id),
            (layout_dirty, layout_dirty.c.project_id),
            (layout_jobs, layout_jobs.c.project_id),
        ):
            await conn.execute(delete(table).where(col == project_id))

    async def clear_layout_dirty(self, project_id: str, up_to_seq: int, *, conn) -> None:
        await conn.execute(
            delete(layout_dirty).where(
                layout_dirty.c.project_id == project_id, layout_dirty.c.seq <= up_to_seq
            )
        )

    # ── meta ────────────────────────────────────────────────────────────
    async def get_layout_meta(self, project_id: str, variant: str) -> dict | None:
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(project_layout_meta).where(
                            project_layout_meta.c.project_id == project_id,
                            project_layout_meta.c.variant == variant,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    # ── jobs ────────────────────────────────────────────────────────────
    async def enqueue_layout_job(self, project_id: str, variant: str, kind: str) -> dict:
        async with self._engine.begin() as conn:
            existing = (
                (
                    await conn.execute(
                        select(layout_jobs).where(
                            layout_jobs.c.project_id == project_id,
                            layout_jobs.c.variant == variant,
                            layout_jobs.c.status.in_(("queued", "running")),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                return dict(existing)
            row = {
                "id": uuid.uuid4().hex,
                "project_id": project_id,
                "variant": variant,
                "kind": kind,
                "status": "queued",
                "requested_at": time.time(),
            }
            await conn.execute(insert(layout_jobs).values(**row))
            return row

    async def next_layout_job(self) -> dict | None:
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(layout_jobs)
                        .where(layout_jobs.c.status == "queued")
                        .order_by(layout_jobs.c.requested_at)
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            result = await conn.execute(
                update(layout_jobs)
                .where(layout_jobs.c.id == row["id"], layout_jobs.c.status == "queued")
                .values(status="running", started_at=time.time())
            )
            if result.rowcount != 1:
                # Another caller claimed this job between our SELECT and
                # UPDATE (or flipped it out of 'queued' entirely) — lose
                # the race cleanly rather than double-claim it.
                return None
            return {**dict(row), "status": "running"}

    async def finish_layout_job(self, job_id: str, *, error: str | None) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(layout_jobs)
                .where(layout_jobs.c.id == job_id)
                .values(status="failed" if error else "done", finished_at=time.time(), error=error)
            )

    async def get_layout_job(self, job_id: str) -> dict | None:
        async with self._engine.begin() as conn:
            row = (
                (await conn.execute(select(layout_jobs).where(layout_jobs.c.id == job_id)))
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def list_layout_jobs(self, project_id: str, variant: str, *, statuses) -> list[dict]:
        """Jobs for one (project, variant) in the given statuses, oldest first."""
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(layout_jobs)
                .where(
                    layout_jobs.c.project_id == project_id,
                    layout_jobs.c.variant == variant,
                    layout_jobs.c.status.in_(list(statuses)),
                )
                .order_by(layout_jobs.c.requested_at)
            )
            return [dict(m) for m in res.mappings()]

    # ── snapshot & rows ──────────────────────────────────────────────────
    async def load_project_snapshot(self, project_id: str):
        from src.database.queries.hierarchy_queries import CONTAINER_KEY, CONTAINER_VALUE
        from src.database.tables import task_dependencies, task_metadata, tasks
        from src.task_graph.layout.model import SnapTask

        async with self._engine.begin() as conn:
            trows = (
                await conn.execute(
                    select(
                        tasks.c.id,
                        tasks.c.parent_task_id,
                        tasks.c.status,
                        tasks.c.created_at,
                        tasks.c.title,
                    ).where(tasks.c.project_id == project_id)
                )
            ).fetchall()
            ids = [r[0] for r in trows]
            containers = set()
            if ids:
                containers = {
                    r[0]
                    for r in (
                        await conn.execute(
                            select(task_metadata.c.task_id).where(
                                task_metadata.c.task_id.in_(ids),
                                task_metadata.c.key == CONTAINER_KEY,
                                task_metadata.c.value == CONTAINER_VALUE,
                            )
                        )
                    ).fetchall()
                }
            edges = []
            if ids:
                edges = [
                    (r[0], r[1], r[2])
                    for r in (
                        await conn.execute(
                            select(
                                task_dependencies.c.task_id,
                                task_dependencies.c.depends_on_task_id,
                                task_dependencies.c.dep_type,
                            ).where(task_dependencies.c.task_id.in_(ids))
                        )
                    ).fetchall()
                ]
        snap = {
            r[0]: SnapTask(
                id=r[0],
                parent_id=r[1],
                is_container=r[0] in containers,
                status=r[2],
                created_at=r[3],
                title=r[4] or "",
            )
            for r in trows
        }
        return snap, edges

    @staticmethod
    def _row_from_mapping(m):
        from src.task_graph.layout.model import LayoutRow

        return LayoutRow(
            task_id=m["task_id"],
            container_id=m["container_id"],
            path=m["path"],
            depth=m["depth"],
            rank=m["rank"],
            order_key=m["order_key"],
            w=m["w"],
            h=m["h"],
            rel_x=m["rel_x"],
            rel_y=m["rel_y"],
            abs_x=m["abs_x"],
            abs_y=m["abs_y"],
            kind=m["kind"],
            agg_children=m["agg_children"],
            agg_descendants=m["agg_descendants"],
            agg_completed=m["agg_completed"],
            agg_running=m["agg_running"],
            agg_blocked=m["agg_blocked"],
            agg_active=m["agg_active"],
        )

    async def load_layout_rows(self, project_id, variant, task_ids):
        from src.database.tables import task_layouts

        ids = list(task_ids)
        if not ids:
            return {}
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts).where(
                    task_layouts.c.project_id == project_id,
                    task_layouts.c.variant == variant,
                    task_layouts.c.task_id.in_(ids),
                )
            )
            return {m["task_id"]: self._row_from_mapping(m) for m in res.mappings()}

    async def load_children_layout_rows(self, project_id, variant, container_id):
        from src.database.tables import task_layouts

        cond = (
            (task_layouts.c.container_id == container_id)
            if container_id is not None
            else task_layouts.c.container_id.is_(None)
        )
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts).where(
                    task_layouts.c.project_id == project_id,
                    task_layouts.c.variant == variant,
                    cond,
                )
            )
            return {m["task_id"]: self._row_from_mapping(m) for m in res.mappings()}

    async def load_subtree_rows(self, project_id, variant) -> dict:
        """Every row stored for *variant*, keyed by task id (across the whole project)."""
        from src.database.tables import task_layouts

        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts).where(
                    task_layouts.c.project_id == project_id,
                    task_layouts.c.variant == variant,
                )
            )
            return {m["task_id"]: self._row_from_mapping(m) for m in res.mappings()}

    async def load_subtree_ids(self, project_id, variant, path_prefix) -> list[str]:
        """Task ids of the row at ``path_prefix`` and every row beneath it."""
        from src.database.tables import task_layouts

        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts.c.task_id).where(
                    task_layouts.c.project_id == project_id,
                    task_layouts.c.variant == variant,
                    task_layouts.c.path.like(like_prefix(path_prefix), escape="\\"),
                )
            )
            return [r[0] for r in res.fetchall()]

    async def load_cells(self, project_id, variant, task_ids) -> dict[str, list[tuple[int, int]]]:
        from src.database.tables import task_layout_cells as cells

        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(cells.c.task_id, cells.c.cell_x, cells.c.cell_y)
                .where(
                    cells.c.project_id == project_id,
                    cells.c.variant == variant,
                    cells.c.task_id.in_(list(task_ids)),
                )
                .order_by(cells.c.task_id, cells.c.cell_x, cells.c.cell_y)
            )
            out: dict[str, list[tuple[int, int]]] = {}
            for t, x, y in res.fetchall():
                out.setdefault(t, []).append((x, y))
            return out

    async def subtree_aggregates(self, project_id, path_prefix) -> dict:
        """Counts over the subtree at *path_prefix*, from the ``all`` variant.

        Deliberately variant-independent: aggregates describe the real task
        tree, so they are always read from ``all`` (the ``active`` variant
        hides finished tasks and stubs whole subtrees away, which would make
        the counts wrong).
        """
        from src.database.tables import task_layouts, tasks
        from src.task_graph.layout.constants import FINISHED_STATUSES, RUNNING_STATUSES

        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(tasks.c.status, tasks.c.is_blocked, task_layouts.c.path)
                .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                .where(
                    task_layouts.c.project_id == project_id,
                    task_layouts.c.variant == "all",
                    task_layouts.c.path.like(like_prefix(path_prefix), escape="\\"),
                    task_layouts.c.path != path_prefix,
                )
            )
            rows = res.fetchall()
        depth = path_prefix.count("/")
        return {
            "children": sum(1 for r in rows if r[2].count("/") == depth + 1),
            "descendants": len(rows),
            "completed": sum(1 for r in rows if r[0] in FINISHED_STATUSES),
            "running": sum(1 for r in rows if r[0] in RUNNING_STATUSES),
            "blocked": sum(1 for r in rows if r[1]),
            "active": sum(1 for r in rows if r[0] not in FINISHED_STATUSES),
        }

    # ── publish ─────────────────────────────────────────────────────────
    async def publish_layout(self, project_id, variant, write_set, *, consumed_seq, extent) -> int:
        """Apply upserts/deletes/translations for one layout variant atomically.

        The node count is always recomputed with a single ``COUNT(*)``
        inside the same transaction, so it stays correct regardless of what
        the caller did.

        A translation applies to DESCENDANTS only
        (``path LIKE prefix || '%' AND path != prefix``): the container's
        own row is upserted separately by whatever engine pass moved it, so
        including it here would shift it twice.
        """
        from sqlalchemy.dialects import postgresql, sqlite
        from src.database.tables import task_layout_cells as cells, task_layouts
        from src.task_graph.layout.flow import cells_for_box

        dialect = self._engine.dialect.name

        def _meta_query():
            q = select(project_layout_meta).where(
                project_layout_meta.c.project_id == project_id,
                project_layout_meta.c.variant == variant,
            )
            return q.with_for_update() if dialect == "postgresql" else q

        async with self._engine.begin() as conn:
            meta = (await conn.execute(_meta_query())).mappings().first()
            if meta is None:
                # Seed a placeholder row first so a concurrent first-publish
                # for the same (project, variant) loses the INSERT race
                # instead of raising IntegrityError — then re-select (with
                # FOR UPDATE on PostgreSQL) so both callers converge on one
                # row and one UPDATE-based version bump below.
                now0 = time.time()
                seed_ins = (postgresql.insert if dialect == "postgresql" else sqlite.insert)(
                    project_layout_meta
                ).values(
                    project_id=project_id,
                    variant=variant,
                    layout_version=0,
                    extent_w=0,
                    extent_h=0,
                    node_count=0,
                    updated_at=now0,
                    reconciled_at=now0,
                )
                await conn.execute(
                    seed_ins.on_conflict_do_nothing(index_elements=["project_id", "variant"])
                )
                meta = (await conn.execute(_meta_query())).mappings().first()

            # deletes
            if write_set.deletes:
                await conn.execute(
                    delete(task_layouts).where(
                        task_layouts.c.project_id == project_id,
                        task_layouts.c.variant == variant,
                        task_layouts.c.task_id.in_(write_set.deletes),
                    )
                )
                await conn.execute(
                    delete(cells).where(
                        cells.c.project_id == project_id,
                        cells.c.variant == variant,
                        cells.c.task_id.in_(write_set.deletes),
                    )
                )

            # upserts — one INSERT ... ON CONFLICT statement executed via
            # ``conn.execute(stmt, rows_vals)`` (a plain DBAPI executemany,
            # not the INSERT...RETURNING "insertmanyvalues" path) instead of
            # a per-row round trip: an incremental batch touching dozens (or
            # hundreds, on a root reflow) of rows was paying one round trip
            # AND one from-scratch N-row VALUES compilation per publish.
            # Keyed by task id, NOT a list: a node can be both upserted (at
            # its pre-translation absolute position) and picked up by the
            # post-upsert re-SELECT of a translation covering it. Two
            # positions for one id would insert two sets of cells — ghost
            # cells at the stale box. The translation pass runs after the
            # upserts, so its entry overwrites and the last write wins.
            touched: dict[str, tuple[float, float, float, float]] = {}
            if write_set.upserts:
                rows_vals = [
                    {
                        "project_id": project_id,
                        "variant": variant,
                        "task_id": r.task_id,
                        "container_id": r.container_id,
                        "path": r.path,
                        "depth": r.depth,
                        "rank": r.rank,
                        "order_key": r.order_key,
                        "w": r.w,
                        "h": r.h,
                        "rel_x": r.rel_x,
                        "rel_y": r.rel_y,
                        "abs_x": r.abs_x,
                        "abs_y": r.abs_y,
                        "kind": r.kind,
                        "agg_children": r.agg_children,
                        "agg_descendants": r.agg_descendants,
                        "agg_completed": r.agg_completed,
                        "agg_running": r.agg_running,
                        "agg_blocked": r.agg_blocked,
                        "agg_active": r.agg_active,
                    }
                    for r in write_set.upserts
                ]
                update_cols = [
                    k for k in rows_vals[0] if k not in ("project_id", "variant", "task_id")
                ]
                ins = (postgresql.insert if dialect == "postgresql" else sqlite.insert)(
                    task_layouts
                )
                upd = {k: ins.excluded[k] for k in update_cols}
                stmt = ins.on_conflict_do_update(
                    index_elements=["project_id", "variant", "task_id"], set_=upd
                )
                await conn.execute(stmt, rows_vals)
                for r in write_set.upserts:
                    touched[r.task_id] = (r.abs_x, r.abs_y, r.w, r.h)

            # translations — descendants only; the container's own row is
            # upserted separately by the pass that moved it (controller
            # ruling: including it here would shift it twice).
            for t in write_set.translations:
                await conn.execute(
                    update(task_layouts)
                    .where(
                        task_layouts.c.project_id == project_id,
                        task_layouts.c.variant == variant,
                        task_layouts.c.path.like(like_prefix(t.path_prefix), escape="\\"),
                        task_layouts.c.path != t.path_prefix,
                    )
                    .values(abs_x=task_layouts.c.abs_x + t.dx, abs_y=task_layouts.c.abs_y + t.dy)
                )
                moved = await conn.execute(
                    select(
                        task_layouts.c.task_id,
                        task_layouts.c.abs_x,
                        task_layouts.c.abs_y,
                        task_layouts.c.w,
                        task_layouts.c.h,
                    ).where(
                        task_layouts.c.project_id == project_id,
                        task_layouts.c.variant == variant,
                        task_layouts.c.path.like(like_prefix(t.path_prefix), escape="\\"),
                        task_layouts.c.path != t.path_prefix,
                    )
                )
                for tid, ax, ay, tw, th in moved.fetchall():
                    touched[tid] = (ax, ay, tw, th)

            # cells for every touched row
            if touched:
                await conn.execute(
                    delete(cells).where(
                        cells.c.project_id == project_id,
                        cells.c.variant == variant,
                        cells.c.task_id.in_(list(touched)),
                    )
                )
                crow = [
                    {
                        "project_id": project_id,
                        "variant": variant,
                        "cell_x": cx,
                        "cell_y": cy,
                        "task_id": tid,
                    }
                    for tid, (bx, by, bw, bh) in touched.items()
                    for cx, cy in cells_for_box(bx, by, bw, bh)
                ]
                if crow:
                    await conn.execute(insert(cells), crow)

            # meta
            count = (
                await conn.execute(
                    select(func.count())
                    .select_from(task_layouts)
                    .where(
                        task_layouts.c.project_id == project_id, task_layouts.c.variant == variant
                    )
                )
            ).scalar_one()
            version = meta["layout_version"] + 1
            now = time.time()
            # `meta` is guaranteed present here (existing row, or the seed
            # row inserted above) — one UPDATE-based bump path, always.
            await conn.execute(
                update(project_layout_meta)
                .where(
                    project_layout_meta.c.project_id == project_id,
                    project_layout_meta.c.variant == variant,
                )
                .values(
                    layout_version=version,
                    extent_w=extent[0],
                    extent_h=extent[1],
                    node_count=count,
                    updated_at=now,
                )
            )

            if consumed_seq is not None:
                await self.clear_layout_dirty(project_id, consumed_seq, conn=conn)
        return version

    # ── bulk view queries ──────────────────────────────────────────────
    # `playbook_run_id` is not a real `tasks` column — like
    # `src/api/graph.py`'s GraphTaskNode construction, it is derived from
    # `dedup_key` (the "playbook-run:<id>" convention) in
    # `_task_dict_from_mapping` below, so `dedup_key` is fetched in its
    # place and swapped out before the row reaches callers.
    _TASK_FIELDS = (
        "id",
        "title",
        "status",
        "priority",
        "is_blocked",
        "profile_id",
        "intelligence_class",
        "assigned_agent_id",
        "branch_name",
        "pr_url",
        "dedup_key",
    )

    @classmethod
    def _task_dict_from_mapping(cls, m):
        task = {f: m[f] for f in cls._TASK_FIELDS if f != "dedup_key"}
        task["is_blocked"] = bool(task["is_blocked"])
        dedup_key = m["dedup_key"]
        task["playbook_run_id"] = (
            dedup_key.removeprefix("playbook-run:")
            if dedup_key and dedup_key.startswith("playbook-run:")
            else None
        )
        return task

    async def load_rows_in_cells(self, project_id, variant, cells_wanted):
        from sqlalchemy import and_, or_
        from src.database.tables import task_layout_cells as cells, task_layouts

        if not cells_wanted:
            return {}
        cond = or_(*[and_(cells.c.cell_x == cx, cells.c.cell_y == cy) for cx, cy in cells_wanted])
        async with self._engine.begin() as conn:
            ids = [
                r[0]
                for r in (
                    await conn.execute(
                        select(cells.c.task_id)
                        .distinct()
                        .where(
                            cells.c.project_id == project_id,
                            cells.c.variant == variant,
                            cond,
                        )
                    )
                ).fetchall()
            ]
            if not ids:
                return {}
            out: dict = {}
            for chunk in _chunks(ids):
                res = await conn.execute(
                    select(task_layouts).where(
                        task_layouts.c.project_id == project_id,
                        task_layouts.c.variant == variant,
                        task_layouts.c.task_id.in_(chunk),
                    )
                )
                out.update({m["task_id"]: self._row_from_mapping(m) for m in res.mappings()})
            return out

    async def load_rows_with_tasks(self, project_id, variant, task_ids):
        from src.database.tables import task_layouts, tasks

        ids = list(task_ids)
        if not ids:
            return {}
        cols = [getattr(tasks.c, f) for f in self._TASK_FIELDS]
        async with self._engine.begin() as conn:
            out: dict = {}
            for chunk in _chunks(ids):
                res = await conn.execute(
                    select(task_layouts, *cols)
                    .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                    .where(
                        task_layouts.c.project_id == project_id,
                        task_layouts.c.variant == variant,
                        task_layouts.c.task_id.in_(chunk),
                    )
                )
                for m in res.mappings():
                    out[m["task_id"]] = (self._row_from_mapping(m), self._task_dict_from_mapping(m))
            return out

    async def load_all_rows_with_tasks(self, project_id, variant):
        from src.database.tables import task_layouts, tasks

        cols = [getattr(tasks.c, f) for f in self._TASK_FIELDS]
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts, *cols)
                .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                .where(
                    task_layouts.c.project_id == project_id, task_layouts.c.variant == variant
                )
            )
            out = {}
            for m in res.mappings():
                out[m["task_id"]] = (self._row_from_mapping(m), self._task_dict_from_mapping(m))
            return out

    async def load_rows_by_prefixes(self, project_id, variant, prefixes):
        from sqlalchemy import or_
        from src.database.tables import task_layouts

        if not prefixes:
            return {}
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts).where(
                    task_layouts.c.project_id == project_id,
                    task_layouts.c.variant == variant,
                    or_(
                        *[
                            task_layouts.c.path.like(like_prefix(p), escape="\\")
                            for p in prefixes
                        ]
                    ),
                )
            )
            return {m["task_id"]: self._row_from_mapping(m) for m in res.mappings()}

    async def load_edges_touching(self, task_ids):
        from sqlalchemy import or_
        from src.database.tables import task_dependencies as td

        ids = list(task_ids)
        if not ids:
            return []
        # Chunking splits `ids` across separate IN-lists, so an edge whose
        # two endpoints land in different chunks would otherwise be
        # selected twice (once per chunk it matches) -- dedupe via a dict
        # keyed by the row itself before sorting back into the documented
        # (task_id, depends_on, dep_type) order.
        seen: dict = {}
        async with self._engine.begin() as conn:
            for chunk in _chunks(ids):
                res = await conn.execute(
                    select(
                        td.c.task_id, td.c.depends_on_task_id, td.c.dep_type, td.c.description
                    ).where(or_(td.c.task_id.in_(chunk), td.c.depends_on_task_id.in_(chunk)))
                )
                for r in res.fetchall():
                    seen[tuple(r)] = None
        return sorted(seen, key=lambda r: (r[0], r[1], r[2]))

    async def load_matching_ids(self, project_id, variant, *, q, status):
        from sqlalchemy import func, or_
        from src.database.tables import task_layouts, tasks

        conds = [task_layouts.c.project_id == project_id, task_layouts.c.variant == variant]
        if q:
            needle = f"%{q.lower()}%"
            conds.append(
                or_(
                    func.lower(tasks.c.title).like(needle),
                    func.lower(tasks.c.id).like(needle),
                )
            )
        if status:
            conds.append(tasks.c.status == status)
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts.c.task_id)
                .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                .where(*conds)
            )
            return {r[0] for r in res.fetchall()}
