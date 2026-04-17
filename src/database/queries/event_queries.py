"""Event (audit log) operations."""

from __future__ import annotations

import time

from sqlalchemy import and_, insert, select

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

    async def get_recent_events(
        self,
        limit: int = 50,
        *,
        event_type: str | None = None,
        since: float | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
    ) -> list[dict]:
        """Return recent events, newest first, with optional filters.

        Args:
            limit: Maximum number of events to return.
            event_type: Filter by event type. Supports prefix matching with
                trailing ``*`` (e.g. ``"task.*"`` matches ``task.started``,
                ``task.completed``, etc.). Exact match otherwise.
            since: Only return events with timestamp >= this Unix epoch value.
            project_id: Filter by project ID (exact match).
            agent_id: Filter by agent ID (exact match).
            task_id: Filter by task ID (exact match).
        """
        conditions = []
        if event_type:
            if event_type.endswith("*"):
                prefix = event_type[:-1]
                conditions.append(events.c.event_type.startswith(prefix))
            else:
                conditions.append(events.c.event_type == event_type)
        if since is not None:
            conditions.append(events.c.timestamp >= since)
        if project_id:
            conditions.append(events.c.project_id == project_id)
        if agent_id:
            conditions.append(events.c.agent_id == agent_id)
        if task_id:
            conditions.append(events.c.task_id == task_id)

        stmt = select(events)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(events.c.id.desc()).limit(limit)

        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [dict(r) for r in result.mappings().fetchall()]
