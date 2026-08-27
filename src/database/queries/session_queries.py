"""Session row CRUD — the persisted half of the session runtime.

Mirrors :class:`~src.database.queries.workspace_queries.WorkspaceQueryMixin`:
expects ``self._engine``, registered in both adapters' MRO and in the
``DatabaseBackend`` protocol.

Why restart counters live here
------------------------------
``restarts`` and ``quarantined_at`` are persisted on the row rather than
held in memory so the stall/crash ladder survives a daemon restart.  An
in-memory counter would reset the ladder on every reboot, which is exactly
when a crash-looping session is most likely to be running.

On ``sessions.name`` uniqueness
-------------------------------
The index on ``name`` is deliberately **non-unique**, and
:meth:`SessionQueryMixin.get_session_by_name` resolves the ambiguity rather
than the schema doing it.  Session names are derived (``s-<task_id>``,
``n-<profile>[--<project>]``), so the naming convention *does* imply one
live session per name — but not one row per name ever:

* the stall ladder's rung 3 kills and relaunches with ``--resume``, and the
  restarted session is a new row with the same name;
* a named session that sleeps and wakes does the same;
* stopped rows are kept as history (restart counts, exit forensics,
  ``quarantined_at``) and are not deleted on stop.

A UNIQUE constraint would force either a destructive delete-on-restart or a
disambiguating suffix in the name — and the name is what a human types into
``tmux attach``, so a suffix is a real cost.  The invariant we actually need
is "at most one *live* row per name", which the reconciler enforces and
:meth:`get_session_by_name` reflects by preferring live rows and, among
equals, the most recently started.  Callers wanting the strict set use
``list_sessions(name=...)``.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, insert, select, update

from src.database.tables import sessions
from src.models import SessionRecord

logger = logging.getLogger(__name__)

#: States a session can be in and still be a candidate for "the current one
#: under this name".  ``stopped`` rows are history; ``quarantined`` rows are
#: terminal-by-default but still the newest thing under that name, so they
#: rank above ``stopped`` and below anything live.
_LIVE_STATES: tuple[str, ...] = ("starting", "running", "draining")

#: The session state machine, enforced by :meth:`SessionQueryMixin.update_session`.
#:
#: The design spec has always described these transitions; nothing checked
#: them, which is the Gas City post-mortem's finding we borrowed the
#: diagnosis of and not the cure.  Two edges are load-bearing:
#:
#: * **nothing revives a ``stopped`` row.**  A restart deliberately produces
#:   a *new* row (see the module docstring on name uniqueness), so a revived
#:   one would put two live rows under a single name and silently break the
#:   invariant ``create_session`` enforces.
#: * **``quarantined`` is terminal.**  "Nothing auto-releases a quarantine"
#:   was a docstring promise; here it is a constraint.
#:
#: Re-writing the state a row already has is a no-op, not a violation:
#: callers converge, and converging twice is normal.
_SESSION_TRANSITIONS: dict[str, frozenset[str]] = {
    "starting": frozenset({"running", "draining", "sleeping", "stopped", "quarantined"}),
    "running": frozenset({"draining", "sleeping", "stopped", "quarantined"}),
    "draining": frozenset({"stopped", "quarantined"}),
    "sleeping": frozenset({"stopped", "quarantined"}),
    "stopped": frozenset(),
    "quarantined": frozenset(),
}

#: Intent (``desired_state``) is deliberately **not** guarded.  Every intent
#: is expressible from every state -- wanting a stopped session running
#: again is the whole point of the column.
_DESIRED_STATES: frozenset[str] = frozenset({"running", "sleeping", "stopped"})


class InvalidSessionTransition(ValueError):
    """An update tried to walk an edge the state machine does not have."""

    def __init__(self, session_id: str, current: str, requested: str):
        self.session_id = session_id
        self.current = current
        self.requested = requested
        allowed = sorted(_SESSION_TRANSITIONS.get(current, frozenset()))
        super().__init__(
            f"session {session_id}: {current!r} -> {requested!r} is not a legal "
            f"transition (allowed from {current!r}: {allowed or 'none — terminal'})"
        )
_STATE_RANK: dict[str, int] = {
    "running": 0,
    "starting": 1,
    "draining": 2,
    "sleeping": 3,
    "quarantined": 4,
    "stopped": 5,
}


def _row_to_session(row) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        task_id=row["task_id"],
        project_id=row["project_id"],
        profile_id=row["profile_id"],
        harness=row["harness"],
        provider=row["provider"],
        name=row["name"],
        lifecycle=row["lifecycle"],
        state=row["state"] or "starting",
        desired_state=row["desired_state"] or "running",
        session_key=row["session_key"],
        work_dir=row["work_dir"],
        epoch=row["epoch"],
        instance_token=row["instance_token"],
        started_at=row["started_at"],
        last_activity=row["last_activity"],
        restarts=row["restarts"] or 0,
        quarantined_at=row["quarantined_at"],
        sleep_reason=row["sleep_reason"],
    )


class SessionQueryMixin:
    """Query mixin for the ``sessions`` table.  Expects ``self._engine``."""

    async def create_session(self, session: SessionRecord) -> None:
        """Insert a session row, enforcing one *live* row per name.

        The module docstring argues (correctly) that ``sessions.name`` must
        not be UNIQUE, and that the invariant we actually need is "at most
        one live row per name".  That invariant was documented and *latent*
        — nothing checked it — which is how a failed adoption pass could
        leave a live row for a name and then have the scheduler insert a
        second one beside it.  A cheap read before the insert makes it
        enforced.
        """
        if session.state in _LIVE_STATES:
            existing = await self.list_sessions(name=session.name, live_only=True)
            clash = [r for r in existing if r.id != session.id]
            if clash:
                raise ValueError(
                    f"session name {session.name!r} already has a live row "
                    f"({', '.join(r.id for r in clash)}) — stop it before "
                    "starting another"
                )
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(sessions).values(
                    id=session.id,
                    task_id=session.task_id,
                    project_id=session.project_id,
                    profile_id=session.profile_id,
                    harness=session.harness,
                    provider=session.provider,
                    name=session.name,
                    lifecycle=session.lifecycle,
                    state=session.state,
                    desired_state=session.desired_state,
                    session_key=session.session_key,
                    work_dir=session.work_dir,
                    epoch=session.epoch,
                    instance_token=session.instance_token,
                    started_at=session.started_at,
                    last_activity=session.last_activity,
                    restarts=session.restarts,
                    quarantined_at=session.quarantined_at,
                    sleep_reason=session.sleep_reason,
                )
            )

    async def get_session(self, session_id: str) -> SessionRecord | None:
        async with self._engine.begin() as conn:
            result = await conn.execute(select(sessions).where(sessions.c.id == session_id))
            row = result.mappings().fetchone()
            return _row_to_session(row) if row else None

    async def get_session_by_name(self, name: str) -> SessionRecord | None:
        """Resolve a provider session name to its current row.

        Prefers live rows, then sleeping/quarantined, then stopped history;
        ties break on the most recent ``started_at``.  See the module
        docstring for why the index is not unique.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(select(sessions).where(sessions.c.name == name))
            rows = [_row_to_session(r) for r in result.mappings().fetchall()]
        if not rows:
            return None
        rows.sort(key=lambda s: (_STATE_RANK.get(s.state, 9), -(s.started_at or 0.0)))
        return rows[0]

    async def get_session_for_task(self, task_id: str) -> SessionRecord | None:
        """The current session for *task_id* (same ranking as by-name)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(sessions).where(sessions.c.task_id == task_id))
            rows = [_row_to_session(r) for r in result.mappings().fetchall()]
        if not rows:
            return None
        rows.sort(key=lambda s: (_STATE_RANK.get(s.state, 9), -(s.started_at or 0.0)))
        return rows[0]

    async def list_sessions(
        self,
        *,
        state: str | None = None,
        states: list[str] | tuple[str, ...] | None = None,
        desired_state: str | None = None,
        lifecycle: str | None = None,
        project_id: str | None = None,
        name: str | None = None,
        live_only: bool = False,
    ) -> list[SessionRecord]:
        """List sessions, newest first.

        ``live_only=True`` is shorthand for ``states=("starting", "running",
        "draining")`` — the set the reconciler observes each tick.
        """
        query = select(sessions)
        if state is not None:
            query = query.where(sessions.c.state == state)
        if live_only:
            query = query.where(sessions.c.state.in_(_LIVE_STATES))
        elif states:
            query = query.where(sessions.c.state.in_(list(states)))
        if desired_state is not None:
            query = query.where(sessions.c.desired_state == desired_state)
        if lifecycle is not None:
            query = query.where(sessions.c.lifecycle == lifecycle)
        if project_id is not None:
            query = query.where(sessions.c.project_id == project_id)
        if name is not None:
            query = query.where(sessions.c.name == name)
        query = query.order_by(sessions.c.started_at.desc())
        async with self._engine.begin() as conn:
            result = await conn.execute(query)
            return [_row_to_session(r) for r in result.mappings().fetchall()]

    async def update_session(self, session_id: str, **fields) -> int:
        """Update arbitrary columns.  Returns rows affected.

        Writes to ``state`` are checked against ``_SESSION_TRANSITIONS`` and
        raise :class:`InvalidSessionTransition` on an illegal edge.  Raising
        is safe for the reconciler, whose steps are individually guarded: a
        bad edge fails one step loudly instead of corrupting the row.
        """
        if not fields:
            return 0
        requested = fields.get("state")
        if requested is not None:
            current = await self._current_state(session_id)
            # A missing row is the UPDATE's problem (it affects 0 rows), not
            # the state machine's -- do not turn it into a transition error.
            if current is not None and requested != current:
                if requested not in _SESSION_TRANSITIONS.get(current, frozenset()):
                    raise InvalidSessionTransition(session_id, current, requested)
        desired = fields.get("desired_state")
        if desired is not None and desired not in _DESIRED_STATES:
            raise ValueError(
                f"session {session_id}: {desired!r} is not a desired state "
                f"({sorted(_DESIRED_STATES)})"
            )
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(sessions).where(sessions.c.id == session_id).values(**fields)
            )
            return result.rowcount

    async def _current_state(self, session_id: str) -> str | None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(sessions.c.state).where(sessions.c.id == session_id)
            )
            row = result.fetchone()
        return row[0] if row else None

    async def bump_session_restarts(self, session_id: str) -> int:
        """Atomically increment ``restarts`` and return the new value.

        Done as a single ``SET restarts = restarts + 1`` statement rather
        than read-modify-write: two reconciler ticks racing on a
        crash-looping session must not both read 2 and both write 3, which
        is how a quarantine threshold silently never fires.
        """
        async with self._engine.begin() as conn:
            await conn.execute(
                update(sessions)
                .where(sessions.c.id == session_id)
                .values(restarts=sessions.c.restarts + 1)
            )
            result = await conn.execute(
                select(sessions.c.restarts).where(sessions.c.id == session_id)
            )
            row = result.fetchone()
            return int(row[0]) if row else 0

    async def touch_session_activity(self, session_id: str, ts: float) -> None:
        """Record observed agent activity (monotonic in practice, not enforced)."""
        async with self._engine.begin() as conn:
            await conn.execute(
                update(sessions)
                .where(sessions.c.id == session_id)
                .values(last_activity=ts)
            )

    async def delete_session(self, session_id: str) -> None:
        """Hard-delete a row.  Admin and tests only — stop() keeps history."""
        async with self._engine.begin() as conn:
            await conn.execute(delete(sessions).where(sessions.c.id == session_id))
