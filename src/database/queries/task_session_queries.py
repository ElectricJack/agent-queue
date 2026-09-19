"""Durable task/session associations, written inside lifecycle transactions."""

from __future__ import annotations

import time
from uuid import uuid4

from sqlalchemy import and_, exists, func, insert, literal, or_, select, update

from src.database.tables import agents, sessions, task_session_attempts, tasks

LIVE_ATTEMPT_STATES = ("starting", "running", "draining")
TERMINAL_SESSION_STATES = ("stopped", "quarantined", "sleeping")

#: How long a running session may be silent before its attempt stops counting
#: as live.  Mirrors ``src.database.queries.digest_queries.DEFAULT_STALE_AFTER``
#: (the digest's default lease TTL); kept as its own constant here so this
#: module never has to import back from the digest layer.
DEFAULT_STALE_AFTER = 480.0

#: Task statuses a live attempt's task must be in for its worker to dock on
#: the task graph.  A finished or not-yet-started task never carries a
#: marker even if a stray session row still points at it.
LIVE_WORKER_TASK_STATUSES = ("ASSIGNED", "IN_PROGRESS")


def open_attempts(session_id):
    return (
        task_session_attempts.c.session_id == session_id,
        task_session_attempts.c.ended_at.is_(None),
        task_session_attempts.c.state.in_(LIVE_ATTEMPT_STATES),
    )


def live_attempt_predicate(now: float, stale_after: float):
    """The shared "this attempt is executing right now" clause.

    Extracted from ``collect_digest_activity`` (implementation spec §8):
    an open attempt in a live state, on a session that is actually
    ``running`` and not yet ``ended_at``, whose session has spoken (or
    started) within ``stale_after``.  Callers join ``task_session_attempts``
    to ``sessions`` themselves and add any query-specific bounds (e.g. the
    digest's ``started_at <= until``) at the call site.
    """
    return and_(
        task_session_attempts.c.ended_at.is_(None),
        task_session_attempts.c.state.in_(LIVE_ATTEMPT_STATES),
        sessions.c.state == "running",
        sessions.c.ended_at.is_(None),
        or_(
            sessions.c.last_activity.is_not(None)
            & (sessions.c.last_activity >= now - stale_after),
            sessions.c.started_at >= now - stale_after,
        ),
    )


class TaskSessionQueryMixin:
    async def _start_task_session_attempt(
        self, conn, session_id, *, started_at=None, work_dir=None
    ):
        """Snapshot a newly-held session in one ``INSERT ... SELECT``.

        The caller already owns the session writer/row lock.  Selecting the
        session, checking for an open attempt, looking up the agent name, and
        inserting the snapshot separately therefore bought no additional
        safety but added four round trips to every pool claim.  Keep the
        idempotence predicate and launch snapshot in one portable statement.
        """
        attempt_id = uuid4().hex
        has_open_attempt = exists(
            select(literal(1)).where(
                task_session_attempts.c.session_id == sessions.c.id,
                task_session_attempts.c.task_id == sessions.c.task_id,
                task_session_attempts.c.ended_at.is_(None),
                task_session_attempts.c.state.in_(LIVE_ATTEMPT_STATES),
            )
        )
        columns = [
            task_session_attempts.c.id,
            task_session_attempts.c.session_id,
            task_session_attempts.c.task_id,
            task_session_attempts.c.project_id,
            task_session_attempts.c.agent_id,
            task_session_attempts.c.agent_name,
            task_session_attempts.c.profile_id,
            task_session_attempts.c.name,
            task_session_attempts.c.lifecycle,
            task_session_attempts.c.model,
            task_session_attempts.c.intelligence_class,
            task_session_attempts.c.llm_provider,
            task_session_attempts.c.harness,
            task_session_attempts.c.provider,
            task_session_attempts.c.state,
            task_session_attempts.c.work_dir,
            task_session_attempts.c.started_at,
            task_session_attempts.c.session_started_at,
            task_session_attempts.c.ended_at,
            task_session_attempts.c.end_reason,
            task_session_attempts.c.outcome,
            task_session_attempts.c.session_key,
        ]
        snapshot = (
            select(
                literal(attempt_id),
                sessions.c.id,
                sessions.c.task_id,
                sessions.c.project_id,
                sessions.c.agent_id,
                agents.c.name,
                sessions.c.profile_id,
                sessions.c.name,
                sessions.c.lifecycle,
                sessions.c.model,
                sessions.c.intelligence_class,
                sessions.c.llm_provider,
                sessions.c.harness,
                sessions.c.provider,
                sessions.c.state,
                literal(work_dir) if work_dir is not None else sessions.c.work_dir,
                literal(started_at) if started_at is not None else sessions.c.started_at,
                sessions.c.started_at,
                sessions.c.ended_at,
                sessions.c.end_reason,
                literal(None),
                sessions.c.session_key,
            )
            .select_from(sessions.outerjoin(agents, agents.c.id == sessions.c.agent_id))
            .where(sessions.c.id == session_id, sessions.c.task_id.is_not(None), ~has_open_attempt)
        )
        result = await conn.execute(insert(task_session_attempts).from_select(columns, snapshot))
        return attempt_id if result.rowcount else None

    async def list_live_task_workers(
        self,
        project_id: str,
        *,
        now: float | None = None,
        stale_after: float = DEFAULT_STALE_AFTER,
    ) -> list[dict]:
        """Dock markers: one row per agent with a live attempt in this project.

        Source of truth for the task graph's worker markers (docked at
        ``current_task_id``) -- deliberately *not* ``agents.current_task_id``,
        which is only cleared on the next claim/release and dangles on a
        finished task in between.  Restricted to a task still ``ASSIGNED`` or
        ``IN_PROGRESS`` so a live attempt on an already-completed task (a race
        between the attempt closing and the task transitioning) never docks.
        """
        now = time.time() if now is None else now
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        task_session_attempts.c.agent_id,
                        task_session_attempts.c.agent_name,
                        task_session_attempts.c.task_id,
                        task_session_attempts.c.started_at,
                    )
                    .select_from(
                        task_session_attempts.join(
                            sessions, sessions.c.id == task_session_attempts.c.session_id
                        ).join(tasks, tasks.c.id == task_session_attempts.c.task_id)
                    )
                    .where(
                        live_attempt_predicate(now, stale_after),
                        tasks.c.project_id == project_id,
                        tasks.c.status.in_(LIVE_WORKER_TASK_STATUSES),
                        task_session_attempts.c.agent_id.is_not(None),
                    )
                    .order_by(task_session_attempts.c.started_at.desc())
                )
            ).all()
        seen: set[tuple[str, str]] = set()
        out: list[dict] = []
        for row in rows:
            key = (row.agent_id, row.task_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {"id": row.agent_id, "name": row.agent_name, "current_task_id": row.task_id}
            )
        return out

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
