"""Chat analyzer suggestion operations."""

from __future__ import annotations

import time


class ChatQueryMixin:
    """Query mixin for chat analyzer suggestion operations.  Expects ``self._db``."""

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
        cursor = await self._db.execute(
            "INSERT INTO chat_analyzer_suggestions "
            "(project_id, channel_id, suggestion_type, suggestion_text, "
            "suggestion_hash, status, created_at, context_snapshot) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                project_id,
                channel_id,
                suggestion_type,
                suggestion_text,
                suggestion_hash,
                now,
                context_snapshot,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def resolve_chat_analyzer_suggestion(
        self,
        suggestion_id: int,
        status: str,
    ) -> None:
        """Mark a suggestion as accepted or dismissed."""
        now = time.time()
        await self._db.execute(
            "UPDATE chat_analyzer_suggestions SET status = ?, resolved_at = ? WHERE id = ?",
            (status, now, suggestion_id),
        )
        await self._db.commit()

    async def get_suggestion_hash_exists(
        self,
        project_id: str,
        suggestion_hash: str,
    ) -> bool:
        """Check if a suggestion with this hash already exists (for dedup)."""
        cursor = await self._db.execute(
            "SELECT 1 FROM chat_analyzer_suggestions "
            "WHERE project_id = ? AND suggestion_hash = ? LIMIT 1",
            (project_id, suggestion_hash),
        )
        return (await cursor.fetchone()) is not None

    async def count_recent_suggestions(
        self,
        project_id: str,
        since: float,
    ) -> int:
        """Count suggestions created since the given timestamp (rate limiting)."""
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM chat_analyzer_suggestions "
            "WHERE project_id = ? AND created_at >= ?",
            (project_id, since),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_suggestion(self, suggestion_id: int) -> dict | None:
        """Return a single chat analyzer suggestion by ID, or None."""
        cursor = await self._db.execute(
            "SELECT * FROM chat_analyzer_suggestions WHERE id = ?",
            (suggestion_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "channel_id": row["channel_id"],
            "suggestion_type": row["suggestion_type"],
            "suggestion_text": row["suggestion_text"],
            "suggestion_hash": row["suggestion_hash"],
            "status": row["status"],
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "context_snapshot": row["context_snapshot"],
        }

    async def get_recent_suggestions(
        self,
        channel_id: int,
        hours: int = 24,
    ) -> list[dict]:
        """Return suggestions for a channel created within the last N hours."""
        since = time.time() - (hours * 3600)
        cursor = await self._db.execute(
            "SELECT * FROM chat_analyzer_suggestions "
            "WHERE channel_id = ? AND created_at >= ? "
            "ORDER BY created_at DESC",
            (channel_id, since),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "channel_id": row["channel_id"],
                "suggestion_type": row["suggestion_type"],
                "suggestion_text": row["suggestion_text"],
                "suggestion_hash": row["suggestion_hash"],
                "status": row["status"],
                "created_at": row["created_at"],
                "resolved_at": row["resolved_at"],
                "context_snapshot": row["context_snapshot"],
            }
            for row in rows
        ]

    async def update_suggestion_status(
        self,
        suggestion_id: int,
        status: str,
        resolved_at: float | None = None,
    ) -> None:
        """Update a suggestion's status and optional resolved_at timestamp."""
        if resolved_at is None:
            resolved_at = time.time()
        await self._db.execute(
            "UPDATE chat_analyzer_suggestions SET status = ?, resolved_at = ? WHERE id = ?",
            (status, resolved_at, suggestion_id),
        )
        await self._db.commit()

    async def get_last_dismiss_time(
        self,
        project_id: str,
        channel_id: int,
    ) -> float | None:
        """Return the timestamp of the most recent dismissal, or None."""
        cursor = await self._db.execute(
            "SELECT MAX(resolved_at) FROM chat_analyzer_suggestions "
            "WHERE project_id = ? AND channel_id = ? AND status = 'dismissed'",
            (project_id, channel_id),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def get_analyzer_suggestion_stats(
        self,
        project_id: str | None = None,
    ) -> dict:
        """Return aggregate stats for chat analyzer suggestions."""
        where = ""
        params: tuple = ()
        if project_id:
            where = " WHERE project_id = ?"
            params = (project_id,)

        cursor = await self._db.execute(
            f"SELECT status, COUNT(*) as cnt FROM chat_analyzer_suggestions{where} GROUP BY status",
            params,
        )
        rows = await cursor.fetchall()
        stats = {"total": 0, "pending": 0, "accepted": 0, "dismissed": 0, "auto_executed": 0}
        for row in rows:
            status = row["status"]
            count = row["cnt"]
            stats["total"] += count
            if status in stats:
                stats[status] = count
        return stats

    async def get_analyzer_suggestion_history(
        self,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Return recent analyzer suggestions, newest first."""
        where = ""
        params: list = []
        if project_id:
            where = " WHERE project_id = ?"
            params.append(project_id)

        params.append(limit)
        cursor = await self._db.execute(
            f"SELECT * FROM chat_analyzer_suggestions{where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "project_id": row["project_id"],
                "channel_id": row["channel_id"],
                "suggestion_type": row["suggestion_type"],
                "suggestion_text": row["suggestion_text"],
                "status": row["status"],
                "created_at": row["created_at"],
                "resolved_at": row["resolved_at"],
            }
            for row in rows
        ]
