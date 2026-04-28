"""Token ledger operations."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, insert, select

from src.database.tables import projects, tasks, token_ledger


class TokenQueryMixin:
    """Query mixin for token ledger operations.  Expects ``self._engine``."""

    async def record_token_usage(
        self,
        project_id: str,
        agent_id: str,
        task_id: str,
        tokens: int,
    ) -> None:
        """Append a token usage record."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(token_ledger).values(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    tokens_used=tokens,
                    timestamp=time.time(),
                )
            )

    async def get_project_token_usage(
        self,
        project_id: str,
        since: float | None = None,
    ) -> int:
        """Return total tokens consumed by a project, optionally since a timestamp."""
        stmt = select(func.coalesce(func.sum(token_ledger.c.tokens_used), 0).label("total")).where(
            token_ledger.c.project_id == project_id
        )
        if since:
            stmt = stmt.where(token_ledger.c.timestamp >= since)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            row = result.fetchone()
            return row[0]

    async def get_token_breakdown(
        self,
        *,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        """Aggregate token_ledger rows for the most useful breakdowns.

        Selects one of three modes by argument:
          * ``task_id`` set     → group by ``agent_id`` (with entry count)
          * ``project_id`` set  → group by ``(task_id, agent_id)``
          * neither             → group by ``project_id``

        Returns ``{"breakdown": [...], "total": int}`` so the caller (the
        ``get_token_usage`` command) can layer on its scope keys.
        """
        if task_id:
            stmt = (
                select(
                    token_ledger.c.agent_id,
                    func.coalesce(func.sum(token_ledger.c.tokens_used), 0).label("total"),
                    func.count().label("entries"),
                )
                .where(token_ledger.c.task_id == task_id)
                .group_by(token_ledger.c.agent_id)
            )
            async with self._engine.begin() as conn:
                rows = (await conn.execute(stmt)).fetchall()
            breakdown = [
                {"agent_id": r.agent_id, "tokens": r.total, "entries": r.entries} for r in rows
            ]
            return {"breakdown": breakdown, "total": sum(r["tokens"] for r in breakdown)}

        if project_id:
            stmt = (
                select(
                    token_ledger.c.task_id,
                    token_ledger.c.agent_id,
                    func.coalesce(func.sum(token_ledger.c.tokens_used), 0).label("total"),
                )
                .where(token_ledger.c.project_id == project_id)
                .group_by(token_ledger.c.task_id, token_ledger.c.agent_id)
                .order_by(func.sum(token_ledger.c.tokens_used).desc())
            )
            async with self._engine.begin() as conn:
                rows = (await conn.execute(stmt)).fetchall()
            breakdown = [
                {"task_id": r.task_id, "agent_id": r.agent_id, "tokens": r.total} for r in rows
            ]
            return {"breakdown": breakdown, "total": sum(r["tokens"] for r in breakdown)}

        stmt = (
            select(
                token_ledger.c.project_id,
                func.coalesce(func.sum(token_ledger.c.tokens_used), 0).label("total"),
            )
            .group_by(token_ledger.c.project_id)
            .order_by(func.sum(token_ledger.c.tokens_used).desc())
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        breakdown = [{"project_id": r.project_id, "tokens": r.total} for r in rows]
        return {"breakdown": breakdown, "total": sum(r["tokens"] for r in breakdown)}

    async def get_token_audit(
        self,
        days: int = 7,
        project_id: str | None = None,
    ) -> dict:
        """Return a comprehensive token audit for a time range.

        Returns a dict with keys: total, since, until, by_project, top_tasks, daily.
        """
        now = time.time()
        since = now - (days * 86400)

        base = token_ledger.c.timestamp >= since
        if project_id:
            base = (token_ledger.c.timestamp >= since) & (token_ledger.c.project_id == project_id)

        async with self._engine.begin() as conn:
            # -- Grand total --
            stmt = select(func.coalesce(func.sum(token_ledger.c.tokens_used), 0)).where(base)
            row = (await conn.execute(stmt)).fetchone()
            grand_total = row[0]

            # -- By project --
            stmt = (
                select(
                    token_ledger.c.project_id,
                    projects.c.name.label("project_name"),
                    func.sum(token_ledger.c.tokens_used).label("tokens"),
                    func.count(func.distinct(token_ledger.c.task_id)).label("task_count"),
                )
                .join(projects, token_ledger.c.project_id == projects.c.id)
                .where(base)
                .group_by(token_ledger.c.project_id, projects.c.name)
                .order_by(func.sum(token_ledger.c.tokens_used).desc())
            )
            rows = (await conn.execute(stmt)).fetchall()
            by_project = [
                {
                    "project_id": r.project_id,
                    "project_name": r.project_name,
                    "tokens": r.tokens,
                    "task_count": r.task_count,
                }
                for r in rows
            ]

            # -- Top tasks --
            stmt = (
                select(
                    token_ledger.c.project_id,
                    token_ledger.c.task_id,
                    tasks.c.title.label("task_title"),
                    tasks.c.status.label("task_status"),
                    func.sum(token_ledger.c.tokens_used).label("tokens"),
                )
                .join(tasks, token_ledger.c.task_id == tasks.c.id)
                .where(base)
                .group_by(
                    token_ledger.c.project_id,
                    token_ledger.c.task_id,
                    tasks.c.title,
                    tasks.c.status,
                )
                .order_by(func.sum(token_ledger.c.tokens_used).desc())
                .limit(20)
            )
            rows = (await conn.execute(stmt)).fetchall()
            top_tasks = [
                {
                    "project_id": r.project_id,
                    "task_id": r.task_id,
                    "title": r.task_title,
                    "status": r.task_status,
                    "tokens": r.tokens,
                }
                for r in rows
            ]

            # -- Daily totals --
            # Group in Python to avoid dialect-specific date functions
            stmt = (
                select(
                    token_ledger.c.timestamp,
                    token_ledger.c.tokens_used,
                )
                .where(base)
                .order_by(token_ledger.c.timestamp)
            )
            rows = (await conn.execute(stmt)).fetchall()
            daily_map: dict[str, int] = {}
            for r in rows:
                day = datetime.fromtimestamp(r.timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
                daily_map[day] = daily_map.get(day, 0) + r.tokens_used
            daily = [{"date": d, "tokens": t} for d, t in sorted(daily_map.items())]

        since_str = datetime.fromtimestamp(since, tz=timezone.utc).strftime("%Y-%m-%d")
        until_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")

        return {
            "total": grand_total,
            "days": days,
            "since": since_str,
            "until": until_str,
            "project_id": project_id,
            "by_project": by_project,
            "top_tasks": top_tasks,
            "daily": daily,
        }
