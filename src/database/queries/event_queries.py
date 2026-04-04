"""Event (audit log) operations."""

from __future__ import annotations

import time

from sqlalchemy import insert, select

from src.database.tables import events


class EventQueryMixin:
    """Query mixin for event/audit log operations.  Expects ``self._engine``."""

    async def log_event(
        self,
        event_type: str,
        project_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        payload: str | None = None,
    ) -> None:
        """Record a lifecycle event."""
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(events).values(
                    event_type=event_type,
                    project_id=project_id,
                    task_id=task_id,
                    agent_id=agent_id,
                    payload=payload,
                    timestamp=time.time(),
                )
            )

    async def get_recent_events(self, limit: int = 50) -> list[dict]:
        """Return the most recent events, newest first."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(events).order_by(events.c.id.desc()).limit(limit))
            return [dict(r) for r in result.mappings().fetchall()]
