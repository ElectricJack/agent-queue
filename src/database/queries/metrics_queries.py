"""Reads and writes for the fleet metrics time series.

Two kinds of method live here, and the split matters:

* the **aggregates** the sampler folds into one sample — every one of them is
  a single grouped statement over an indexed column, because they run once a
  second and must stay invisible next to twenty live agents;
* the **store** for ``metrics_samples`` — an idempotent upsert keyed on
  (resolution, bucket) plus the range read the ``/api/metrics/series`` route
  and the retention sweep use.

Nothing here interprets a sample.  Shape and roll-up arithmetic belong to
:mod:`src.metrics.sampler`; this module only moves rows.
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import Float, and_, case, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from src.database.tables import (
    metrics_samples,
    sessions,
    subagent_events,
    system_config,
    task_completion_records,
    tasks,
    token_ledger,
    workspaces,
)

#: Session states that count as "an agent is up right now".  ``draining`` is
#: deliberately excluded from the running total but reported separately —
#: a draining session still holds a slot but is no longer taking work.
LIVE_SESSION_STATES = ("starting", "running")

#: Task statuses a live worker can hold.  Mirrors
#: ``src.agents.subagents._ACTIVE_TASKS`` — kept as strings here because this
#: layer speaks columns, not enums.
ACTIVE_TASK_STATUSES = ("ASSIGNED", "IN_PROGRESS", "WAITING_INPUT")

#: ``system_config`` key holding the durable daemon-start counter.
_DAEMON_STARTS_KEY = "metrics.daemon_starts"

# ---------------------------------------------------------------------------
# Statement builders.  Separated from the readers so several can share one
# connection without each opening its own.
# ---------------------------------------------------------------------------


def _live_sessions_stmt():
    return (
        select(
            sessions.c.state,
            sessions.c.harness,
            sessions.c.profile_id,
            sessions.c.lifecycle,
            func.count().label("count"),
        )
        .where(sessions.c.state.in_((*LIVE_SESSION_STATES, "draining")))
        .group_by(
            sessions.c.state,
            sessions.c.harness,
            sessions.c.profile_id,
            sessions.c.lifecycle,
        )
    )


def _task_counts_stmt():
    return select(tasks.c.status, func.count().label("count")).group_by(tasks.c.status)


def _live_session_ids_stmt():
    return select(sessions.c.id, sessions.c.name, sessions.c.hooks_provisioned).where(
        sessions.c.state.in_((*LIVE_SESSION_STATES, "draining"))
    )


def _native_fold_stmt(session_ids: list[str]):
    starts = func.sum(case((subagent_events.c.event == "start", 1), else_=0))
    stops = func.sum(case((subagent_events.c.event == "stop", 1), else_=0))
    return (
        select(
            subagent_events.c.session_id,
            starts.label("starts"),
            stops.label("stops"),
        )
        .where(subagent_events.c.session_id.in_(session_ids))
        .group_by(subagent_events.c.session_id)
    )


def _delegated_stmt(session_ids: list[str]):
    return (
        select(tasks.c.created_by_id, func.count().label("count"))
        .where(
            and_(
                tasks.c.created_by_kind == "session",
                tasks.c.created_by_id.in_(session_ids),
                tasks.c.status.in_(ACTIVE_TASK_STATUSES),
                tasks.c.assigned_agent_id.isnot(None),
            )
        )
        .group_by(tasks.c.created_by_id)
    )


#: The four token columns that are individually attributable, and the total
#: they are measured against.  ``tokens_used`` includes cache volume, so any
#: reader that sums only ``input``/``output`` reports a small fraction of a
#: cached session's real traffic and writes the rest off as unattributed.
_TOKEN_COLUMNS = (
    ("input_tokens", token_ledger.c.input_tokens),
    ("output_tokens", token_ledger.c.output_tokens),
    ("cache_read_tokens", token_ledger.c.cache_read_tokens),
    ("cache_write_tokens", token_ledger.c.cache_write_tokens),
    ("tokens_used", token_ledger.c.tokens_used),
)


def _token_window_stmt(since_ts: float, recent_ts: float):
    """Per-model token sums over ``[since_ts, now)``, and over ``[recent_ts, now)``.

    Both windows come out of one scan.  The wide window is what the reported
    rate is computed from — a 60-second window sampled once a second reads 0
    for most seconds and spikes on the tick after a harness flushes a turn's
    usage, which looks exactly like a broken chart.  The narrow window is
    carried alongside under ``recent_*`` so the raw, unsmoothed reading is
    still available rather than being silently replaced.
    """
    columns = [token_ledger.c.model]
    for name, column in _TOKEN_COLUMNS:
        columns.append(func.coalesce(func.sum(column), 0).label(name))
        columns.append(
            func.coalesce(
                func.sum(case((token_ledger.c.timestamp >= recent_ts, column), else_=0)),
                0,
            ).label(f"recent_{name}")
        )
    return (
        select(*columns)
        .where(token_ledger.c.timestamp >= since_ts)
        .group_by(token_ledger.c.model)
    )


def _subagent_spawn_stmt(since_ts: float):
    """Sub-agent *starts* per session since *since_ts*, whatever their session.

    Deliberately not scoped to live sessions, unlike :func:`_native_fold_stmt`.
    The open-children fold answers "how many are running right now", which
    under pool lifecycles is almost always ~0 because the sessions that spawned
    them are already gone — while the events themselves are still right there
    in the table.  Served by ``idx_subagent_events_occurred``.
    """
    return (
        select(subagent_events.c.session_id, func.count().label("starts"))
        .where(
            and_(
                subagent_events.c.event == "start",
                subagent_events.c.occurred_at >= since_ts,
            )
        )
        .group_by(subagent_events.c.session_id)
    )


def _completion_window_stmt(since_ts: float):
    with_pr = func.sum(case((task_completion_records.c.pr_url.isnot(None), 1), else_=0))
    return select(
        func.count().label("completions"), with_pr.label("with_pr")
    ).where(task_completion_records.c.completed_at >= since_ts)


def _slot_counts_stmt():
    locked = func.sum(case((workspaces.c.locked_by_task_id.isnot(None), 1), else_=0))
    return select(func.count().label("total"), locked.label("locked")).where(
        workspaces.c.slot_index.isnot(None)
    )



class MetricsQueryMixin:
    """Query mixin for the metrics sampler.  Expects ``self._engine``."""

    # -- live fleet aggregates --------------------------------------------
    #
    # Grouped into two readers, not seven, because SQLite runs on
    # ``NullPool``: every ``connect()`` builds a fresh connection and replays
    # its PRAGMA setup, ~3.7 ms on the reference box.  At one tick a second
    # the connection count, not the queries, is the cost — so each tier
    # opens exactly one connection and runs all of its statements on it.

    async def metrics_live_counts(self) -> dict:
        """The per-second tier: sessions by state and tasks by status.

        Returns ``{"sessions": [...], "tasks": {status: count}}``.  Both
        statements are served by existing indexes over small tables.
        """
        async with self._engine.connect() as conn:
            sessions_rows = (
                await conn.execute(_live_sessions_stmt())
            ).mappings().all()
            task_rows = (await conn.execute(_task_counts_stmt())).mappings().all()
        return {
            "sessions": [dict(row) for row in sessions_rows],
            "tasks": {row["status"]: int(row["count"] or 0) for row in task_rows},
        }

    async def metrics_slow_snapshot(
        self,
        since_ts: float,
        *,
        recent_ts: float | None = None,
        spawn_since_ts: float | None = None,
    ) -> dict:
        """The tier that range-scans append-only tables.

        Returns ``{"live": [(session_id, name, hooks)], "native": {...},
        "delegated": {...}, "slots": {...}, "ledger": [...], "spawned": {...}}``.
        The sub-agent and delegation folds are scoped to the live session ids
        so they read an index rather than the whole event table; the ledger
        window is served by ``idx_token_ledger_timestamp``.

        ``since_ts`` bounds the ledger window.  ``recent_ts`` is the narrower
        window carried alongside it (default: the trailing minute of that same
        window).  ``spawn_since_ts`` bounds the sub-agent *start* count, which
        is not scoped to live sessions at all — it defaults to ``since_ts``,
        but the sampler passes a wider window because a spawn rate measured
        over five minutes of a pool fleet is mostly zeros.
        """
        recent = since_ts if recent_ts is None else recent_ts
        spawn_since = since_ts if spawn_since_ts is None else spawn_since_ts
        async with self._engine.connect() as conn:
            live_rows = (await conn.execute(_live_session_ids_stmt())).mappings().all()
            live = [
                (row["id"], row["name"], bool(row["hooks_provisioned"]))
                for row in live_rows
            ]
            ids = [row[0] for row in live]

            native: dict[str, int] = {}
            delegated: dict[str, int] = {}
            if ids:
                for row in (
                    await conn.execute(_native_fold_stmt(ids))
                ).mappings().all():
                    native[row["session_id"]] = max(
                        0, int(row["starts"] or 0) - int(row["stops"] or 0)
                    )
                for row in (
                    await conn.execute(_delegated_stmt(ids))
                ).mappings().all():
                    delegated[row["created_by_id"]] = int(row["count"] or 0)

            slot_row = (await conn.execute(_slot_counts_stmt())).mappings().one()
            ledger_rows = (
                await conn.execute(_token_window_stmt(since_ts, recent))
            ).mappings().all()
            spawn_rows = (
                await conn.execute(_subagent_spawn_stmt(spawn_since))
            ).mappings().all()

        return {
            "live": live,
            "native": native,
            "delegated": delegated,
            "spawned": {
                row["session_id"]: int(row["starts"] or 0) for row in spawn_rows
            },
            "slots": {
                "total": int(slot_row["total"] or 0),
                "locked": int(slot_row["locked"] or 0),
            },
            "ledger": [
                {
                    "model": row["model"],
                    **{
                        key: int(row[key] or 0)
                        for name, _ in _TOKEN_COLUMNS
                        for key in (name, f"recent_{name}")
                    },
                }
                for row in ledger_rows
            ],
        }

    async def metrics_completion_window(self, since_ts: float) -> dict[str, int]:
        """``{"completions": n, "with_pr": n}`` for completions since *since_ts*.

        ``with_pr`` is the closest durable proxy for merge-sweep throughput:
        a completion record carrying a PR url is a task whose branch reached
        a pull request.  Served by ``idx_task_completion_records_completed_at``.
        """
        async with self._engine.connect() as conn:
            row = (await conn.execute(_completion_window_stmt(since_ts))).mappings().one()
        return {
            "completions": int(row["completions"] or 0),
            "with_pr": int(row["with_pr"] or 0),
        }

    # -- the sample store --------------------------------------------------

    async def write_metrics_samples(
        self, resolution: str, rows: list[tuple[float, dict[str, Any]]]
    ) -> None:
        """Upsert several samples in one transaction.

        The sampler batches its per-second rows through here rather than
        committing each one: on SQLite a commit is an fsync (~60 ms on the
        reference box), and one fsync for five seconds of samples costs the
        same as one for a single second.  Idempotent per row, so a batch that
        overlaps an already-written bucket updates it.
        """
        if not rows:
            return
        fresh: list[dict] = []
        async with self._engine.begin() as conn:
            for bucket_ts, payload in rows:
                body = json.dumps(payload, separators=(",", ":"))
                result = await conn.execute(
                    update(metrics_samples)
                    .where(
                        and_(
                            metrics_samples.c.resolution == resolution,
                            metrics_samples.c.bucket_ts == float(bucket_ts),
                        )
                    )
                    .values(payload=body)
                )
                if not result.rowcount:
                    fresh.append(
                        {
                            "resolution": resolution,
                            "bucket_ts": float(bucket_ts),
                            "payload": body,
                        }
                    )
            if fresh:
                await conn.execute(insert(metrics_samples), fresh)

    async def write_metrics_sample(
        self, resolution: str, bucket_ts: float, payload: dict[str, Any]
    ) -> None:
        """Upsert one sample.  Idempotent on (resolution, bucket_ts).

        Written as insert-then-update-on-conflict rather than a dialect
        ``ON CONFLICT``: this file has to run identically on SQLite and
        PostgreSQL, and the collision is rare enough (a re-run roll-up, a
        double tick across a clock adjustment) that the extra round trip
        never happens on the hot path.
        """
        body = json.dumps(payload, separators=(",", ":"))
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(metrics_samples).values(
                        resolution=resolution, bucket_ts=float(bucket_ts), payload=body
                    )
                )
            return
        except IntegrityError:
            pass
        async with self._engine.begin() as conn:
            await conn.execute(
                update(metrics_samples)
                .where(
                    and_(
                        metrics_samples.c.resolution == resolution,
                        metrics_samples.c.bucket_ts == float(bucket_ts),
                    )
                )
                .values(payload=body)
            )

    async def read_metrics_samples(
        self,
        resolution: str,
        from_ts: float,
        to_ts: float,
        *,
        limit: int = 10_000,
    ) -> list[dict]:
        """Ascending ``[{"bucket_ts": ..., **payload}]`` in ``[from_ts, to_ts]``.

        The payload is merged over ``bucket_ts`` rather than nested under it
        so a stored sample and a live WebSocket tick have the same shape on
        the client — the chart appends one to the other without a branch.
        """
        stmt = (
            select(metrics_samples.c.bucket_ts, metrics_samples.c.payload)
            .where(
                and_(
                    metrics_samples.c.resolution == resolution,
                    metrics_samples.c.bucket_ts >= float(from_ts),
                    metrics_samples.c.bucket_ts <= float(to_ts),
                )
            )
            .order_by(metrics_samples.c.bucket_ts)
            .limit(max(1, int(limit)))
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        out: list[dict] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            payload["ts"] = float(row["bucket_ts"])
            out.append(payload)
        return out

    async def prune_metrics_samples(self, horizons: dict[str, float]) -> dict[str, int]:
        """Drop each tier's samples older than its horizon, in one transaction.

        ``horizons`` is ``{resolution: before_ts}``; a tier the caller omits
        is left alone.  One transaction rather than one per tier for the same
        reason the writes are batched — on SQLite each commit is an fsync,
        and the sweep runs on a tick that is already doing the roll-up.
        """
        if not horizons:
            return {}
        deleted: dict[str, int] = {}
        async with self._engine.begin() as conn:
            for resolution, before_ts in horizons.items():
                result = await conn.execute(
                    delete(metrics_samples).where(
                        and_(
                            metrics_samples.c.resolution == resolution,
                            metrics_samples.c.bucket_ts < float(before_ts),
                        )
                    )
                )
                deleted[resolution] = int(result.rowcount or 0)
        return deleted

    async def bump_daemon_start_count(self) -> int:
        """Increment and return the durable count of daemon starts.

        ``system_config`` is a plain key/value table with no other writer, so
        the read-modify-write is done inside one transaction.  This is how the
        Metrics tab can show a restart count that survives the restart it is
        counting — an in-process counter would read 0 forever.
        """
        async with self._engine.begin() as conn:
            current = (
                await conn.execute(
                    select(system_config.c.value).where(
                        system_config.c.key == _DAEMON_STARTS_KEY
                    )
                )
            ).scalar()
            try:
                count = int(current) + 1
            except (TypeError, ValueError):
                count = 1
            if current is None:
                await conn.execute(
                    insert(system_config).values(
                        key=_DAEMON_STARTS_KEY, value=str(count)
                    )
                )
            else:
                await conn.execute(
                    update(system_config)
                    .where(system_config.c.key == _DAEMON_STARTS_KEY)
                    .values(value=str(count))
                )
        return count

    async def oldest_metrics_bucket(self, resolution: str) -> float | None:
        """Oldest bucket at *resolution*, or ``None`` when the tier is empty.

        The roll-up starts a cold backfill here rather than at the retention
        horizon: with a 30-day horizon and no minutes stored yet, walking
        from the horizon means crawling 720 empty hour-buckets before
        reaching any data.
        """
        stmt = select(func.min(metrics_samples.c.bucket_ts).cast(Float)).where(
            metrics_samples.c.resolution == resolution
        )
        async with self._engine.connect() as conn:
            value = (await conn.execute(stmt)).scalar()
        return float(value) if value is not None else None

    async def latest_metrics_bucket(self, resolution: str) -> float | None:
        """Newest bucket at *resolution*, or ``None`` when the tier is empty.

        The roll-up uses this as its resume point after a daemon restart, so
        a restart backfills the buckets it slept through instead of leaving a
        hole in the 1-minute series.
        """
        stmt = select(func.max(metrics_samples.c.bucket_ts).cast(Float)).where(
            metrics_samples.c.resolution == resolution
        )
        async with self._engine.connect() as conn:
            value = (await conn.execute(stmt)).scalar()
        return float(value) if value is not None else None


def now() -> float:
    """Indirection point for tests that need a deterministic clock."""
    return time.time()
