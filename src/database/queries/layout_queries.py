"""Layout storage queries (spatial-layout design §4.6, §4.10). Expects ``self._engine``."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Iterable
from itertools import islice

from sqlalchemy import case, delete, exists, func, insert, select, update

from src.database.tables import (
    layout_dirty,
    layout_jobs,
    layout_reflow_requests,
    layout_tidy_request_pairs,
    layout_tidy_requests,
    project_layout_meta,
)


LAYOUT_WRITE_BATCH_SIZE = 256
"""Bound synchronous parameter preparation between event-loop yields during publication."""

MAX_REFLOW_SCOPES_PER_JOB = 32
"""Bound one periodic reflow group, even for a very busy project."""

REFLOW_LEASE_SECONDS = 1_800
MAX_REFLOW_ATTEMPTS = 3
REFLOW_RETRY_BASE_SECONDS = 900


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
    return like_escape(prefix) + "%"


def like_escape(needle: str) -> str:
    """Escape LIKE metacharacters in a user-supplied literal.

    Always pair with ``.like(..., escape="\\")``.
    """
    return needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: Edges touching a set of ids whose two endpoints are *drawn* at different
#: nodes (see :meth:`LayoutQueryMixin.load_edges_touching`).
#:
#: The shape of the ``WHERE`` clause is load-bearing and was got wrong once.
#: ``task_dependencies`` is the preserved side of two outer joins, so a
#: restriction expressed as ``a.task_id IS NOT NULL OR b.task_id IS NOT NULL``
#: is a post-join predicate: it is not null-rejecting for either side, the
#: planner cannot push it down, and with the CTE referenced twice it is
#: materialised without indexes.  The result was a **sequential scan of the
#: whole dependency table on every tiles request** — invisible on a
#: single-project fixture and quadratic-feeling on a real install, since the
#: table holds every project's edges and this read has no project column to
#: filter on.  Measured on the §9 fixture plus one unrelated 560k-edge
#: project: 232ms for the post-join form against 84ms for this one.
#:
#: So the base restriction is stated directly on ``d`` against the two
#: indexes that exist (``idx_task_deps_task_type``,
#: ``idx_task_deps_depson_type``, giving a ``BitmapOr``), and the joins are
#: left to do nothing but look the owner up.  ``dep_type`` is restricted here
#: too: only ``DRAWN_TYPES`` can become an arrow, and on an expanded view the
#: ``parent-child`` rows the filter removes are the dominant class.
#: ``view.remap_edges`` still applies both filters, as the backstop.
CROSSING_EDGES_SQL = """
WITH own AS (
    SELECT * FROM unnest(:ids, :owners) AS o(task_id, owner_key)
)
SELECT d.task_id, d.depends_on_task_id, d.dep_type, d.description
FROM task_dependencies d
LEFT JOIN own a ON a.task_id = d.task_id
LEFT JOIN own b ON b.task_id = d.depends_on_task_id
WHERE (d.task_id = ANY(:ids) OR d.depends_on_task_id = ANY(:ids))
  AND d.dep_type = ANY(:drawn)
  AND COALESCE(a.owner_key, d.task_id)
      IS DISTINCT FROM COALESCE(b.owner_key, d.depends_on_task_id)
