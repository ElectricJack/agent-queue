"""Durable task/session associations, written inside lifecycle transactions."""

from __future__ import annotations

import time
from uuid import uuid4

from sqlalchemy import func, insert, or_, select, update

from src.database.tables import agents, sessions, task_session_attempts

LIVE_ATTEMPT_STATES = ("starting", "running", "draining")
TERMINAL_SESSION_STATES = ("stopped", "quarantined", "sleeping")


def open_attempts(session_id):
    return (
        task_session_attempts.c.session_id == session_id,
        task_session_attempts.c.ended_at.is_(None),
        task_session_attempts.c.state.in_(LIVE_ATTEMPT_STATES),
    )


class TaskSessionQueryMixin:
    async def _start_task_session_attempt(
        self, conn, session_id, *, started_at=None, work_dir=None
    ):
        row = (
            (await conn.execute(select(sessions).where(sessions.c.id == session_id)))
            .mappings()
            .first()
        )
        if row is None or not row["task_id"]:
            return None
        # Caller holds the session writer/row lock, so a repeated assignment
        # converges without making duplicate associations.
        existing = (
            await conn.execute(
                select(task_session_attempts.c.id).where(
                    *open_attempts(session_id),
                    task_session_attempts.c.task_id == row["task_id"],
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        agent_name = None
        if row["agent_id"]:
            agent_name = (
                await conn.execute(
                    select(agents.c.name).where(
                        agents.c.id == row["agent_id"],
                    )
                )
            ).scalar_one_or_none()
        values = {
            key: row[key]
            for key in (
                "task_id",
                "project_id",
                "agent_id",
                "profile_id",
                "name",
                "lifecycle",
                "model",
                "intelligence_class",
                "llm_provider",
                "harness",
                "provider",
                "state",
                "work_dir",
                "session_key",
                "ended_at",
                "end_reason",
            )
        }
        values.update(
            id=uuid4().hex,
            session_id=session_id,
            agent_name=agent_name,
            started_at=row["started_at"] if started_at is None else started_at,
            session_started_at=row["started_at"],
            outcome=None,
        )
        if work_dir is not None:
            values["work_dir"] = work_dir
        await conn.execute(insert(task_session_attempts).values(**values))
        return values["id"]

    async def get_task_session_attempt(self, attempt_id: str) -> dict | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(task_session_attempts).where(
                            task_session_attempts.c.id == attempt_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            snapshot = dict(row)
            snapshot["transcript_end_at"] = None
            if row["ended_at"] is None and row["state"] in TERMINAL_SESSION_STATES:
                # Legacy exit times are unknown. A later launch in the same
                # conversation or workspace supplies a conservative *reading*
                # boundary, never a fabricated session exit timestamp.
                shared_identity = []
                if row["session_key"]:
                    shared_identity.append(
                        task_session_attempts.c.session_key == row["session_key"]
                    )
                if row["work_dir"]:
                    shared_identity.append(task_session_attempts.c.work_dir == row["work_dir"])
                if shared_identity:
                    snapshot["transcript_end_at"] = (
                        await conn.execute(
                            select(func.min(task_session_attempts.c.session_started_at)).where(
                                task_session_attempts.c.session_started_at
                                > row["session_started_at"],
                                or_(*shared_identity),
                            )
                        )
                    ).scalar_one_or_none()
            return snapshot

    async def list_task_session_attempts(
        self,
        task_id: str,
        *,
        project_id: str | None = None,
        since: float | None = None,
    ) -> list[dict]:
        query = select(task_session_attempts).where(task_session_attempts.c.task_id == task_id)
        if project_id is not None:
            query = query.where(task_session_attempts.c.project_id == project_id)
        if since is not None:
            query = query.where(task_session_attempts.c.started_at >= since)
        async with self._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        query.order_by(
                            task_session_attempts.c.started_at.desc(),
                            task_session_attempts.c.id.desc(),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def finish_task_session_attempt(
        self,
        session_id,
        *,
        task_id=None,
        ended_at=None,
        end_reason=None,
        outcome=None,
        state="stopped",
        conn=None,
    ) -> int:
        async def run(c):
            stmt = update(task_session_attempts).where(*open_attempts(session_id))
            if task_id is not None:
                stmt = stmt.where(task_session_attempts.c.task_id == task_id)
            values = dict(ended_at=time.time() if ended_at is None else ended_at, state=state)
            if end_reason is not None:
                values["end_reason"] = end_reason
            if outcome is not None:
                values["outcome"] = outcome
            return (await c.execute(stmt.values(**values))).rowcount

        if conn is not None:
            return await run(conn)
        async with self.immediate() as c:
            return await run(c)

    async def record_task_session_outcome(self, task_id, outcome, *, session_id=None, conn=None):
        """Attach an accepted close outcome to the latest attempt, even after exit.

        Closing a task does not mean the harness has exited; that timestamp
        comes from release or an observed terminal session transition.
        """

        async def run(c):
            stmt = select(task_session_attempts.c.id).where(
                task_session_attempts.c.task_id == task_id
            )
            if session_id is not None:
                stmt = stmt.where(task_session_attempts.c.session_id == session_id)
            attempt_id = (
                await c.execute(
                    stmt.order_by(
                        task_session_attempts.c.started_at.desc(),
                        task_session_attempts.c.id.desc(),
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if attempt_id is not None:
                await c.execute(
                    update(task_session_attempts)
                    .where(
                        task_session_attempts.c.id == attempt_id,
                    )
                    .values(outcome=outcome)
                )

        if conn is not None:
            return await run(conn)
        async with self.immediate() as c:
            return await run(c)
