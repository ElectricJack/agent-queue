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
