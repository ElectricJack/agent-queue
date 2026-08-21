"""Merge slot lease queries — atomic acquire/renew/release/break.

Worktree-execution implementation spec §5 / design §4.1.  One row per
project in ``merge_slots``; ``holder_task_id is NULL`` means the slot is
free.  Acquisition is a single atomic conditional UPDATE — seed the row
with ``INSERT OR IGNORE`` / ``ON CONFLICT DO NOTHING`` first, then the
UPDATE succeeds iff the slot is free, held by the caller, or the lease
has expired.  ``rowcount == 1`` means "acquired".

Concurrency model
-----------------
The daemon is a single Python process.  All lease mutators
(``acquire_merge_slot_row``, ``release_merge_slot_row``,
``break_expired_merge_slot_rows``) are serialized in-process by a shared
``asyncio.Lock`` (``_merge_slot_lock``).  This is the authoritative
mutual-exclusion mechanism for a running daemon.

Why the in-process lock?  The production SQLite engine uses
``StaticPool``, which hands out one physical DBAPI connection to every
concurrent checkout — so two ``async with engine.begin()`` blocks share
the same connection and their statements interleave across every await
point.  Even though each individual SQL statement is atomic, a mutator
that awaits mid-transaction (e.g. between the seed and the UPDATE) can
observe *another* mutator's committed change and end up with the wrong
result.  The in-process lock closes that window deterministically,
regardless of pool/dialect semantics.

The conditional UPDATE remains as the cross-restart authority: a
daemon restart cannot see the in-process lock, and the DB row's holder
+ ``expires_at`` fields are what let a fresh daemon adopt or break a
stale lease (see ``break_expired_merge_slot_rows`` and the callers in
``src/orchestrator/merge_slot.py`` / ``src/orchestrator/git_ops.py``).

Cross-process concurrent acquire is *not* a supported topology.

Dialect notes
-------------
SQLite: the seed + UPDATE run inside the default (deferred) transaction.
Correctness comes from the in-process lock above, not from the SQL
transaction mode.  On ``SQLITE_BUSY`` we return False (contention, not
error) so callers can retry.
PostgreSQL: the conditional UPDATE takes the row lock; the in-process
lock still serializes callers in the single daemon process.
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from src.database.tables import merge_slots
from src.models import MergeSlot


class MergeSlotQueriesMixin:
    """Query mixin for ``merge_slots``.  Expects ``self._engine``.

    Provides a lazily-initialized ``asyncio.Lock`` used by every mutator
    (acquire/release/break) to serialize lease mutations in-process.
    """

    def _get_merge_slot_lock(self) -> asyncio.Lock:
        """Return the mixin's shared ``asyncio.Lock``, creating it lazily.

        Lazy construction avoids binding the lock to a particular event
        loop at ``__init__`` time (adapter instances outlive any given
        loop in tests).
        """
        lock = getattr(self, "_merge_slot_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._merge_slot_lock = lock
        return lock

    async def _seed_merge_slot(self, conn, project_id: str, now: float) -> None:
        """Insert a NULL-holder row for *project_id* if none exists.

        Idempotent on every dialect: SQLite ``INSERT OR IGNORE`` and PG
        ``ON CONFLICT DO NOTHING``.
        """
        dialect = conn.dialect.name
        values = {
            "project_id": project_id,
            "holder_task_id": None,
            "acquired_at": None,
            "expires_at": None,
            "updated_at": now,
        }
        if dialect == "sqlite":
            stmt = sqlite_insert(merge_slots).values(**values).on_conflict_do_nothing(
                index_elements=["project_id"]
            )
        elif dialect == "postgresql":
            stmt = pg_insert(merge_slots).values(**values).on_conflict_do_nothing(
                index_elements=["project_id"]
            )
        else:  # generic fallback
            existing = await conn.execute(
                select(merge_slots.c.project_id).where(merge_slots.c.project_id == project_id)
            )
            if existing.fetchone() is not None:
                return
            stmt = insert(merge_slots).values(**values)
        await conn.execute(stmt)

    async def acquire_merge_slot_row(
        self, project_id: str, task_id: str, ttl: float
    ) -> bool:
        """Atomic conditional acquire.  Returns True iff the slot is now held
        by *task_id*.

        Spec §5:
          UPDATE merge_slots
             SET holder_task_id = :task, acquired_at = :now,
                 expires_at = :now + :ttl, updated_at = :now
           WHERE project_id = :project
             AND (holder_task_id IS NULL OR holder_task_id = :task
                  OR expires_at < :now)

        A re-acquire by the current holder renews the lease (idempotent).

        In-process mutual exclusion is enforced by ``_merge_slot_lock``:
        the entire seed+UPDATE runs under the lock so no other mutator
        (acquire/release/break) can interleave while we're mid-transaction.
        On ``SQLITE_BUSY`` we return False (contention → caller retries).
        """
        now = time.time()
        async with self._get_merge_slot_lock():
            try:
                async with self._engine.begin() as conn:
                    await self._seed_merge_slot(conn, project_id, now)
                    result = await conn.execute(
                        update(merge_slots)
                        .where(
                            (merge_slots.c.project_id == project_id)
                            & (
                                merge_slots.c.holder_task_id.is_(None)
                                | (merge_slots.c.holder_task_id == task_id)
                                | (merge_slots.c.expires_at < now)
                            )
                        )
                        .values(
                            holder_task_id=task_id,
                            acquired_at=now,
                            expires_at=now + ttl,
                            updated_at=now,
                        )
                    )
                    return result.rowcount == 1
            except OperationalError as e:
                msg = str(e).lower()
                if "locked" in msg or "busy" in msg:
                    return False
                raise

    async def release_merge_slot_row(self, project_id: str, task_id: str) -> None:
        """Clear the lease iff *task_id* still holds it.  No-op otherwise.

        Serialized against ``acquire_merge_slot_row`` /
        ``break_expired_merge_slot_rows`` via ``_merge_slot_lock``.
        """
        now = time.time()
        async with self._get_merge_slot_lock():
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(merge_slots)
                    .where(
                        (merge_slots.c.project_id == project_id)
                        & (merge_slots.c.holder_task_id == task_id)
                    )
                    .values(
                        holder_task_id=None,
                        acquired_at=None,
                        expires_at=None,
                        updated_at=now,
                    )
                )

    async def break_expired_merge_slot_rows(self) -> list[str]:
        """Clear every lease whose ``expires_at < now``.  Returns the list of
        project ids that were broken (for event emission by the caller).

        Serialized against the other mutators via ``_merge_slot_lock`` so
        no acquire/release can slip in between the expired-scan and the
        clearing UPDATE.
        """
        now = time.time()
        async with self._get_merge_slot_lock():
            async with self._engine.begin() as conn:
                expired = (
                    await conn.execute(
                        select(merge_slots.c.project_id).where(
                            merge_slots.c.holder_task_id.isnot(None)
                            & merge_slots.c.expires_at.isnot(None)
                            & (merge_slots.c.expires_at < now)
                        )
                    )
                ).scalars().all()
                if not expired:
                    return []
                await conn.execute(
                    update(merge_slots)
                    .where(merge_slots.c.project_id.in_(list(expired)))
                    .values(
                        holder_task_id=None,
                        acquired_at=None,
                        expires_at=None,
                        updated_at=now,
                    )
                )
                return list(expired)

    async def get_merge_slot(self, project_id: str) -> MergeSlot | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(merge_slots).where(merge_slots.c.project_id == project_id)
                )
            ).mappings().fetchone()
        if row is None:
            return None
        return MergeSlot(
            project_id=row["project_id"],
            holder_task_id=row["holder_task_id"],
            acquired_at=row["acquired_at"],
            expires_at=row["expires_at"],
            updated_at=row["updated_at"] or 0.0,
        )
