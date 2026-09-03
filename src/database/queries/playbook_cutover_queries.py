"""Append-only audit of the Playbook V1 → V2 cutover.

Playbook V2 Package 7 §6
(``docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md``).

Two reads and one write, and deliberately no update or delete: the point of
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
from typing import Any

from sqlalchemy import insert, select

from src.database.tables import CUTOVER_EVENT_KINDS, playbook_cutover_events

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
