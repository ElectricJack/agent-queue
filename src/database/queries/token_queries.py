"""Token ledger operations."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    agents, archived_tasks, benchmark_stage_spans, projects, sessions, task_session_attempts, tasks,
    token_ledger, transcript_usage_calls,
)


class TokenQueryMixin:
    """Query mixin for token ledger operations.  Expects ``self._engine``."""

    async def record_transcript_usage(
        self, *, usage_key: str, project_id: str | None, agent_id: str,
        task_id: str, session_id: str, attempt_id: str | None,
        model: str | None, model_source: str | None, counters: dict[str, int],
        legacy_call_ids: list[str],
    ) -> None:
        """Atomically append only new per-call counter maxima.

        The progress row serializes concurrent readers, independently of AQ
        session IDs and byte checkpoints. Original ledger rows are immutable.
        On upgrade, legacy content UUIDs seed maxima, never sums of duplicates.
        """
        fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        observed = {key: int(counters.get(key) or 0) for key in fields}
        if any(value < 0 for value in observed.values()):
            raise ValueError("transcript usage counters must be nonnegative")
        async with self._engine.begin() as conn:
            await conn.execute(pg_insert(transcript_usage_calls).values(
                usage_key=usage_key, first_ledger_id=None, updated_at=time.time(),
                **dict.fromkeys(fields, 0),
            ).on_conflict_do_nothing(index_elements=["usage_key"]))
            prior = (await conn.execute(select(transcript_usage_calls).where(
                transcript_usage_calls.c.usage_key == usage_key
            ).with_for_update())).mappings().one()
            baseline = {key: prior[key] for key in fields}
            first_id = prior["first_ledger_id"]
            source = None
            if first_id:
                source = (await conn.execute(select(token_ledger).where(
                    token_ledger.c.id == first_id
                ))).mappings().one()
            elif legacy_call_ids:
                legacy = (await conn.execute(select(token_ledger).where(
                    token_ledger.c.call_id.in_(legacy_call_ids)
                ).order_by(token_ledger.c.timestamp, token_ledger.c.id))).mappings().all()
                if legacy:
                    source = legacy[0]
                    first_id = source["id"]
                    baseline = {key: max(int(row[key] or 0) for row in legacy) for key in fields}
            maxima = {key: max(baseline[key], observed[key]) for key in fields}
            delta = {key: maxima[key] - baseline[key] for key in fields}
            if sum(delta.values()):
                revision = ":".join(str(maxima[key]) for key in fields)
                call_id = f"{usage_key}:{revision}"
                ledger_id = str(uuid.uuid5(uuid.NAMESPACE_URL, call_id))
                attribution = {
                    "project_id": project_id, "agent_id": agent_id, "task_id": task_id,
                    "session_id": session_id, "attempt_id": attempt_id,
                }
                if source is not None:
                    attribution = {key: source[key] for key in attribution}
                await conn.execute(insert(token_ledger).values(
                    id=ledger_id, **attribution, call_id=call_id,
                    model=model or (source["model"] if source is not None else None),
                    model_source=model_source or (
                        source["model_source"] if source is not None else None
                    ),
                    tokens_used=sum(delta.values()), **delta, timestamp=time.time(),
                ))
                first_id = first_id or ledger_id
            await conn.execute(update(transcript_usage_calls).where(
                transcript_usage_calls.c.usage_key == usage_key
            ).values(**maxima, first_ledger_id=first_id, updated_at=time.time()))

    async def record_token_usage(
        self,
        project_id: str | None,
        agent_id: str,
        task_id: str,
        tokens: int,
        *,
        model: str | None = None,
        model_source: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        session_id: str | None = None,
        attempt_id: str | None = None,
        call_id: str | None = None,
    ) -> None:
        """Append a token usage record.

        ``tokens`` remains the authoritative total.  ``model`` and the
        input/output split are optional because most writers only know the
        total: a row without them is reported as ``unpriced_tokens`` by
        :meth:`get_cost_rollup` rather than priced at a guessed rate
        (``docs/specs/design/trust-and-ops.md`` §7 — honesty over estimates).

        The cache figures are a *third* category, not part of that split:
        they are billed at their own rates, so they are recorded beside
        ``input_tokens`` rather than inside it.  Passing them is what lets a
        reader account for the whole of ``tokens`` instead of writing the
        difference off as unattributed.
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
                    model_source=model_source,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    session_id=session_id,
                    attempt_id=attempt_id,
                    call_id=call_id,
                    timestamp=time.time(),
                )
            )

    async def get_benchmark_evidence(self, project_id: str, task_ids: list[str]) -> dict:
        """Read an explicit frozen task set, including archived tasks and failed attempts."""
        if not task_ids or len(task_ids) > 500 or len(set(task_ids)) != len(task_ids):
            raise ValueError("task_ids must be 1-500 distinct IDs")
        async with self._engine.begin() as conn:
            active = (await conn.execute(select(tasks.c.id, tasks.c.status, tasks.c.route).where(
                tasks.c.id.in_(task_ids), tasks.c.project_id == project_id
            ))).mappings().all()
            archived = (await conn.execute(select(
                archived_tasks.c.id, archived_tasks.c.status, archived_tasks.c.route
            ).where(
                archived_tasks.c.id.in_(task_ids), archived_tasks.c.project_id == project_id
            ))).mappings().all()
            found = {row["id"]: dict(row) for row in [*active, *archived]}
            missing = sorted(set(task_ids) - set(found))
            if missing:
                raise ValueError(f"tasks missing from project {project_id}: {missing}")
            attempts = (await conn.execute(select(task_session_attempts).where(
                task_session_attempts.c.project_id == project_id,
                task_session_attempts.c.task_id.in_(task_ids),
            ).order_by(task_session_attempts.c.started_at, task_session_attempts.c.id))).mappings().all()
            ledger = (await conn.execute(select(token_ledger).where(
                token_ledger.c.project_id == project_id,
                token_ledger.c.task_id.in_(task_ids),
            ).order_by(token_ledger.c.timestamp, token_ledger.c.id))).mappings().all()
            spans = (await conn.execute(select(benchmark_stage_spans).where(
                benchmark_stage_spans.c.project_id == project_id,
                benchmark_stage_spans.c.task_id.in_(task_ids),
            ).order_by(benchmark_stage_spans.c.recorded_at, benchmark_stage_spans.c.id))).mappings().all()
        return {
            "tasks": found,
            "attempts": [dict(row) for row in attempts],
            "ledger": [dict(row) for row in ledger],
            "stage_spans": [dict(row) for row in spans],
        }

    async def record_benchmark_stage(
        self, *, span_id: str, project_id: str, task_id: str,
        session_attempt_id: str | None, stage: str,
        started_monotonic_ns: int, ended_monotonic_ns: int,
        owner_session_id: str | None = None, claim_epoch: int | None = None,
    ) -> bool:
        """Append one monotonic stage measurement, idempotent by span ID."""
        values = {
            "id": span_id, "project_id": project_id, "task_id": task_id,
            "session_attempt_id": session_attempt_id, "stage": stage,
            "started_monotonic_ns": started_monotonic_ns,
            "ended_monotonic_ns": ended_monotonic_ns,
            "duration_ms": (ended_monotonic_ns - started_monotonic_ns) / 1_000_000,
            "recorded_at": time.time(),
        }
        async with self._engine.begin() as conn:
            if owner_session_id is not None:
                owner = select(tasks.c.id).select_from(
                    tasks.join(sessions, sessions.c.task_id == tasks.c.id)
                ).where(
                    tasks.c.id == task_id,
                    tasks.c.project_id == project_id,
                    sessions.c.id == owner_session_id,
                    sessions.c.project_id == project_id,
                    sessions.c.agent_id == tasks.c.assigned_agent_id,
                    sessions.c.state.in_(("starting", "running", "draining")),
                ).with_for_update(of=tasks)
                if claim_epoch is not None:
                    owner = owner.where(tasks.c.claim_epoch == claim_epoch)
                if (await conn.execute(owner)).scalar_one_or_none() is None:
                    raise ValueError("this session no longer owns the task or claim")
            result = await conn.execute(pg_insert(benchmark_stage_spans).values(
                **values
            ).on_conflict_do_nothing(index_elements=["id"]))
            if result.rowcount:
                return True
            old = (await conn.execute(select(benchmark_stage_spans).where(
                benchmark_stage_spans.c.id == span_id
            ))).mappings().one()
            if any(old[key] != value for key, value in values.items() if key != "recorded_at"):
                raise ValueError("span_id already names a different stage measurement")
            return False

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
            token_ledger.c.cache_read_tokens,
            token_ledger.c.cache_write_tokens,
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
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "tokens_used": 0,
                    "entries": 0,
                },
            )
            bucket["input_tokens"] += r.input_tokens or 0
            bucket["output_tokens"] += r.output_tokens or 0
            bucket["cache_read_tokens"] += r.cache_read_tokens or 0
            bucket["cache_write_tokens"] += r.cache_write_tokens or 0
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
