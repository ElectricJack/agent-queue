"""Chat analyzer suggestion operations."""

from __future__ import annotations

import time

from sqlalchemy import func, insert, select, update

from src.database.tables import chat_analyzer_suggestions as cas


class ChatQueryMixin:
    """Query mixin for chat analyzer suggestion operations.  Expects ``self._engine``."""

    async def create_chat_analyzer_suggestion(
        self,
        project_id: str,
        channel_id: int,
        suggestion_type: str,
        suggestion_text: str,
        suggestion_hash: str,
        context_snapshot: str | None = None,
    ) -> int:
        """Insert a new chat analyzer suggestion and return its row ID."""
        now = time.time()
        async with self._engine.begin() as conn:
            result = await conn.execute(
                insert(cas).values(
                    project_id=project_id,
                    channel_id=channel_id,
                    suggestion_type=suggestion_type,
                    suggestion_text=suggestion_text,
                    suggestion_hash=suggestion_hash,
                    status="pending",
                    created_at=now,
                    context_snapshot=context_snapshot,
                )
            )
            return result.lastrowid

    async def create_suppressed_chat_analyzer_suggestion(
        self,
        project_id: str,
        channel_id: int,
        suggestion_type: str,
        suggestion_text: str,
        suggestion_hash: str,
        suppressed_by: str,
        context_snapshot: str | None = None,
    ) -> int:
        """Record a suppressed suggestion attempt.

        Phase 8 of the chat-analyzer overhaul gives every suppression
        gate (confidence, dedup, in-flight task, dismiss-cooldown)
        observable footprint in the same table the bot uses for posted
        suggestions.  ``status`` is set to ``"suppressed"`` and
        ``suppressed_by`` records *which* gate fired so the
        ``get_chat_analyzer_metrics`` admin command can break suppression
        counts down by gate without scanning logs.

        ``resolved_at`` is set to the same timestamp as ``created_at``
        because a suppressed suggestion is never seen by a human and
        therefore needs no separate resolution step.
        """
        now = time.time()
        async with self._engine.begin() as conn:
            result = await conn.execute(
                insert(cas).values(
                    project_id=project_id,
                    channel_id=channel_id,
                    suggestion_type=suggestion_type,
                    suggestion_text=suggestion_text,
                    suggestion_hash=suggestion_hash,
                    status="suppressed",
                    suppressed_by=suppressed_by,
                    created_at=now,
                    resolved_at=now,
                    context_snapshot=context_snapshot,
                )
            )
            return result.lastrowid

    async def resolve_chat_analyzer_suggestion(
        self,
        suggestion_id: int,
        status: str,
    ) -> None:
        """Mark a suggestion as accepted or dismissed."""
        now = time.time()
        async with self._engine.begin() as conn:
            await conn.execute(
                update(cas).where(cas.c.id == suggestion_id).values(status=status, resolved_at=now)
            )

    async def get_suggestion_hash_exists(
        self,
        project_id: str,
        suggestion_hash: str,
    ) -> bool:
        """Check if a suggestion with this hash already exists (for dedup)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(cas.c.id)
                .where(
                    (cas.c.project_id == project_id) & (cas.c.suggestion_hash == suggestion_hash)
                )
                .limit(1)
            )
            return result.fetchone() is not None

    async def count_recent_suggestions(
        self,
        project_id: str,
        since: float,
    ) -> int:
        """Count suggestions created since the given timestamp (rate limiting)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(func.count())
                .select_from(cas)
                .where((cas.c.project_id == project_id) & (cas.c.created_at >= since))
            )
            row = result.fetchone()
            return row[0] if row else 0

    async def get_suggestion(self, suggestion_id: int) -> dict | None:
        """Return a single chat analyzer suggestion by ID, or None."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(cas).where(cas.c.id == suggestion_id))
            row = result.mappings().fetchone()
            if not row:
                return None
            return dict(row)

    async def get_recent_suggestions(
        self,
        channel_id: int,
        hours: int = 24,
    ) -> list[dict]:
        """Return suggestions for a channel created within the last N hours."""
        since = time.time() - (hours * 3600)
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(cas)
                .where((cas.c.channel_id == channel_id) & (cas.c.created_at >= since))
                .order_by(cas.c.created_at.desc())
            )
            return [dict(r) for r in result.mappings().fetchall()]

    async def update_suggestion_status(
        self,
        suggestion_id: int,
        status: str,
        resolved_at: float | None = None,
    ) -> None:
        """Update a suggestion's status and optional resolved_at timestamp."""
        if resolved_at is None:
            resolved_at = time.time()
        async with self._engine.begin() as conn:
            await conn.execute(
                update(cas)
                .where(cas.c.id == suggestion_id)
                .values(status=status, resolved_at=resolved_at)
            )

    async def get_last_dismiss_time(
        self,
        project_id: str,
        channel_id: int,
    ) -> float | None:
        """Return the timestamp of the most recent dismissal, or None."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(func.max(cas.c.resolved_at)).where(
                    (cas.c.project_id == project_id)
                    & (cas.c.channel_id == channel_id)
                    & (cas.c.status == "dismissed")
                )
            )
            row = result.fetchone()
            return row[0] if row and row[0] else None

    async def get_analyzer_suggestion_stats(
        self,
        project_id: str | None = None,
        since: float | None = None,
    ) -> dict:
        """Return aggregate stats for chat analyzer suggestions.

        Phase 8 enrichments on top of the original per-status counts:

        * ``suppressed`` is now reported as a first-class status (the
          schema gained a ``"suppressed"`` value alongside the existing
          ``pending``/``accepted``/``dismissed``/``auto_executed``).
        * ``accept_rate`` = ``accepted / (accepted + dismissed)``;
          ``dismiss_rate`` = ``dismissed / (accepted + dismissed)``.
          Both are ``None`` when no suggestion has been resolved yet —
          ``0.0`` would falsely imply a perfect rejection rate.
        * ``suppression_count_by_gate`` is a dict mapping the gate label
          (``"confidence"``, ``"dedup"``, ``"in_flight_active_task"``,
          ``"dismiss_cooldown"``, …) to the number of suppression rows
          attributed to that gate.

        Pass ``since`` (Unix epoch seconds) to restrict every count to
        rows whose ``created_at`` is at or after that timestamp.
        """
        # Per-status counts.
        stmt = select(cas.c.status, func.count().label("cnt"))
        if project_id:
            stmt = stmt.where(cas.c.project_id == project_id)
        if since is not None:
            stmt = stmt.where(cas.c.created_at >= since)
        stmt = stmt.group_by(cas.c.status)

        # Suppression breakdown by gate.
        gate_stmt = select(cas.c.suppressed_by, func.count().label("cnt")).where(
            cas.c.status == "suppressed"
        )
        if project_id:
            gate_stmt = gate_stmt.where(cas.c.project_id == project_id)
        if since is not None:
            gate_stmt = gate_stmt.where(cas.c.created_at >= since)
        gate_stmt = gate_stmt.group_by(cas.c.suppressed_by)

        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            stats: dict = {
                "total": 0,
                "pending": 0,
                "accepted": 0,
                "dismissed": 0,
                "auto_executed": 0,
                "suppressed": 0,
            }
            for row in result.mappings().fetchall():
                status = row["status"]
                count = row["cnt"]
                stats["total"] += count
                if status in stats:
                    stats[status] = count

            gate_result = await conn.execute(gate_stmt)
            suppression_count_by_gate: dict[str, int] = {}
            for row in gate_result.mappings().fetchall():
                gate = row["suppressed_by"]
                # Defensive: skip rows with status=suppressed but no gate.
                # Shouldn't happen via create_suppressed_chat_analyzer_suggestion
                # but a manual SQL backfill could leave NULLs in place.
                if gate is None:
                    continue
                suppression_count_by_gate[gate] = row["cnt"]
            stats["suppression_count_by_gate"] = suppression_count_by_gate

        # Resolution rates.  None when no suggestion has been resolved
        # so callers can distinguish "no data" from "everyone hates them".
        resolved = stats["accepted"] + stats["dismissed"]
        if resolved > 0:
            stats["accept_rate"] = stats["accepted"] / resolved
            stats["dismiss_rate"] = stats["dismissed"] / resolved
        else:
            stats["accept_rate"] = None
            stats["dismiss_rate"] = None

        return stats

    async def get_analyzer_suggestion_history(
        self,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Return recent analyzer suggestions, newest first."""
        stmt = select(
            cas.c.id,
            cas.c.project_id,
            cas.c.channel_id,
            cas.c.suggestion_type,
            cas.c.suggestion_text,
            cas.c.status,
            cas.c.created_at,
            cas.c.resolved_at,
        )
        if project_id:
            stmt = stmt.where(cas.c.project_id == project_id)
        stmt = stmt.order_by(cas.c.created_at.desc()).limit(limit)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [dict(r) for r in result.mappings().fetchall()]
