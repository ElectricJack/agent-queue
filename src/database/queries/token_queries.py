"""Token ledger operations."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, insert, select

from src.database.tables import agents, archived_tasks, projects, tasks, token_ledger


class TokenQueryMixin:
    """Query mixin for token ledger operations.  Expects ``self._engine``."""

    async def record_token_usage(
        self,
        project_id: str,
        agent_id: str,
        task_id: str,
        tokens: int,
        *,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """Append a token usage record.

        ``tokens`` remains the authoritative total.  ``model`` and the
        input/output split are optional because most writers only know the
        total: a row without them is reported as ``unpriced_tokens`` by
        :meth:`get_cost_rollup` rather than priced at a guessed rate
        (``docs/specs/design/trust-and-ops.md`` §7 — honesty over estimates).
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(token_ledger).values(
                    id=str(uuid.uuid4()),
                    project_id=project_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    tokens_used=tokens,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    timestamp=time.time(),
                )
            )

    async def get_cost_rollup(
        self,
        *,
        project_id: str | None = None,
        since_ts: float | None = None,
        group_by: str = "project",
    ) -> list[dict]:
        """Roll the token ledger up for cost reporting.

        Args:
            project_id: Restrict to one project.
            since_ts: Unix timestamp lower bound (inclusive).
            group_by: ``"project"``, ``"profile"`` (via ``agents.profile_id``)
                or ``"day"``.

        Returns:
            One dict per ``(group key, model)`` pair with keys ``group``,
            ``model``, ``input_tokens``, ``output_tokens``, ``tokens_used``
            and ``entries``.  ``model`` is ``None`` for rows the writer could
            not attribute; rows lacking a split leave ``input_tokens`` /
            ``output_tokens`` at 0 so the caller can count them as unpriced.

        Grouping happens in Python (like :meth:`get_token_audit`) so no
        dialect-specific date functions are needed.
        """
        if group_by not in ("project", "profile", "day"):
            raise ValueError(f"unknown group_by: {group_by!r}")

        stmt = select(
            token_ledger.c.project_id,
            token_ledger.c.agent_id,
            token_ledger.c.tokens_used,
            token_ledger.c.model,
            token_ledger.c.input_tokens,
            token_ledger.c.output_tokens,
            token_ledger.c.timestamp,
            agents.c.profile_id.label("profile_id"),
        ).select_from(
            token_ledger.join(agents, token_ledger.c.agent_id == agents.c.id, isouter=True)
        )
        if project_id:
            stmt = stmt.where(token_ledger.c.project_id == project_id)
        if since_ts is not None:
            stmt = stmt.where(token_ledger.c.timestamp >= since_ts)

        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).fetchall()

        buckets: dict[tuple[str, str | None], dict] = {}
        for r in rows:
            if group_by == "project":
                key = r.project_id or "(unknown)"
            elif group_by == "profile":
                key = r.profile_id or "(unknown)"
            else:
                key = datetime.fromtimestamp(r.timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
            bucket_key = (key, r.model)
            bucket = buckets.setdefault(
                bucket_key,
                {
                    "group": key,
                    "model": r.model,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "tokens_used": 0,
                    "entries": 0,
                },
            )
            bucket["input_tokens"] += r.input_tokens or 0
            bucket["output_tokens"] += r.output_tokens or 0
            bucket["tokens_used"] += r.tokens_used or 0
            bucket["entries"] += 1

        return [buckets[k] for k in sorted(buckets, key=lambda k: (k[0], k[1] or ""))]

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
                .join(projects, token_ledger.c.project_id == projects.c.id, isouter=True)
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
                # Outer join: completed tasks are moved to ``archived_tasks``,
                # so an inner join would silently drop exactly the finished
                # work that dominates spend — while ``total`` above still
                # counted it, making the two halves of the report disagree.
                .join(tasks, token_ledger.c.task_id == tasks.c.id, isouter=True)
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

            # Most spend belongs to *finished* work, which lives in
            # ``archived_tasks``.  Backfill titles/statuses for those so the
            # report shows names instead of a column of bare ids.
            missing = [t["task_id"] for t in top_tasks if t["title"] is None and t["task_id"]]
            if missing:
                arch_rows = (
                    await conn.execute(
                        select(
                            archived_tasks.c.id,
                            archived_tasks.c.title,
                            archived_tasks.c.status,
                        ).where(archived_tasks.c.id.in_(missing))
                    )
                ).fetchall()
                arch = {r.id: (r.title, r.status) for r in arch_rows}
                for t in top_tasks:
                    hit = arch.get(t["task_id"])
                    if hit is not None:
                        t["title"], t["status"] = hit
                        t["archived"] = True

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
