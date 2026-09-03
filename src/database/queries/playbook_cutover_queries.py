"""Append-only audit of the Playbook V1 → V2 cutover.

Playbook V2 Package 7 §6
(``docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md``).

Two reads and one write on the audit table, and deliberately no update or
delete: the point of
``playbook_cutover_events`` is that an operator cannot rewrite the record of
which runtime the fleet was on at a given moment.  ``playbook_cutover_switch``
and ``playbook_cutover_window_status`` both read it — the second compares it
against live config, so a runtime hand-edited around the gate shows up as a
disagreement rather than disappearing.

Follows the same mixin pattern as the other query modules — expects
``self._engine`` to be set by the adapter class.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import cast, func, insert, select
from sqlalchemy.dialects.postgresql import JSONB

from src.database.tables import (
    CUTOVER_EVENT_KINDS,
    playbook_cutover_events,
    playbook_pending_events,
    playbook_step_receipts,
    playbook_v2_runs,
    playbook_waits,
)

logger = logging.getLogger(__name__)


class PlaybookCutoverQueryMixin:
    """Query mixin for ``playbook_cutover_events``.  Expects ``self._engine``."""

    async def append_playbook_cutover_event(
        self,
        *,
        kind: str,
        actor: str,
        reason: str,
        detail: dict[str, Any] | None = None,
        at: float | None = None,
    ) -> dict[str, Any]:
        """Append one audit row and return it.

        ``kind`` is validated here as well as by the check constraint so a
        typo fails with the closed set named rather than with a driver-level
        integrity error the caller cannot act on.
        """
        if kind not in CUTOVER_EVENT_KINDS:
            raise ValueError(
                f"unknown cutover event kind {kind!r}; expected one of "
                + ", ".join(CUTOVER_EVENT_KINDS)
            )
        row = {
            "event_id": uuid.uuid4().hex,
            "kind": kind,
            "at": float(at if at is not None else time.time()),
            "actor": actor,
            "reason": reason,
            "detail": json.dumps(detail or {}, sort_keys=True),
        }
        async with self._engine.begin() as conn:
            await conn.execute(insert(playbook_cutover_events).values(**row))
        return {**row, "detail": dict(detail or {})}

    async def list_playbook_cutover_events(
        self,
        kind: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Cutover events oldest first — the order an audit is read in."""
        stmt = select(playbook_cutover_events).order_by(
            playbook_cutover_events.c.at.asc(),
            playbook_cutover_events.c.event_id.asc(),
        )
        if kind is not None:
            stmt = stmt.where(playbook_cutover_events.c.kind == kind)
        stmt = stmt.limit(limit)
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [_row_to_event(row) for row in rows]

    async def latest_playbook_cutover_event(self, kind: str) -> dict[str, Any] | None:
        """The most recent event of one kind, or ``None``."""
        stmt = (
            select(playbook_cutover_events)
            .where(playbook_cutover_events.c.kind == kind)
            .order_by(
                playbook_cutover_events.c.at.desc(),
                playbook_cutover_events.c.event_id.desc(),
            )
            .limit(1)
        )
        async with self._engine.begin() as conn:
            row = (await conn.execute(stmt)).mappings().fetchone()
        return _row_to_event(row) if row else None

    # ------------------------------------------------------------------
    # Rollback-window evidence (§3.5) — aggregates over ``[since, now]``
    # ------------------------------------------------------------------
    #
    # Every read here is bounded by a durable timestamp the caller took from
    # the ``switched_to_v2`` audit row, never by an in-memory counter: the
    # window is 72 hours long and the daemon will restart inside it.

    async def count_v2_runs_by_playbook(self, since: float) -> dict[str, int]:
        """V2 runs started at or after *since*, per playbook — coverage and volume."""
        stmt = (
            select(playbook_v2_runs.c.playbook_id, func.count())
            .where(playbook_v2_runs.c.started_at >= since)
            .group_by(playbook_v2_runs.c.playbook_id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return {str(playbook_id): int(count) for playbook_id, count in rows}

    def _snapshot_received_at(self):
        """``snapshot.event._received_at`` as a column, per dialect.

        The stamp is written by the V2 dispatch entry (``core.py``) and lives
        inside the canonical snapshot JSON; extracting it in SQL keeps this a
        range scan on ``started_at`` rather than a parse of every snapshot.
        """
        column = playbook_v2_runs.c.snapshot
        if self._engine.dialect.name == "postgresql":
            return func.jsonb_extract_path_text(cast(column, JSONB), "event", "_received_at")
        return func.json_extract(column, "$.event._received_at")

    async def v2_dispatch_latencies_ms(self, since: float, *, limit: int = 5000) -> list[float]:
        """``started_at - event._received_at`` for stamped runs since *since*.

        Runs without a stamp (a replay, a run created by hand) contribute no
        sample rather than a zero — measure 6 fails closed on an empty sample.
        """
        stmt = (
            select(playbook_v2_runs.c.started_at, self._snapshot_received_at())
            .where(playbook_v2_runs.c.started_at >= since)
            .order_by(playbook_v2_runs.c.started_at.desc())
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        latencies: list[float] = []
        for started_at, received_at in rows:
            try:
                received = float(received_at)
            except (TypeError, ValueError):
                continue
            latencies.append(max(0.0, (float(started_at) - received) * 1000.0))
        return latencies

    async def wait_resume_latencies_ms(self, since: float, *, limit: int = 5000) -> list[float]:
        """``claimed_at - received_at`` of the causing event, for claimed waits.

        Joined through the run to the wait inbox row (``reason =
        wait_registration``) that carries the event's arrival time.  Timer,
        human and agent-task claims have no causing event and no sample.
        """
        stmt = (
            select(playbook_waits.c.claimed_at, playbook_pending_events.c.received_at)
            .select_from(
                playbook_waits.join(
                    playbook_v2_runs, playbook_v2_runs.c.run_id == playbook_waits.c.run_id
                ).join(
                    playbook_pending_events,
                    (playbook_pending_events.c.event_id == playbook_waits.c.claimed_event_id)
                    & (playbook_pending_events.c.playbook_id == playbook_v2_runs.c.playbook_id)
                    & (playbook_pending_events.c.reason == "wait_registration"),
                )
            )
            .where(
                playbook_waits.c.state == "claimed",
                playbook_waits.c.claimed_event_id.is_not(None),
                playbook_waits.c.claimed_at.is_not(None),
                playbook_waits.c.claimed_at >= since,
            )
            .order_by(playbook_waits.c.claimed_at.desc())
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return [
            max(0.0, (float(claimed_at) - float(received_at)) * 1000.0)
            for claimed_at, received_at in rows
        ]

    async def count_step_receipts_since(self, since: float) -> list[dict[str, Any]]:
        """Receipts since *since*, grouped by ``(step_kind, receipt_kind, outcome, error_code)``.

        One query feeds measures 5 (boundaries), 8, 9, 10 and 11.
        """
        stmt = (
            select(
                playbook_step_receipts.c.step_kind,
                playbook_step_receipts.c.receipt_kind,
                playbook_step_receipts.c.outcome,
                playbook_step_receipts.c.error_code,
                func.count(),
            )
            .where(playbook_step_receipts.c.started_at >= since)
            .group_by(
                playbook_step_receipts.c.step_kind,
                playbook_step_receipts.c.receipt_kind,
                playbook_step_receipts.c.outcome,
                playbook_step_receipts.c.error_code,
            )
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).fetchall()
        return [
            {
                "step_kind": step_kind,
                "receipt_kind": receipt_kind,
                "outcome": outcome,
                "error_code": error_code,
                "count": int(count),
            }
            for step_kind, receipt_kind, outcome, error_code, count in rows
        ]

    async def agent_task_wait_orphans(self, now: float) -> list[dict[str, Any]]:
        """Active agent-task waits older than twice their own timeout (measure 10).

        The timeout is ``deadline_at - created_at``; a wait with no deadline
        has nothing to double and is not counted here.
        """
        timeout = playbook_waits.c.deadline_at - playbook_waits.c.created_at
        stmt = (
            select(
                playbook_waits.c.wait_id,
                playbook_waits.c.run_id,
                playbook_waits.c.step_id,
                playbook_waits.c.created_at,
                playbook_waits.c.deadline_at,
            )
            .where(
                playbook_waits.c.kind == "agent_task",
                playbook_waits.c.state == "active",
                playbook_waits.c.deadline_at.is_not(None),
                (playbook_waits.c.deadline_at + timeout) <= now,
            )
            .order_by(playbook_waits.c.created_at, playbook_waits.c.wait_id)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [
            {
                "wait_id": row["wait_id"],
                "run_id": row["run_id"],
                "step_id": row["step_id"],
                "created_at": float(row["created_at"]),
                "deadline_at": float(row["deadline_at"]),
            }
            for row in rows
        ]

    async def agent_task_cancellations_since(
        self, since: float, *, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Cancelled agent-task steps since *since*, by run (measure 11, reported)."""
        stmt = (
            select(
                playbook_step_receipts.c.run_id,
                playbook_step_receipts.c.step_id,
                playbook_step_receipts.c.started_at,
                playbook_step_receipts.c.cancelled_at,
            )
            .where(
                playbook_step_receipts.c.step_kind == "agent_task",
                playbook_step_receipts.c.outcome == "cancelled",
                playbook_step_receipts.c.started_at >= since,
            )
            .order_by(playbook_step_receipts.c.started_at.desc())
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [
            {
                "run_id": row["run_id"],
                "step_id": row["step_id"],
                "started_at": float(row["started_at"]),
                "cancelled_at": row["cancelled_at"],
            }
            for row in rows
        ]

    async def pending_event_summary(
        self, *, reasons: Collection[str] | None = None
    ) -> dict[str, Any]:
        """Unresolved pending events: how many, and the oldest arrival (14, 15)."""
        stmt = select(func.count(), func.min(playbook_pending_events.c.received_at)).where(
            playbook_pending_events.c.resolved_at.is_(None)
        )
        if reasons is not None:
            stmt = stmt.where(playbook_pending_events.c.reason.in_(list(reasons)))
        async with self._engine.connect() as conn:
            count, oldest = (await conn.execute(stmt)).one()
        return {
            "count": int(count or 0),
            "oldest_received_at": float(oldest) if oldest is not None else None,
        }


def _row_to_event(row) -> dict[str, Any]:
    """One row as a plain dict, with ``detail`` decoded.

    A ``detail`` that will not parse is surfaced as ``{"_unparsed": ...}``
    rather than raising: an audit reader must still be able to see that the
    event happened, and when.
    """
    raw = row["detail"] or "{}"
    try:
        detail = json.loads(raw)
        if not isinstance(detail, dict):
            detail = {"_unparsed": raw}
    except (TypeError, ValueError):
        detail = {"_unparsed": raw}
    return {
        "event_id": row["event_id"],
        "kind": row["kind"],
        "at": float(row["at"]),
        "actor": row["actor"],
        "reason": row["reason"],
        "detail": detail,
    }
