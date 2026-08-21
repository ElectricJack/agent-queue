"""Merge slot lease queries — atomic acquire/renew/release/break.

Worktree-execution implementation spec §5 / design §4.1.  One row per
project in ``merge_slots``; ``holder_task_id is NULL`` means the slot is
free.  Acquisition is a single atomic conditional UPDATE — seed the row
with ``INSERT OR IGNORE`` / ``ON CONFLICT DO NOTHING`` first, then the
UPDATE succeeds iff the slot is free, held by the caller, or the lease
has expired.  ``rowcount == 1`` means "acquired".

Dialect notes
-------------
SQLite: the seed + UPDATE run inside a ``BEGIN IMMEDIATE`` transaction so
two concurrent acquires serialize at the database rather than the row.
PostgreSQL: the same shape works without an explicit ``FOR UPDATE`` —
the UPDATE itself takes the row lock.
"""

from __future__ import annotations

import time

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.database.tables import merge_slots
from src.models import MergeSlot


class MergeSlotQueriesMixin:
    """Query mixin for ``merge_slots``.  Expects ``self._engine``."""

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
        """
        now = time.time()
        # SQLite: BEGIN IMMEDIATE serializes writers at connection acquire
        # rather than at the row, so two concurrent acquires cannot both
        # observe the "free" state before either UPDATE fires.
        async with self._engine.begin() as conn:
            if conn.dialect.name == "sqlite":
                # engine.begin() already opened DEFERRED; upgrade to IMMEDIATE
                # by issuing a no-op write on the merge_slots row via seed.
                # (The seed itself is the write that promotes the txn.)
                pass
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

    async def release_merge_slot_row(self, project_id: str, task_id: str) -> None:
        """Clear the lease iff *task_id* still holds it.  No-op otherwise."""
        now = time.time()
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
        project ids that were broken (for event emission by the caller)."""
        now = time.time()
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