"""


def crossing_edges_statement(ids: list[str], owners, *, explain: bool = False):
    """:data:`CROSSING_EDGES_SQL` bound for ``ids`` under ``owners``.

    Separate from the read, and with an ``explain`` seam, so the plan guard in
    ``tests/perf/test_layout_api_statements.py`` explains the exact statement
    the endpoint ships rather than a copy of it that can drift.
    """
    from sqlalchemy import ARRAY, Text, bindparam, text

    from src.task_graph.layout.constants import DRAWN_TYPES

    sql = ("EXPLAIN " + CROSSING_EDGES_SQL) if explain else CROSSING_EDGES_SQL
    return text(sql).bindparams(
        bindparam("ids", value=list(ids), type_=ARRAY(Text)),
        bindparam("owners", value=[owners.get(t, t) for t in ids], type_=ARRAY(Text)),
        bindparam("drawn", value=sorted(DRAWN_TYPES), type_=ARRAY(Text)),
    )


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
            (layout_reflow_requests, layout_reflow_requests.c.project_id),
            (layout_jobs, layout_jobs.c.project_id),
            (layout_tidy_request_pairs, layout_tidy_request_pairs.c.project_id),
        ):
            await conn.execute(delete(table).where(col == project_id))

    async def clear_layout_dirty(self, project_id: str, up_to_seq: int, *, conn) -> None:
        await conn.execute(
            delete(layout_dirty).where(
                layout_dirty.c.project_id == project_id, layout_dirty.c.seq <= up_to_seq
            )
        )

    # ── deferred active reflow ─────────────────────────────────────────
    async def _enqueue_layout_reflows_on_conn(
        self,
        conn,
        project_id: str,
        variant: str,
        scope_keys: Iterable[str],
    ) -> None:
        """Coalesce fresh finished-leaf reflows into their durable ledger.

        A running lease is deliberately retained: its worker owns the older
        generation, while the upsert records the newer generation behind it.
        The acknowledgement path releases that successor back to ``queued``.
        """
        keys = sorted(set(scope_keys))
        if not keys:
            return
        from sqlalchemy.dialects import postgresql, sqlite

        now = time.time()
        dialect = self._engine.dialect.name
        ins = (postgresql.insert if dialect == "postgresql" else sqlite.insert)(
            layout_reflow_requests
        )
        current = layout_reflow_requests
        stmt = ins.values(
            [
                {
                    "project_id": project_id,
                    "variant": variant,
                    "scope_key": scope_key,
                    "generation": 1,
                    "state": "queued",
                    "requested_at": now,
                    "retry_after": now,
                    "attempts": 0,
                    "last_error": None,
                    "claimed_generation": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                }
                for scope_key in keys
            ]
        ).on_conflict_do_update(
            index_elements=["project_id", "variant", "scope_key"],
            set_={
                "generation": current.c.generation + 1,
                "state": case((current.c.state == "running", "running"), else_="queued"),
                "requested_at": now,
                "retry_after": now,
                "attempts": 0,
                "last_error": None,
                "claimed_generation": case(
                    (current.c.state == "running", current.c.claimed_generation), else_=None
                ),
                "lease_token": case(
                    (current.c.state == "running", current.c.lease_token), else_=None
                ),
                "lease_expires_at": case(
                    (current.c.state == "running", current.c.lease_expires_at), else_=None
                ),
            },
        )
        await conn.execute(stmt)

    async def enqueue_layout_reflows(
        self, project_id: str, variant: str, scope_keys: Iterable[str]
    ) -> None:
        async with self._engine.begin() as conn:
            await self._enqueue_layout_reflows_on_conn(conn, project_id, variant, scope_keys)

    async def capture_layout_reflow_generations(
        self, project_id: str, variant: str
    ) -> dict[str, int]:
        """Capture generations a full layout may acknowledge on publication.

        This deliberately happens before the full-layout snapshot.  A newer
        trigger arriving after this read has a greater generation and survives
        the later acknowledgement.
        """
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(
                        layout_reflow_requests.c.scope_key,
                        layout_reflow_requests.c.generation,
                    ).where(
                        layout_reflow_requests.c.project_id == project_id,
                        layout_reflow_requests.c.variant == variant,
                    )
                )
            ).all()
        return {scope_key: generation for scope_key, generation in rows}

    @staticmethod
    def _reflow_claim(row) -> dict:
        return {
            "project_id": row["project_id"],
            "variant": row["variant"],
            "scope_key": row["scope_key"],
            "generation": row["generation"],
            "lease_token": row["lease_token"],
        }

    def _reflow_for_update(self, statement):
        return statement.with_for_update(skip_locked=True)

    async def _release_layout_reflow_claim_on_conn(self, conn, claim: dict, error: str) -> bool:
        """Release one expired/failed claim, preserving a newer generation."""
        row = (
            await conn.execute(
                self._reflow_for_update(
                    select(layout_reflow_requests).where(
                        layout_reflow_requests.c.project_id == claim["project_id"],
                        layout_reflow_requests.c.variant == claim["variant"],
                        layout_reflow_requests.c.scope_key == claim["scope_key"],
                    )
                )
            )
        ).mappings().first()
        if (
            row is None
            or row["state"] != "running"
            or row["claimed_generation"] != claim["generation"]
            or row["lease_token"] != claim["lease_token"]
        ):
            return False
        now = time.time()
        current_generation = row["generation"]
        if current_generation != claim["generation"]:
            values = {
                "state": "queued",
                "retry_after": now,
                "attempts": 0,
                "last_error": None,
                "claimed_generation": None,
                "lease_token": None,
                "lease_expires_at": None,
            }
        else:
            attempts = int(row["attempts"]) + 1
            failed = attempts >= MAX_REFLOW_ATTEMPTS
            values = {
                "state": "failed" if failed else "queued",
                "retry_after": now
                + (0 if failed else REFLOW_RETRY_BASE_SECONDS * 2 ** (attempts - 1)),
                "attempts": attempts,
                "last_error": error,
                "claimed_generation": None,
                "lease_token": None,
                "lease_expires_at": None,
            }
        result = await conn.execute(
            update(layout_reflow_requests)
            .where(
                layout_reflow_requests.c.project_id == claim["project_id"],
                layout_reflow_requests.c.variant == claim["variant"],
                layout_reflow_requests.c.scope_key == claim["scope_key"],
                layout_reflow_requests.c.state == "running",
                layout_reflow_requests.c.claimed_generation == claim["generation"],
                layout_reflow_requests.c.lease_token == claim["lease_token"],
            )
            .values(**values)
        )
        return result.rowcount == 1

    async def reap_expired_layout_reflows(self) -> int:
        """Recover leases left by a stopped daemon before the periodic claim."""
        async with self._engine.begin() as conn:
            now = time.time()
            rows = (
                await conn.execute(
                    self._reflow_for_update(
                        select(layout_reflow_requests).where(
                            layout_reflow_requests.c.state == "running",
                            layout_reflow_requests.c.lease_expires_at.is_not(None),
                            layout_reflow_requests.c.lease_expires_at <= now,
                        )
                    )
                )
            ).mappings().all()
            released = 0
            for row in rows:
                released += await self._release_layout_reflow_claim_on_conn(
                    conn, self._reflow_claim(row), "lease expired"
                )
            return released

    async def claim_layout_reflow_group(self) -> dict | None:
        """Claim one bounded, oldest deferred group for a sweep.

        Ordinary queued/running layout jobs keep precedence for their exact
        project/variant.  Only this sweep-facing query reads the reflow table;
        the five-second dirty loop continues to inspect ``layout_dirty`` only.
        """
        await self.reap_expired_layout_reflows()
        async with self._engine.begin() as conn:
            now = time.time()
            ordinary_job = exists(
                select(layout_jobs.c.id).where(
                    layout_jobs.c.project_id == layout_reflow_requests.c.project_id,
                    layout_jobs.c.variant == layout_reflow_requests.c.variant,
                    layout_jobs.c.status.in_(("queued", "running")),
                )
            )
            rows = (
                await conn.execute(
                    self._reflow_for_update(
                        select(layout_reflow_requests)
                        .where(
                            layout_reflow_requests.c.state == "queued",
                            layout_reflow_requests.c.retry_after <= now,
                            ~ordinary_job,
                        )
                        .order_by(
                            layout_reflow_requests.c.requested_at,
                            layout_reflow_requests.c.project_id,
                            layout_reflow_requests.c.variant,
                            layout_reflow_requests.c.scope_key,
                        )
                        .limit(MAX_REFLOW_SCOPES_PER_JOB * 4)
                    )
                )
            ).mappings().all()
            if not rows:
                return None
            project_id, variant = rows[0]["project_id"], rows[0]["variant"]
            group_rows = [
                row
                for row in rows
                if row["project_id"] == project_id and row["variant"] == variant
            ][:MAX_REFLOW_SCOPES_PER_JOB]
            token = uuid.uuid4().hex
            expires = now + REFLOW_LEASE_SECONDS
            claims: list[dict] = []
            for row in group_rows:
                result = await conn.execute(
                    update(layout_reflow_requests)
                    .where(
                        layout_reflow_requests.c.project_id == project_id,
                        layout_reflow_requests.c.variant == variant,
                        layout_reflow_requests.c.scope_key == row["scope_key"],
                        layout_reflow_requests.c.generation == row["generation"],
                        layout_reflow_requests.c.state == "queued",
                    )
                    .values(
                        state="running",
                        claimed_generation=row["generation"],
                        lease_token=token,
                        lease_expires_at=expires,
                    )
                )
                if result.rowcount:
                    claims.append(
                        {
                            "project_id": project_id,
                            "variant": variant,
                            "scope_key": row["scope_key"],
                            "generation": row["generation"],
                            "lease_token": token,
                        }
                    )
            return {"project_id": project_id, "variant": variant, "claims": claims} if claims else None

    async def fail_layout_reflow_claims(self, claims: Iterable[dict], error: str) -> None:
        """Release a failed compute/publish group with bounded backoff."""
        async with self._engine.begin() as conn:
            for claim in claims:
                await self._release_layout_reflow_claim_on_conn(conn, claim, error)

    async def _ack_layout_reflows_on_conn(
        self,
        conn,
        project_id: str,
        variant: str,
        *,
        captured: dict[str, int],
        claims: Iterable[dict],
    ) -> None:
        """Acknowledge a full-layout capture or exact deferred leases.

        A newer generation is never deleted.  When an older running lease has
        a successor, its successful publication releases the successor for a
        future sweep instead of making the newer request disappear.
        """
        for scope_key, generation in captured.items():
            await conn.execute(
                delete(layout_reflow_requests).where(
                    layout_reflow_requests.c.project_id == project_id,
                    layout_reflow_requests.c.variant == variant,
                    layout_reflow_requests.c.scope_key == scope_key,
                    layout_reflow_requests.c.generation <= generation,
                )
            )
        for claim in claims:
            row = (
                await conn.execute(
                    self._reflow_for_update(
                        select(layout_reflow_requests).where(
                            layout_reflow_requests.c.project_id == project_id,
                            layout_reflow_requests.c.variant == variant,
                            layout_reflow_requests.c.scope_key == claim["scope_key"],
                            layout_reflow_requests.c.state == "running",
                            layout_reflow_requests.c.claimed_generation == claim["generation"],
                            layout_reflow_requests.c.lease_token == claim["lease_token"],
                        )
                    )
                )
            ).mappings().first()
            if row is None:
                continue
            if row["generation"] == claim["generation"]:
                await conn.execute(
                    delete(layout_reflow_requests).where(
                        layout_reflow_requests.c.project_id == project_id,
                        layout_reflow_requests.c.variant == variant,
                        layout_reflow_requests.c.scope_key == claim["scope_key"],
                        layout_reflow_requests.c.generation == claim["generation"],
                        layout_reflow_requests.c.lease_token == claim["lease_token"],
                    )
                )
            else:
                await conn.execute(
                    update(layout_reflow_requests)
                    .where(
                        layout_reflow_requests.c.project_id == project_id,
                        layout_reflow_requests.c.variant == variant,
                        layout_reflow_requests.c.scope_key == claim["scope_key"],
                        layout_reflow_requests.c.lease_token == claim["lease_token"],
                    )
                    .values(
                        state="queued",
                        retry_after=time.time(),
                        claimed_generation=None,
                        lease_token=None,
                        lease_expires_at=None,
                    )
                )

    async def layout_reflow_status(self, project_id: str | None = None) -> dict:
        """Return operator diagnostics without changing deferred work."""
        async with self._engine.begin() as conn:
            condition = [] if project_id is None else [layout_reflow_requests.c.project_id == project_id]
            rows = (
                await conn.execute(
                    select(layout_reflow_requests).where(*condition).order_by(
                        layout_reflow_requests.c.project_id,
                        layout_reflow_requests.c.variant,
                        layout_reflow_requests.c.requested_at,
                        layout_reflow_requests.c.scope_key,
                    )
                )
            ).mappings().all()
        counts = {state: 0 for state in ("queued", "running", "failed")}
        failed: list[dict] = []
        for row in rows:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
            if row["state"] == "failed":
                failed.append(
                    {
                        "project_id": row["project_id"],
                        "variant": row["variant"],
                        "scope_key": row["scope_key"],
                        "generation": row["generation"],
                        "attempts": row["attempts"],
                        "last_error": row["last_error"],
                    }
                )
        return {**counts, "failed_scopes": failed}

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
    async def _enqueue_layout_job_on_conn(
        self, conn, project_id: str, variant: str, kind: str
    ) -> dict:
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

    async def enqueue_layout_job(self, project_id: str, variant: str, kind: str) -> dict:
        async with self._engine.begin() as conn:
            return await self._enqueue_layout_job_on_conn(conn, project_id, variant, kind)

    async def _layout_tidy_request_summary(self, conn, request_id: str) -> dict | None:
        request = (
            await conn.execute(
                select(layout_tidy_requests).where(layout_tidy_requests.c.id == request_id)
            )
        ).mappings().first()
        if request is None:
            return None
        rows = (
            await conn.execute(
                select(layout_tidy_request_pairs.c.status, func.count().label("count"))
                .where(layout_tidy_request_pairs.c.request_id == request_id)
                .group_by(layout_tidy_request_pairs.c.status)
            )
        ).mappings().all()
        counts = {status: 0 for status in ("pending", "queued", "running", "completed", "failed")}
        counts.update({row["status"]: int(row["count"]) for row in rows})
        return {
            **dict(request),
            "total": sum(counts.values()),
            "remaining": counts["pending"] + counts["queued"] + counts["running"],
            **counts,
        }

    async def create_layout_tidy_request(self, *, reason: str) -> dict:
        """Create (or report) a durable all-project Tidy request.

        Repeated calls with the same active reason are status reads, so an
        operator can re-run ``aq graph tidy --all`` to see progress without
        queuing duplicate work. Completed requests do not block a later run.
        """
        from src.database.tables import projects

        async with self._engine.begin() as conn:
            active = (
                await conn.execute(
                    select(layout_tidy_requests.c.id)
                    .where(
                        layout_tidy_requests.c.reason == reason,
                        layout_tidy_requests.c.status.in_(("queued", "running")),
                    )
                    .order_by(layout_tidy_requests.c.requested_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if active is not None:
                return (await self._layout_tidy_request_summary(conn, active)) or {}

            now = time.time()
            request_id = uuid.uuid4().hex
            pairs = (
                await conn.execute(
                    select(project_layout_meta.c.project_id, project_layout_meta.c.variant)
                    .join(projects, projects.c.id == project_layout_meta.c.project_id)
                    .where(projects.c.status == "ACTIVE")
                    .order_by(project_layout_meta.c.project_id, project_layout_meta.c.variant)
                )
            ).all()
            status = "queued" if pairs else "completed"
            await conn.execute(
                insert(layout_tidy_requests).values(
                    id=request_id,
                    reason=reason,
                    status=status,
                    requested_at=now,
                    finished_at=None if pairs else now,
                )
            )
            if pairs:
                await conn.execute(
                    insert(layout_tidy_request_pairs),
                    [
                        {
                            "request_id": request_id,
                            "project_id": project_id,
                            "variant": variant,
                            "status": "pending",
                        }
                        for project_id, variant in pairs
                    ],
                )
            return (await self._layout_tidy_request_summary(conn, request_id)) or {}

    async def layout_tidy_request_for_job(self, job_id: str, *, running: bool = False) -> None:
        """Reflect an existing layout job's lifecycle in its bulk request."""
        async with self._engine.begin() as conn:
            if running:
                await conn.execute(
                    update(layout_tidy_request_pairs)
                    .where(
                        layout_tidy_request_pairs.c.job_id == job_id,
                        layout_tidy_request_pairs.c.status == "queued",
                    )
                    .values(status="running")
                )
                return
            job = (
                await conn.execute(select(layout_jobs).where(layout_jobs.c.id == job_id))
            ).mappings().first()
            if job is None or job["status"] not in ("done", "failed"):
                return
            await conn.execute(
                update(layout_tidy_request_pairs)
                .where(
                    layout_tidy_request_pairs.c.job_id == job_id,
                    layout_tidy_request_pairs.c.status.in_(("queued", "running")),
                )
                .values(
                    status="completed" if job["status"] == "done" else "failed",
                    error=job["error"],
                    finished_at=job["finished_at"] or time.time(),
                )
            )

    async def advance_layout_tidy_requests(self) -> dict | None:
        """Settle completed bulk pairs and release at most one pending pair.

        The scheduler only releases another pair once the existing layout-job
        queue is clear. That leaves an operator's ordinary per-project Tidy
        ahead of future bulk entries while preserving the already-committed
        pair across a daemon restart.
        """
        async with self._engine.begin() as conn:
            tracked = (
                await conn.execute(
                    select(
                        layout_tidy_request_pairs.c.request_id,
                        layout_tidy_request_pairs.c.project_id,
                        layout_tidy_request_pairs.c.variant,
                        layout_tidy_request_pairs.c.job_id,
                        layout_jobs.c.status.label("job_status"),
                        layout_jobs.c.error.label("job_error"),
                        layout_jobs.c.finished_at.label("job_finished_at"),
                    )
                    .select_from(
                        layout_tidy_request_pairs.outerjoin(
                            layout_jobs,
                            layout_jobs.c.id == layout_tidy_request_pairs.c.job_id,
                        )
                    )
                    .where(layout_tidy_request_pairs.c.status.in_(("queued", "running")))
                )
            ).mappings().all()
            for pair in tracked:
                if pair["job_status"] in ("done", "failed"):
                    await conn.execute(
                        update(layout_tidy_request_pairs)
                        .where(
                            layout_tidy_request_pairs.c.request_id == pair["request_id"],
                            layout_tidy_request_pairs.c.project_id == pair["project_id"],
                            layout_tidy_request_pairs.c.variant == pair["variant"],
                        )
                        .values(
                            status="completed" if pair["job_status"] == "done" else "failed",
                            error=pair["job_error"],
                            finished_at=pair["job_finished_at"] or time.time(),
                        )
                    )
                elif pair["job_status"] is None:
                    await conn.execute(
                        update(layout_tidy_request_pairs)
                        .where(
                            layout_tidy_request_pairs.c.request_id == pair["request_id"],
                            layout_tidy_request_pairs.c.project_id == pair["project_id"],
                            layout_tidy_request_pairs.c.variant == pair["variant"],
                        )
                        .values(
                            status="failed",
                            error="layout job disappeared",
                            finished_at=time.time(),
                        )
                    )

            active_requests = (
                await conn.execute(
                    select(layout_tidy_requests.c.id).where(
                        layout_tidy_requests.c.status.in_(("queued", "running"))
                    )
                )
            ).scalars().all()
            for request_id in active_requests:
                pending = (
                    await conn.execute(
                        select(func.count())
                        .select_from(layout_tidy_request_pairs)
                        .where(
                            layout_tidy_request_pairs.c.request_id == request_id,
                            layout_tidy_request_pairs.c.status.in_(("pending", "queued", "running")),
                        )
                    )
                ).scalar_one()
                if not pending:
                    await conn.execute(
                        update(layout_tidy_requests)
                        .where(layout_tidy_requests.c.id == request_id)
                        .values(status="completed", finished_at=time.time())
                    )

            in_flight_pair = (
                await conn.execute(
                    select(layout_tidy_request_pairs.c.request_id)
                    .where(layout_tidy_request_pairs.c.status.in_(("queued", "running")))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if in_flight_pair is not None:
                return await self._layout_tidy_request_summary(conn, in_flight_pair)

            queued_job = (
                await conn.execute(
                    select(layout_jobs.c.id)
                    .where(layout_jobs.c.status.in_(("queued", "running")))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if queued_job is not None:
                return None

            pair = (
                await conn.execute(
                    select(
                        layout_tidy_request_pairs.c.request_id,
                        layout_tidy_request_pairs.c.project_id,
                        layout_tidy_request_pairs.c.variant,
                    )
                    .join(
                        layout_tidy_requests,
                        layout_tidy_requests.c.id == layout_tidy_request_pairs.c.request_id,
                    )
                    .where(
                        layout_tidy_requests.c.status.in_(("queued", "running")),
                        layout_tidy_request_pairs.c.status == "pending",
                    )
                    .order_by(
                        layout_tidy_requests.c.requested_at,
                        layout_tidy_request_pairs.c.project_id,
                        layout_tidy_request_pairs.c.variant,
                    )
                    .limit(1)
                )
            ).mappings().first()
            if pair is None:
                return None
            job = await self._enqueue_layout_job_on_conn(
                conn, pair["project_id"], pair["variant"], "tidy"
            )
            await conn.execute(
                update(layout_tidy_request_pairs)
                .where(
                    layout_tidy_request_pairs.c.request_id == pair["request_id"],
                    layout_tidy_request_pairs.c.project_id == pair["project_id"],
                    layout_tidy_request_pairs.c.variant == pair["variant"],
                )
                .values(job_id=job["id"], status="queued")
            )
            await conn.execute(
                update(layout_tidy_requests)
                .where(layout_tidy_requests.c.id == pair["request_id"])
                .values(status="running")
            )
            return await self._layout_tidy_request_summary(conn, pair["request_id"])

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

    async def layout_job_ledger(self, kind: str) -> dict[tuple[str, str], dict]:
        """Every ``(project, variant)`` this *kind* has a job for, in ONE statement.

        ``layout_jobs`` rows are never trimmed, so jobs of kind
        ``rules:<ENGINE_RULES_VERSION>`` are the engine-rules convergence
        ledger (reorganisation design §3.3). Each entry carries:

        - ``settled`` — a non-failed job exists (queued, running or done), so
          the pair is converged or already scheduled. A ``failed`` row is
          deliberately *not* convergence: the rebuild never happened.
        - ``in_flight`` — a job of this kind is queued or running somewhere.
        - ``failed`` — how many attempts failed, i.e. the pair's spent retry
          budget.
        - ``last_error`` — the most recently recorded failure, for the log.

        Reading the rows rather than aggregating in SQL keeps ``last_error``
        honest; a rules kind holds at most one settled row plus a capped
        handful of failures per pair, so the scan is small and bounded.
        """
        async with self._engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        select(
                            layout_jobs.c.project_id,
                            layout_jobs.c.variant,
                            layout_jobs.c.status,
                            layout_jobs.c.error,
                            layout_jobs.c.finished_at,
                        ).where(layout_jobs.c.kind == kind)
                    )
                )
                .mappings()
                .all()
            )
        ledger: dict[tuple[str, str], dict] = {}
        seen_at: dict[tuple[str, str], float] = {}
        for row in rows:
            key = (row["project_id"], row["variant"])
            entry = ledger.setdefault(
                key, {"settled": False, "in_flight": False, "failed": 0, "last_error": None}
            )
            if row["status"] == "failed":
                entry["failed"] += 1
                at = row["finished_at"] or 0.0
                if at >= seen_at.get(key, -1.0):
                    seen_at[key] = at
                    entry["last_error"] = row["error"]
            else:
                entry["settled"] = True
                if row["status"] in ("queued", "running"):
                    entry["in_flight"] = True
        return ledger

    async def published_layout_variants(self) -> set[tuple[str, str]]:
        """Every ``(project_id, variant)`` with a published layout, in one statement."""
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(project_layout_meta.c.project_id, project_layout_meta.c.variant)
                )
            ).all()
        return {(r[0], r[1]) for r in rows}

    async def reap_stale_layout_jobs(self, *, started_before: float, error: str) -> list[dict]:
        """Fail every job stuck ``running`` since before *started_before*.

        Nothing else ever resets a ``running`` row — ``next_layout_job``
        claims only ``queued`` — so a daemon killed mid-rebuild (shutdown
        waits 30 s while a tidy may run 60) would leave one forever. A stuck
        row wedges the engine-rules stand-down fleet-wide *and* counts as
        convergence for its pair, so it must not simply be ignored.
        """
        async with self._engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        update(layout_jobs)
                        .where(
                            layout_jobs.c.status == "running",
                            layout_jobs.c.started_at.is_not(None),
                            layout_jobs.c.started_at < started_before,
                        )
                        .values(status="failed", finished_at=time.time(), error=error)
                        .returning(
                            layout_jobs.c.id,
                            layout_jobs.c.project_id,
                            layout_jobs.c.variant,
                            layout_jobs.c.kind,
                            layout_jobs.c.started_at,
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

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
    async def load_layout_blocked_ids(self, project_id: str) -> set[str]:
        """Read only the blocked IDs needed for layout aggregate counters."""
        from src.database.tables import tasks

        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(tasks.c.id).where(
                    tasks.c.project_id == project_id,
                    tasks.c.is_blocked != 0,
                )
            )
            return set(result.scalars())

    async def load_project_snapshot(self, project_id: str):
        from src.database.queries.hierarchy_queries import (
            CONTAINER_KEY,
            CONTAINER_VALUE,
            PHASE_KEY,
        )
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
            # Container flags and phase orders come from ONE read: this
            # snapshot is loaded on the 5-second dirty path as well as by
            # ``full_layout``, so the metadata it needs is a single pass over
            # both keys rather than a statement each.
            containers: set[str] = set()
            phase_orders: dict[str, int] = {}
            if ids:
                for tid, key, raw in (
                    await conn.execute(
                        select(
                            task_metadata.c.task_id,
                            task_metadata.c.key,
                            task_metadata.c.value,
                        ).where(
                            task_metadata.c.task_id.in_(ids),
                            task_metadata.c.key.in_((CONTAINER_KEY, PHASE_KEY)),
                        )
                    )
                ).fetchall():
                    if key == CONTAINER_KEY:
                        if raw == CONTAINER_VALUE:
                            containers.add(tid)
                        continue
                    # PHASE_KEY: the tidy seed orders declared phases by
                    # their order (§3.2).
                    try:
                        order = json.loads(raw).get("order")
                    except (ValueError, AttributeError):
                        continue
                    if isinstance(order, int) and not isinstance(order, bool):
                        phase_orders[tid] = order
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
                phase_order=phase_orders.get(r[0]),
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
        """Rows for *task_ids*, keyed by task id.

        Chunked like every other id-list query here: callers pass sets that
        are bounded by the viewport in the common case but by a filter
        match set (or a forced-expansion closure) in the worst one, which
        can exceed the dialect's bound-parameter cap.
        """
        from src.database.tables import task_layouts

        ids = list(task_ids)
        if not ids:
            return {}
        out: dict = {}
        async with self._engine.begin() as conn:
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
    async def publish_layout(
        self,
        project_id,
        variant,
        write_set,
        *,
        consumed_seq,
        extent,
        captured_reflows: dict[str, int] | None = None,
        reflow_claims: Iterable[dict] = (),
    ) -> int:
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
        # A reflow worker may have computed while a Tidy/full layout was
        # publishing.  Materialise once because the preflight and the final
        # acknowledgement both need the exact same leased claims.
        reflow_claims = list(reflow_claims)

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

            # The meta-row lock serializes layout publications for this pair.
            # Do this before touching geometry: if a full layout already
            # acknowledged this lease, an older reflow must become a harmless
            # no-op rather than overwrite the Tidy's newer ordering.
            for claim in reflow_claims:
                valid = (
                    await conn.execute(
                        select(layout_reflow_requests.c.scope_key).where(
                            layout_reflow_requests.c.project_id == project_id,
                            layout_reflow_requests.c.variant == variant,
                            layout_reflow_requests.c.scope_key == claim["scope_key"],
                            layout_reflow_requests.c.state == "running",
                            layout_reflow_requests.c.claimed_generation == claim["generation"],
                            layout_reflow_requests.c.lease_token == claim["lease_token"],
                        )
                    )
                ).scalar_one_or_none()
                if valid is None:
                    return meta["layout_version"]

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
                # Keep parameter construction and executemany bounded; a
                # 10k-row publication otherwise starves health on this loop.
                # All batches share this transaction and its meta-row lock.
                for chunk in _chunks(write_set.upserts, LAYOUT_WRITE_BATCH_SIZE):
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
                        for r in chunk
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
                    for r in chunk:
                        touched[r.task_id] = (r.abs_x, r.abs_y, r.w, r.h)
                    await asyncio.sleep(0)

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
                for chunk in _chunks(list(touched), LAYOUT_WRITE_BATCH_SIZE):
                    await conn.execute(
                        delete(cells).where(
                            cells.c.project_id == project_id,
                            cells.c.variant == variant,
                            cells.c.task_id.in_(chunk),
                        )
                    )
                cell_values = (
                    {
                        "project_id": project_id,
                        "variant": variant,
                        "cell_x": cx,
                        "cell_y": cy,
                        "task_id": tid,
                    }
                    for tid, (bx, by, bw, bh) in touched.items()
                    for cx, cy in cells_for_box(bx, by, bw, bh)
                )
                while crow := list(islice(cell_values, LAYOUT_WRITE_BATCH_SIZE)):
                    await conn.execute(insert(cells), crow)
                    await asyncio.sleep(0)

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

            # The active incremental batch adds requests only when it proved
            # that normal work merely removes a finished leaf and refreshes
            # aggregates.  Keeping this upsert in the geometry transaction
            # makes a successful hole-creating publication inseparable from
            # its eventual compaction request.
            if write_set.reflow_scopes:
                await self._enqueue_layout_reflows_on_conn(
                    conn, project_id, variant, write_set.reflow_scopes
                )
            if captured_reflows or reflow_claims:
                await self._ack_layout_reflows_on_conn(
                    conn,
                    project_id,
                    variant,
                    captured=captured_reflows or {},
                    claims=reflow_claims,
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

    async def load_active_container_rows(self, project_id, variant) -> dict:
        """Container candidates for `active_expansion` (design A3), one statement.

        Every ``container``-kind row that could possibly be selected: any
        with ``agg_running > 0``, or a root (``container_id IS NULL``) with
        ``agg_active > 0``. Both `active_expansion` branches read from this
        one set -- it is a superset of whichever branch actually applies,
        never a per-branch query -- so `auto_expand` costs exactly one
        statement. A finished container under ``variant="active"`` is a
        ``"stub"`` row and is excluded by the ``kind`` filter, same as
        `active_expansion` itself.
        """
        from sqlalchemy import and_, or_
        from src.database.tables import task_layouts

        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts).where(
                    task_layouts.c.project_id == project_id,
                    task_layouts.c.variant == variant,
                    task_layouts.c.kind == "container",
                    or_(
                        task_layouts.c.agg_running > 0,
                        and_(
                            task_layouts.c.container_id.is_(None),
                            task_layouts.c.agg_active > 0,
                        ),
                    ),
                )
            )
            return {m["task_id"]: self._row_from_mapping(m) for m in res.mappings()}

    async def load_rows_for_containers(self, project_id, variant, container_ids):
        """Rows directly inside any of *container_ids*, joined to their tasks.

        ``None`` in the list selects the project roots (``container_id IS
        NULL``).  This is what the ``list`` endpoint pages over: its cost is
        bounded by the number of open containers, so it never has to load a
        whole project (design §5.3).
        """
        from sqlalchemy import or_

        from src.database.tables import task_layouts, tasks

        ids = list(dict.fromkeys(container_ids))
        if not ids:
            return {}
        want_root = None in ids
        named = [c for c in ids if c is not None]
        batches = _chunks(named) or ([[]] if want_root else [])
        cols = [getattr(tasks.c, f) for f in self._TASK_FIELDS]
        out: dict = {}
        async with self._engine.begin() as conn:
            for i, chunk in enumerate(batches):
                clauses = []
                if chunk:
                    clauses.append(task_layouts.c.container_id.in_(chunk))
                # The roots ride along with the first batch only.
                if want_root and i == 0:
                    clauses.append(task_layouts.c.container_id.is_(None))
                if not clauses:
                    continue
                res = await conn.execute(
                    select(task_layouts, *cols)
                    .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                    .where(
                        task_layouts.c.project_id == project_id,
                        task_layouts.c.variant == variant,
                        or_(*clauses),
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

    async def load_paths_by_prefixes(self, project_id, variant, prefixes) -> dict[str, str]:
        """Light form of `load_rows_by_prefixes`: task_id -> path only.

        Callers that only need to know which task owns which path (e.g.
        `owner_map`) don't need the other dozen layout columns for what can
        be ~1,000 hidden rows.
        """
        from sqlalchemy import or_
        from src.database.tables import task_layouts

        if not prefixes:
            return {}
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts.c.task_id, task_layouts.c.path).where(
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
            return {m["task_id"]: m["path"] for m in res.mappings()}

    async def load_paths_by_ids(self, project_id, variant, task_ids) -> dict[str, str]:
        """Light form of `load_layout_rows`: task_id -> path only.

        The tiles filter path needs nothing but paths for a match set (to
        derive the forced-expansion ancestors); membership testing needs no
        row at all.  A match set is unbounded by the viewport, so loading a
        full `LayoutRow` per match is the one place the endpoint's cost
        tracked the project rather than the screen.
        """
        from src.database.tables import task_layouts

        ids = list(task_ids)
        if not ids:
            return {}
        out: dict[str, str] = {}
        async with self._engine.begin() as conn:
            for chunk in _chunks(ids):
                res = await conn.execute(
                    select(task_layouts.c.task_id, task_layouts.c.path).where(
                        task_layouts.c.project_id == project_id,
                        task_layouts.c.variant == variant,
                        task_layouts.c.task_id.in_(chunk),
                    )
                )
                out.update({m["task_id"]: m["path"] for m in res.mappings()})
        return out

    async def load_edges_touching(self, task_ids, *, owners=None):
        """Dependency rows with an endpoint in ``task_ids``.

        ``owners`` maps each id to the node an edge endpoint is *drawn* at —
        itself for a visible node, its collapsed container for a node hidden
        inside one (what ``view.owner_map`` returns).  When it is supplied the
        database drops every row whose two endpoints share an owner, which is
        exactly the ``f == t: continue`` arm of :func:`view.remap_edges`, and
        restricts ``dep_type`` to ``DRAWN_TYPES`` the way that function's other
        early ``continue`` does.  See :data:`CROSSING_EDGES_SQL`.

        That filter is not an optimisation of the margins: a collapsed
        container owns every edge *inside* its subtree, and those edges can
        never be drawn.  A fully collapsed view of the §9 reference project
        reads 9,970 rows this way and draws 50 of them — the other 99.5% are
        intra-container edges that cross the wire only to be discarded in
        Python.  With ``owners`` the same request reads 50 rows (67ms -> 11ms
        measured), and it is one statement rather than one per id chunk,
        because the owner map is passed as an array rather than the ids being
        split across ``IN`` lists.
        """
        from sqlalchemy import or_
        from src.database.tables import task_dependencies as td

        ids = list(task_ids)
        if not ids:
            return []
        if owners is not None:
            return await self._load_crossing_edges(ids, owners)
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

    async def _load_crossing_edges(self, ids: list[str], owners) -> list[tuple]:
        """Edges touching ``ids`` whose two endpoints are drawn at different nodes.

        The owner map is handed to the database as a pair of arrays and
        joined to both endpoints, so the comparison the endpoint would have
        made in Python happens before the rows are sent.  An endpoint outside
        ``ids`` has no owner row and stands for itself — the same fallback
        ``view.remap_edges`` applies when it records an orphan — which is why
        both joins are outer ones and the comparison coalesces.
        """
        stmt = crossing_edges_statement(ids, owners)
        async with self._engine.begin() as conn:
            res = await conn.execute(stmt)
            rows = {tuple(r) for r in res.fetchall()}
        return sorted(rows, key=lambda r: (r[0], r[1], r[2]))

    @staticmethod
    def _match_conditions(project_id, variant, *, q, status) -> list:
        """WHERE terms shared by the id-only and row-returning filter reads."""
        from sqlalchemy import func, or_
        from src.database.tables import task_layouts, tasks

        conds = [task_layouts.c.project_id == project_id, task_layouts.c.variant == variant]
        if q:
            # The needle is user text and may contain LIKE metacharacters
            # ('%', '_', '\'), which must match literally rather than act as
            # wildcards.
            needle = f"%{like_escape(q.lower())}%"
            conds.append(
                or_(
                    func.lower(tasks.c.title).like(needle, escape="\\"),
                    func.lower(tasks.c.id).like(needle, escape="\\"),
                )
            )
        if status:
            conds.append(tasks.c.status == status)
        return conds

    async def load_matching_ids(self, project_id, variant, *, q, status):
        from src.database.tables import task_layouts, tasks

        conds = self._match_conditions(project_id, variant, q=q, status=status)
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts.c.task_id)
                .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                .where(*conds)
            )
            return {r[0] for r in res.fetchall()}

    async def load_matching_rows_ordered(
        self, project_id, variant, *, q, status, limit
    ) -> tuple[list, bool]:
        """The first *limit* matching rows in reading order, plus a flag.

        Filter, ``ORDER BY abs_y, abs_x, task_id`` and ``LIMIT limit + 1``
        all happen in SQL: ``locate`` used to fetch every matching id and
        then every matching row before slicing in Python, so a broad needle
        on a big project paid for the whole project to answer a 200-hit
        page.  The extra row is how truncation is detected.

        Returns ``(rows, truncated)`` — at most *limit* ``LayoutRow``s.
        """
        from src.database.tables import task_layouts, tasks

        conds = self._match_conditions(project_id, variant, q=q, status=status)
        async with self._engine.begin() as conn:
            res = await conn.execute(
                select(task_layouts)
                .select_from(task_layouts.join(tasks, tasks.c.id == task_layouts.c.task_id))
                .where(*conds)
                .order_by(task_layouts.c.abs_y, task_layouts.c.abs_x, task_layouts.c.task_id)
                .limit(limit + 1)
            )
            rows = [self._row_from_mapping(m) for m in res.mappings()]
        return rows[:limit], len(rows) > limit
