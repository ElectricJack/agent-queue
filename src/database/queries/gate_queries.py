"""Gate queries — the ``gates`` and ``task_gates`` read/write surface.

Implements docs/specs/implementation/work-graph.md §4.3.  Gates block their
attached tasks until *resolved*; every mutation here recomputes the
``tasks.is_blocked`` projection for the waiters in the same transaction so
readers never observe a gate change without its projection.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Iterable

from sqlalchemy import and_, insert, select, update
from sqlalchemy.exc import IntegrityError

from src.database.tables import gates, task_gates, tasks

logger = logging.getLogger(__name__)

__all__ = ["GateQueriesMixin"]


_VALID_STATUSES = ("open", "resolved", "expired")


def _row_to_gate(row) -> dict:
    """Convert a ``gates`` row mapping to a plain dict."""
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "gate_type": row["gate_type"],
        "title": row["title"],
        "question": row["question"],
        "await_id": row["await_id"],
        "timeout_at": row["timeout_at"],
        "status": row["status"],
        "resolved_by": row["resolved_by"],
        "resolution": row["resolution"],
        "created_at": row["created_at"],
    }


class GateQueriesMixin:
    """Gate CRUD + resolution + expiry (work-graph §4.3).

    Mixed into the database adapters alongside the other query mixins.
    Expects ``self._engine`` and the ``BlockedStateMixin``'s
    ``recompute_blocked`` / ``log_blocked_flips`` helpers.
    """

    async def create_gate(
        self,
        project_id: str,
        gate_type: str,
        title: str,
        *,
        question: str = "",
        await_id: str | None = None,
        timeout_at: float | None = None,
        waiter_task_ids: Iterable[str] = (),
        conn=None,
        unrouted_only: bool = False,
    ) -> tuple[str | None, bool]:
        """Insert a gate + its ``task_gates`` rows and recompute waiters.

        Returns ``(gate_id, was_created)``. If an ``open`` gate already
        exists with the same ``(project_id, gate_type, await_id)`` *and*
        an identical waiter set, its id is returned with
        ``was_created=False`` — pipeline reruns can no longer stack
        duplicate gates. Otherwise a fresh gate is inserted and
        ``was_created=True``.  All writes happen in one transaction so a
        reader can never see the gate rows without the waiters' refreshed
        projection.

        Pass ``conn`` to run inside a caller-owned transaction (e.g. worker
        filing's ``immediate()`` block) — the caller is then responsible for
        the surrounding commit/rollback and for calling
        :meth:`log_blocked_flips` afterwards. Without ``conn`` this opens
        (and commits) its own transaction as before.
        """
        if unrouted_only and gate_type == "routing":
            # Serialize with task_route's guarded UPDATE and with competing
            # gate creation. Rechecking the profile under this lock prevents
            # a late pipeline callback from gating an already routed task.
            async def attach(connection):
                requested = sorted(set(waiter_task_ids))
                rows = (await connection.execute(
                    select(tasks.c.id, tasks.c.profile_id)
                    .where(tasks.c.id.in_(requested))
                    .order_by(tasks.c.id).with_for_update()
                )).fetchall()
                routed = {row.id for row in rows if (row.profile_id or "").strip()}
                waiters = [tid for tid in requested if tid not in routed]
                if requested and not waiters:
                    return None, False, set()
                return await self._create_gate_on(
                    connection, project_id, gate_type, title,
                    question=question, await_id=await_id, timeout_at=timeout_at,
                    waiter_task_ids=waiters, caller_owns_conn=True,
                )
            if conn is not None:
                gate_id, was_created, _flipped = await attach(conn)
            else:
                async with self.immediate() as owned_conn:
                    gate_id, was_created, flipped = await attach(owned_conn)
                await self.log_blocked_flips(flipped)
            return gate_id, was_created

        if conn is not None:
            gate_id, was_created, _flipped = await self._create_gate_on(
                conn,
                project_id,
                gate_type,
                title,
                question=question,
                await_id=await_id,
                timeout_at=timeout_at,
                waiter_task_ids=waiter_task_ids,
                caller_owns_conn=True,
            )
            return gate_id, was_created

        async with self._engine.begin() as owned_conn:
            gate_id, was_created, flipped = await self._create_gate_on(
                owned_conn,
                project_id,
                gate_type,
                title,
                question=question,
                await_id=await_id,
                timeout_at=timeout_at,
                waiter_task_ids=waiter_task_ids,
                caller_owns_conn=False,
            )
        await self.log_blocked_flips(flipped)
        return gate_id, was_created

    async def _create_gate_on(
        self,
        conn,
        project_id: str,
        gate_type: str,
        title: str,
        *,
        question: str,
        await_id: str | None,
        timeout_at: float | None,
        waiter_task_ids: Iterable[str],
        caller_owns_conn: bool,
    ) -> tuple[str, bool, set[str]]:
        """Do the actual insert + dedup + recompute on *conn*.

        Caller owns the transaction. Returns ``(gate_id, was_created,
        flipped)`` — ``flipped`` is the ``is_blocked`` flip set from
        :meth:`recompute_blocked`, left for the caller to log post-commit.
        """
        waiters = sorted(set(waiter_task_ids))
        waiter_set = set(waiters)
        now = time.time()
        # Dedup: match on (project_id, gate_type, await_id) among
        # open gates, then compare waiter sets. NULL await_id
        # requires an explicit IS NULL predicate (SQL NULL != NULL).
        match_conds = [
            gates.c.project_id == project_id,
            gates.c.gate_type == gate_type,
            gates.c.status == "open",
        ]
        if await_id is None:
            match_conds.append(gates.c.await_id.is_(None))
        else:
            match_conds.append(gates.c.await_id == await_id)
        candidate_rows = (
            await conn.execute(select(gates.c.id).where(and_(*match_conds)))
        ).fetchall()
        for (cand_id,) in candidate_rows:
            existing_waiters = {
                r[0]
                for r in (
                    await conn.execute(
                        select(task_gates.c.task_id).where(task_gates.c.gate_id == cand_id)
                    )
                ).fetchall()
            }
            if existing_waiters == waiter_set:
                return cand_id, False, set()

        gate_id = "gate-" + uuid.uuid4().hex[:12]
        try:
            await conn.execute(
                insert(gates).values(
                    id=gate_id,
                    project_id=project_id,
                    gate_type=gate_type,
                    title=title,
                    question=question,
                    await_id=await_id,
                    timeout_at=timeout_at,
                    status="open",
                    created_at=now,
                )
            )
        except IntegrityError:
            # Partial unique index (uq_gates_open_dedup) fired — a
            # concurrent tx inserted an open gate with the same
            # (project_id, gate_type, await_id) between our SELECT and
            # INSERT (Postgres READ COMMITTED). Return the winner.
            # SQLite serializes writers so this arm is normally cold
            # on SQLite (Postgres exercises the real race).
            #
            # Recovery re-SELECTs on a *fresh* connection, which is only
            # safe when we own the surrounding transaction — a caller-
            # supplied ``conn`` (e.g. worker filing's ``immediate()``)
            # cannot safely open a second write connection (SQLite holds
            # a non-reentrant lock there), so re-raise and let the whole
            # caller transaction roll back instead.
            if caller_owns_conn:
                raise
            gate_id, was_created = await self._resolve_open_gate_winner(
                project_id=project_id,
                gate_type=gate_type,
                await_id=await_id,
            )
            return gate_id, was_created, set()
        for tid in waiters:
            await conn.execute(insert(task_gates).values(task_id=tid, gate_id=gate_id))
        flipped: set[str] = set()
        if waiters:
            flipped = await self.recompute_blocked(set(waiters), conn=conn)
        return gate_id, True, flipped

    async def _resolve_open_gate_winner(
        self,
        *,
        project_id: str,
        gate_type: str,
        await_id: str | None,
    ) -> tuple[str, bool]:
        """Re-SELECT the winning open gate after an IntegrityError on insert."""
        match_conds = [
            gates.c.project_id == project_id,
            gates.c.gate_type == gate_type,
            gates.c.status == "open",
        ]
        if await_id is None:
            match_conds.append(gates.c.await_id.is_(None))
        else:
            match_conds.append(gates.c.await_id == await_id)
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(select(gates.c.id).where(and_(*match_conds)))
            ).fetchone()
        if row is None:
            # Extremely unlikely — the winner resolved before we could
            # look it up. Bubble up as the same IntegrityError shape.
            raise IntegrityError(
                "create_gate: unique-index conflict but no open winner",
                params=None,
                orig=None,
            )
        return row[0], False

    async def resolve_gate(
        self,
        gate_id: str,
        *,
        resolved_by: str,
        resolution: str = "",
    ) -> set[str]:
        """Resolve *gate_id* (idempotent) and recompute its waiters.

        Returns the set of task ids whose ``is_blocked`` flipped as a result.
        Resolving an already-``resolved`` gate is a no-op and returns the
        empty set.  Resolving an ``expired`` gate is allowed — the caller
        may explicitly close a timed-out gate to unblock waiters.
        """
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(select(gates.c.status).where(gates.c.id == gate_id))
            ).fetchone()
            if row is None:
                return set()
            if row[0] == "resolved":
                return set()

            waiters = {
                r[0]
                for r in (
                    await conn.execute(
                        select(task_gates.c.task_id).where(task_gates.c.gate_id == gate_id)
                    )
                ).fetchall()
            }
            await conn.execute(
                update(gates)
                .where(gates.c.id == gate_id)
                .values(status="resolved", resolved_by=resolved_by, resolution=resolution)
            )
            flipped = await self.recompute_blocked(waiters, conn=conn) if waiters else set()
            ready_ids = await self._note_frontier_entry(conn, set(flipped), reason="unblocked")
        await self.log_blocked_flips(flipped)
        await self._notify_ready([(tid, "unblocked") for tid in ready_ids])
        return flipped

    async def expire_open_gates(self, now: float) -> list[str]:
        """Mark every ``open`` gate with ``timeout_at <= now`` ``expired``.

        Expiry deliberately keeps blocking (design §5.4): a timed-out
        approval must never silently self-approve.  Waiters therefore need
        no recompute — the projection was already 1 and stays 1.  Returns
        the list of gate ids that were expired this call.
        """
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(gates.c.id).where(
                        and_(
                            gates.c.status == "open",
                            gates.c.timeout_at.isnot(None),
                            gates.c.timeout_at <= now,
                        )
                    )
                )
            ).fetchall()
            expired = [r[0] for r in rows]
            if expired:
                await conn.execute(
                    update(gates)
                    .where(gates.c.id.in_(expired))
                    .values(status="expired")
                )
        return expired

    async def list_gates(
        self,
        project_id: str | None = None,
        status: str | None = None,
        gate_type: str | None = None,
    ) -> list[dict]:
        """List gates with optional filters, newest first."""
        stmt = select(gates)
        conditions = []
        if project_id is not None:
            conditions.append(gates.c.project_id == project_id)
        if status is not None:
            conditions.append(gates.c.status == status)
        if gate_type is not None:
            conditions.append(gates.c.gate_type == gate_type)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(gates.c.created_at.desc())
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [_row_to_gate(r) for r in rows]

    async def get_gate(self, gate_id: str) -> dict | None:
        """Return the gate row as a dict, or ``None``."""
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(select(gates).where(gates.c.id == gate_id))
            ).mappings().fetchone()
        return _row_to_gate(row) if row else None

    async def get_gates_for_task(self, task_id: str) -> list[dict]:
        """Return every gate attached to *task_id*, newest first."""
        stmt = (
            select(gates)
            .select_from(gates.join(task_gates, task_gates.c.gate_id == gates.c.id))
            .where(task_gates.c.task_id == task_id)
            .order_by(gates.c.created_at.desc())
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [_row_to_gate(r) for r in rows]

    async def list_open_gates_by_type(self, gate_type: str) -> list[dict]:
        """Return every ``open`` gate of a given type, oldest first."""
        stmt = (
            select(gates)
            .where(and_(gates.c.status == "open", gates.c.gate_type == gate_type))
            .order_by(gates.c.created_at.asc())
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [_row_to_gate(r) for r in rows]

    async def get_gate_waiters(self, gate_id: str) -> set[str]:
        """Return the set of task ids attached to *gate_id*."""
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(task_gates.c.task_id).where(task_gates.c.gate_id == gate_id)
                )
            ).fetchall()
        return {r[0] for r in rows}

    async def list_gate_waiters_for_project(self, project_id: str) -> dict[str, list[str]]:
        """``gate_id -> sorted waiter task ids`` for every gate in *project_id*.

        One statement for the whole project; the graph endpoint used to ask
        ``get_gate_waiters`` once per gate.
        """
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(task_gates.c.gate_id, task_gates.c.task_id)
                    .select_from(task_gates.join(gates, gates.c.id == task_gates.c.gate_id))
                    .where(gates.c.project_id == project_id)
                    .order_by(task_gates.c.gate_id.asc(), task_gates.c.task_id.asc())
                )
            ).fetchall()
        out: dict[str, list[str]] = {}
        for gate_id, task_id in rows:
            out.setdefault(gate_id, []).append(task_id)
        return out
