"""Event (audit log) operations."""

from __future__ import annotations

import time


class EventQueryMixin:
    """Query mixin for event/audit log operations.  Expects ``self._db``."""

    async def log_event(
        self,
        event_type: str,
        project_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        payload: str | None = None,
    ) -> None:
        """Record a lifecycle event."""
        await self._db.execute(
            "INSERT INTO events (event_type, project_id, task_id, agent_id, "
            "payload, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (event_type, project_id, task_id, agent_id, payload, time.time()),
        )
        await self._db.commit()

    async def get_recent_events(self, limit: int = 50) -> list[dict]:
        """Return the most recent events, newest first."""
        cursor = await self._db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
