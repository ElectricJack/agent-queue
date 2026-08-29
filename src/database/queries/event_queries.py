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
        *,
        conn=None,
    ) -> int:
        """Record a lifecycle event and return the inserted row id.

        The row id doubles as the WebSocket replay/live-handoff ``seq``:
        callers that persist-then-emit thread this id into the bus
        payload so a client reconnecting with ``after_seq=N`` can dedup
        live frames overlapping the replay window (WG-4 spec §8).

        When *conn* is given, the insert runs on the caller's connection
        instead of opening a new transaction — for callers that must write
        the audit row in the same transaction as the state change it
        records (e.g. ``task.ready`` frontier entries, spec §9).
        """
        if conn is not None:
            return await self._insert_event_row(conn, event_type, project_id, task_id, agent_id, payload)
        async with self._engine.begin() as conn:
            return await self._insert_event_row(conn, event_type, project_id, task_id, agent_id, payload)

    async def _insert_event_row(
        self, conn, event_type, project_id, task_id, agent_id, payload
    ) -> int:
        result = await conn.execute(
            insert(events).values(
                event_type=event_type,
                project_id=project_id,
                task_id=task_id,
                agent_id=agent_id,
                payload=payload,
                timestamp=time.time(),
            )
        )
        # ``inserted_primary_key`` works for both SQLite + Postgres
        # under SQLAlchemy Core when the PK is auto-increment.
        try:
            return int(result.inserted_primary_key[0])
        except Exception:
            return 0

    async def get_recent_events(
        self,
        limit: int = 50,
        *,
        event_type: str | None = None,
        since: float | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        after_id: int | None = None,
    ) -> list[dict]:
        """Return events with optional filters.

        Args:
            limit: Maximum number of events to return.
            event_type: Filter by event type. Supports prefix matching with
                trailing ``*`` (e.g. ``"task.*"`` matches ``task.started``,
                ``task.completed``, etc.). Exact match otherwise.
            since: Only return events with timestamp >= this Unix epoch value.
            project_id: Filter by project ID (exact match).
            agent_id: Filter by agent ID (exact match).
            task_id: Filter by task ID (exact match).
            after_id: Replay mode — when set, returns events with
                ``id > after_id`` ordered by id ASC (gapless pagination for
                WebSocket ``after_seq`` reconnect).  When ``None`` (default),
                behaviour is unchanged: newest-first by id DESC.
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
        if after_id is not None:
            conditions.append(events.c.id > after_id)

        stmt = select(events)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        if after_id is not None:
            # Replay: ascending, gapless.
            stmt = stmt.order_by(events.c.id.asc()).limit(limit)
        else:
            stmt = stmt.order_by(events.c.id.desc()).limit(limit)

        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [dict(r) for r in result.mappings().fetchall()]
