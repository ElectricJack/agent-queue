"""Layout storage queries (spatial-layout design §4.6, §4.10). Expects ``self._engine``."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable

from sqlalchemy import delete, func, insert, select, update

from src.database.tables import layout_dirty, layout_jobs, project_layout_meta


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

    async def dirty_layout_projects(self) -> list[str]:
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(layout_dirty.c.project_id).distinct().order_by(layout_dirty.c.project_id)
            )
            return [r[0] for r in res.fetchall()]

    async def pop_layout_dirty(
        self, project_id: str, *, min_age_seconds: float
    ) -> tuple[int, list[tuple[str, str]]]:
        """Read (not delete) the project's dirty rows if the newest is old enough."""
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
            )
            rows = res.fetchall()
        return (max(r[0] for r in rows), [(r[1], r[2]) for r in rows]) if rows else (0, [])

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
                await conn.execute(
                    select(project_layout_meta).where(
                        project_layout_meta.c.project_id == project_id,
                        project_layout_meta.c.variant == variant,
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    # ── jobs ────────────────────────────────────────────────────────────
    async def enqueue_layout_job(self, project_id: str, variant: str, kind: str) -> dict:
        async with self._engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(layout_jobs).where(
                        layout_jobs.c.project_id == project_id,
                        layout_jobs.c.variant == variant,
                        layout_jobs.c.status.in_(("queued", "running")),
                    )
                )
            ).mappings().first()
            if existing:
                return dict(existing)
            row = {
                "id": uuid.uuid4().hex, "project_id": project_id, "variant": variant,
                "kind": kind, "status": "queued", "requested_at": time.time(),
            }
            await conn.execute(insert(layout_jobs).values(**row))
            return row

    async def next_layout_job(self) -> dict | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(layout_jobs)
                    .where(layout_jobs.c.status == "queued")
                    .order_by(layout_jobs.c.requested_at)
                    .limit(1)
                )
            ).mappings().first()
            if not row:
                return None
            await conn.execute(
                update(layout_jobs)
                .where(layout_jobs.c.id == row["id"], layout_jobs.c.status == "queued")
                .values(status="running", started_at=time.time())
            )
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
                await conn.execute(select(layout_jobs).where(layout_jobs.c.id == job_id))
            ).mappings().first()
        return dict(row) if row else None

    # ── snapshot & rows ──────────────────────────────────────────────────
    async def load_project_snapshot(self, project_id: str):
        from src.database.queries.hierarchy_queries import CONTAINER_KEY, CONTAINER_VALUE
        from src.database.tables import task_dependencies, task_metadata, tasks
        from src.task_graph.layout.model import SnapTask

        async with self._engine.begin() as conn:
            trows = (
                await conn.execute(
                    select(tasks.c.id, tasks.c.parent_task_id, tasks.c.status,
                           tasks.c.created_at, tasks.c.title)
                    .where(tasks.c.project_id == project_id)
                )
            ).fetchall()
            ids = [r[0] for r in trows]
            containers = set()
            if ids:
                containers = {
                    r[0] for r in (
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
                    (r[0], r[1], r[2]) for r in (
                        await conn.execute(
                            select(task_dependencies.c.task_id,
                                   task_dependencies.c.depends_on_task_id,
                                   task_dependencies.c.dep_type)
                            .where(task_dependencies.c.task_id.in_(ids))
                        )
                    ).fetchall()
                ]
        snap = {
            r[0]: SnapTask(id=r[0], parent_id=r[1], is_container=r[0] in containers,
                           status=r[2], created_at=r[3], title=r[4] or "")
            for r in trows
        }
        return snap, edges

    @staticmethod
    def _row_from_mapping(m):
        from src.task_graph.layout.model import LayoutRow
        return LayoutRow(
            task_id=m["task_id"], container_id=m["container_id"], path=m["path"],
            depth=m["depth"], rank=m["rank"], order_key=m["order_key"], w=m["w"], h=m["h"],
            rel_x=m["rel_x"], rel_y=m["rel_y"], abs_x=m["abs_x"], abs_y=m["abs_y"],
            kind=m["kind"], agg_children=m["agg_children"], agg_descendants=m["agg_descendants"],
            agg_completed=m["agg_completed"], agg_running=m["agg_running"],
            agg_blocked=m["agg_blocked"], agg_active=m["agg_active"],
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
        cond = (task_layouts.c.container_id == container_id) if container_id is not None \
            else task_layouts.c.container_id.is_(None)
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts).where(
                    task_layouts.c.project_id == project_id,
                    task_layouts.c.variant == variant, cond,
                )
            )
            return {m["task_id"]: self._row_from_mapping(m) for m in res.mappings()}

    async def load_cells(self, project_id, variant, task_ids) -> dict[str, list[tuple[int, int]]]:
        from src.database.tables import task_layout_cells as cells
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(cells.c.task_id, cells.c.cell_x, cells.c.cell_y)
                .where(cells.c.project_id == project_id, cells.c.variant == variant,
                       cells.c.task_id.in_(list(task_ids)))
                .order_by(cells.c.task_id, cells.c.cell_x, cells.c.cell_y)
            )
            out: dict[str, list[tuple[int, int]]] = {}
            for t, x, y in res.fetchall():
                out.setdefault(t, []).append((x, y))
            return out

    async def subtree_aggregates(self, project_id, variant, path_prefix) -> dict:
        from src.database.tables import task_layouts, tasks
        from src.task_graph.layout.constants import FINISHED_STATUSES, RUNNING_STATUSES
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(tasks.c.status, tasks.c.is_blocked, task_layouts.c.path)
                .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                .where(task_layouts.c.project_id == project_id,
                       task_layouts.c.variant == "all",
                       task_layouts.c.path.like(path_prefix + "%"),
                       task_layouts.c.path != path_prefix)
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
    async def publish_layout(self, project_id, variant, write_set, *, consumed_seq,
                             extent, node_count_delta) -> int:
        """Apply upserts/deletes/translations for one layout variant atomically.

        ``node_count_delta`` is accepted for interface stability but the
        node count is always recomputed with a single ``COUNT(*)`` inside
        the same transaction, so it stays correct regardless of caller math.

        A translation applies to DESCENDANTS only
        (``path LIKE prefix || '%' AND path != prefix``): the container's
        own row is upserted separately by whatever engine pass moved it, so
        including it here would shift it twice.
        """
        from sqlalchemy.dialects import postgresql, sqlite
        from src.database.tables import task_layout_cells as cells, task_layouts
        from src.task_graph.layout.flow import cells_for_box

        dialect = self._engine.dialect.name
        async with self._engine.begin() as conn:
            meta_q = select(project_layout_meta).where(
                project_layout_meta.c.project_id == project_id,
                project_layout_meta.c.variant == variant,
            )
            if dialect == "postgresql":
                meta_q = meta_q.with_for_update()
            meta = (await conn.execute(meta_q)).mappings().first()

            # deletes
            if write_set.deletes:
                await conn.execute(delete(task_layouts).where(
                    task_layouts.c.project_id == project_id, task_layouts.c.variant == variant,
                    task_layouts.c.task_id.in_(write_set.deletes)))
                await conn.execute(delete(cells).where(
                    cells.c.project_id == project_id, cells.c.variant == variant,
                    cells.c.task_id.in_(write_set.deletes)))

            # upserts
            touched: list[tuple[str, float, float, float, float]] = []
            for r in write_set.upserts:
                vals = {
                    "project_id": project_id, "variant": variant, "task_id": r.task_id,
                    "container_id": r.container_id, "path": r.path, "depth": r.depth,
                    "rank": r.rank, "order_key": r.order_key, "w": r.w, "h": r.h,
                    "rel_x": r.rel_x, "rel_y": r.rel_y, "abs_x": r.abs_x, "abs_y": r.abs_y,
                    "kind": r.kind, "agg_children": r.agg_children,
                    "agg_descendants": r.agg_descendants, "agg_completed": r.agg_completed,
                    "agg_running": r.agg_running, "agg_blocked": r.agg_blocked,
                    "agg_active": r.agg_active,
                }
                ins = (postgresql.insert if dialect == "postgresql" else sqlite.insert)(task_layouts).values(**vals)
                upd = {k: v for k, v in vals.items() if k not in ("project_id", "variant", "task_id")}
                await conn.execute(ins.on_conflict_do_update(
                    index_elements=["project_id", "variant", "task_id"], set_=upd))
                touched.append((r.task_id, r.abs_x, r.abs_y, r.w, r.h))

            # translations — descendants only; the container's own row is
            # upserted separately by the pass that moved it (controller
            # ruling: including it here would shift it twice).
            for t in write_set.translations:
                await conn.execute(
                    update(task_layouts)
                    .where(task_layouts.c.project_id == project_id,
                           task_layouts.c.variant == variant,
                           task_layouts.c.path.like(t.path_prefix + "%"),
                           task_layouts.c.path != t.path_prefix)
                    .values(abs_x=task_layouts.c.abs_x + t.dx, abs_y=task_layouts.c.abs_y + t.dy)
                )
                moved = await conn.execute(
                    select(task_layouts.c.task_id, task_layouts.c.abs_x, task_layouts.c.abs_y,
                           task_layouts.c.w, task_layouts.c.h)
                    .where(task_layouts.c.project_id == project_id,
                           task_layouts.c.variant == variant,
                           task_layouts.c.path.like(t.path_prefix + "%"),
                           task_layouts.c.path != t.path_prefix)
                )
                touched.extend(tuple(m) for m in moved.fetchall())

            # cells for every touched row
            if touched:
                ids = [t[0] for t in touched]
                await conn.execute(delete(cells).where(
                    cells.c.project_id == project_id, cells.c.variant == variant,
                    cells.c.task_id.in_(ids)))
                crow = []
                for tid, x, y, w, h in touched:
                    for cx, cy in cells_for_box(x, y, w, h):
                        crow.append({"project_id": project_id, "variant": variant,
                                     "cell_x": cx, "cell_y": cy, "task_id": tid})
                # a task may appear twice in `touched` (upsert + translation); dedupe
                seen = set()
                crow = [c for c in crow if (c["task_id"], c["cell_x"], c["cell_y"]) not in seen
                        and not seen.add((c["task_id"], c["cell_x"], c["cell_y"]))]
                if crow:
                    await conn.execute(insert(cells), crow)

            # meta
            count = (await conn.execute(
                select(func.count()).select_from(task_layouts).where(
                    task_layouts.c.project_id == project_id, task_layouts.c.variant == variant)
            )).scalar_one()
            version = (meta["layout_version"] if meta else 0) + 1
            now = time.time()
            if meta:
                await conn.execute(update(project_layout_meta).where(
                    project_layout_meta.c.project_id == project_id,
                    project_layout_meta.c.variant == variant,
                ).values(layout_version=version, extent_w=extent[0], extent_h=extent[1],
                         node_count=count, updated_at=now))
            else:
                await conn.execute(insert(project_layout_meta).values(
                    project_id=project_id, variant=variant, layout_version=version,
                    extent_w=extent[0], extent_h=extent[1], node_count=count,
                    updated_at=now, reconciled_at=now))

            if consumed_seq is not None:
                await self.clear_layout_dirty(project_id, consumed_seq, conn=conn)
        return version
