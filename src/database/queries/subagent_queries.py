"""Native subagent lifecycle events, and the folds the flock reads.

The table is append-only (``src/database/tables.py::subagent_events``): a
``SubagentStart`` / ``SubagentStop`` hook reports a fact about a moment,
and "how many children are running" is a fold over those facts.  Keeping a
mutable counter instead would make a re-delivered hook or a lost Stop
permanently wrong, with nothing left to audit.
"""

from __future__ import annotations

import hashlib
import time

from sqlalchemy import case, func, insert, select
from sqlalchemy.exc import IntegrityError

from src.database.tables import subagent_events


def subagent_event_id(session_id: str, event: str, subagent_id: str) -> str:
    """Deterministic primary key, so a duplicate delivery is a no-op.

    Both harnesses identify a child by ``agent_id`` on *both* halves of its
    life, so (session, event, child) names exactly one fact.  A digest
    rather than a composite key keeps the row addressable by a single
    ``id`` like every other table here.
    """
    digest = hashlib.sha256(
        "\x1f".join((session_id, event, subagent_id)).encode("utf-8")
    )
    return digest.hexdigest()


class SubagentQueriesMixin:
    """Query mixin for ``subagent_events``.  Expects ``self._engine``."""

    async def record_subagent_event(
        self,
        *,
        session_id: str,
        harness: str,
        event: str,
        subagent_id: str,
        project_id: str | None = None,
        task_id: str | None = None,
        agent_type: str | None = None,
        turn_id: str | None = None,
        occurred_at: float | None = None,
    ) -> bool:
        """Insert one lifecycle event.  Returns True when it was new.

        Idempotent by construction: a hook the harness delivers twice hashes
        to the row it already wrote and is dropped.  A ``stop`` whose
        ``start`` never arrived is stored anyway — the fold clamps at zero,
        because losing a Start must not make a session look like it is
        running a child forever.
        """
        row_id = subagent_event_id(session_id, event, subagent_id)
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(subagent_events).values(
                        id=row_id,
                        session_id=session_id,
                        harness=harness,
                        project_id=project_id,
                        task_id=task_id,
                        subagent_id=subagent_id,
                        agent_type=agent_type,
                        turn_id=turn_id,
                        event=event,
                        occurred_at=occurred_at if occurred_at is not None else time.time(),
                    )
                )
            return True
        except IntegrityError:
            return False

    async def subagent_counts_by_session(
        self, session_ids: list[str] | None = None
    ) -> dict[str, dict[str, int]]:
        """``{session_id: {"starts": n, "stops": n}}`` for the whole table.

        One grouped statement rather than a query per agent: the flock view
        folds every agent in a single pass and would otherwise issue one
        round trip per row.  Sessions with no events are simply absent —
        callers read them as zero.
        """
        starts = func.sum(case((subagent_events.c.event == "start", 1), else_=0))
        stops = func.sum(case((subagent_events.c.event == "stop", 1), else_=0))
        stmt = select(
            subagent_events.c.session_id,
            starts.label("starts"),
            stops.label("stops"),
        ).group_by(subagent_events.c.session_id)
        if session_ids is not None:
            if not session_ids:
                return {}
            stmt = stmt.where(subagent_events.c.session_id.in_(list(session_ids)))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return {
            row["session_id"]: {
                "starts": int(row["starts"] or 0),
                "stops": int(row["stops"] or 0),
            }
            for row in rows
        }

    async def list_subagent_events(
        self, session_id: str, *, limit: int = 200
    ) -> list[dict]:
        """Newest-first detail for one session — the per-session drill-down."""
        stmt = (
            select(subagent_events)
            .where(subagent_events.c.session_id == session_id)
            .order_by(subagent_events.c.occurred_at.desc())
            .limit(max(1, int(limit)))
        )
        async with self._engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(stmt)).mappings().all()]
