"""Durable wait source writes, watermark reads and bounded retention."""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import delete, func, select, text

from src.database.tables import wait_listener_cursors, wait_source_events
from src.waits.sources import _SOURCE_SEQUENCE_LOCK, record_source_event


class WaitSourceQueriesMixin:
    async def wait_source_commit(self, *, conn=None, **event) -> int | None:
        """Use the owning transaction, or commit an external observation atomically."""
        if conn is not None:
            return await record_source_event(conn, **event)
        async with self._engine.begin() as owned:
            return await record_source_event(owned, **event)

    async def wait_source_batch_commit(self, events, *, conn=None):
        """Commit a measurement's source rows and notification in one statement."""
        from src.waits.source_sql import source_batch_statement

        if not events:
            return []
        if conn is not None:
            return list((await conn.execute(source_batch_statement(events))).scalars())
        async with self._engine.begin() as owned:
            return list((await owned.execute(source_batch_statement(events))).scalars())

    async def wait_source_watermark(self, *, source_key=None) -> int:
        """Read after subscribing, then drain through this committed high watermark.

        A dedicated, short read transaction exclusively fences event allocation:
        MAX(seq) alone can skip an earlier sequence committed late. Release this
        fence before dispatching or locking source/owner rows, then drain through
        the returned bound. Registration takes this before its mutation transaction
        and re-observes its source afterwards. READ COMMITTED gives each lock
        attempt and the final read a fresh snapshot, even on a stricter engine.
        Cursor progress supplies a floor
        after retention removes old rows.
        """
        async def read(active):
            stmt = select(func.max(wait_source_events.c.seq))
            if source_key is not None:
                stmt = stmt.where(wait_source_events.c.source_key == source_key)
            high = (await active.execute(stmt)).scalar_one() or 0
            cursor = (await active.execute(select(wait_listener_cursors.c.last_seq).where(
                wait_listener_cursors.c.listener_key == "source",
            ))).scalar_one_or_none() or 0
            return max(high, cursor)
        async with self._engine.connect() as connection:
            owned = await connection.execution_options(isolation_level="READ COMMITTED")
            async with owned.begin():
                # Never queue an exclusive lock behind writers: a later writer may
                # hold a source row an earlier writer still needs. A queued reader
                # would block its shared lock and invert that existing row order.
                async with asyncio.timeout(30):
                    while not await owned.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                                 {"key": _SOURCE_SEQUENCE_LOCK}):
                        await asyncio.sleep(0.01)
                return await read(owned)

    async def purge_wait_source_events(self, *, now=None, limit=1000) -> int:
        """Remove dispatched rows older than seven days, never unread cursor history."""
        now = time.time() if now is None else now
        cursor = select(wait_listener_cursors.c.last_seq).where(
            wait_listener_cursors.c.listener_key == "source",
        ).scalar_subquery()
        candidates = select(wait_source_events.c.seq).where(
            wait_source_events.c.committed_at < now - 7 * 86400,
            wait_source_events.c.dispatched_at.is_not(None),
            wait_source_events.c.seq <= cursor,
        ).order_by(wait_source_events.c.seq).limit(limit).with_for_update(skip_locked=True)
        async with self._engine.begin() as conn:
            result = await conn.execute(delete(wait_source_events).where(
                wait_source_events.c.seq.in_(candidates),
            ))
            return result.rowcount

    async def fire_wait_due_deadline(self, wait_id: str, generation: int, *, now=None, conn=None):
        """CAS a due timer and its source event together; stale/cancelled fires are no-ops.

        Escalation/ceiling/transition/reservation deadlines belong to their drivers;
        only a due deadline is a timer condition and can produce this source event.
        """
        from sqlalchemy import update

        from src.database.tables import wait_deadlines, wait_records

        now = time.time() if now is None else now

        newer = wait_deadlines.alias("newer")

        async def fire(active):
            row = (await active.execute(update(wait_deadlines).where(
                wait_deadlines.c.wait_id == wait_id, wait_deadlines.c.deadline_type == "due",
                wait_deadlines.c.generation == generation, wait_deadlines.c.due_at <= now,
                wait_deadlines.c.fired_at.is_(None), wait_deadlines.c.cancelled_at.is_(None),
                ~select(newer.c.id).where(
                    # The unique key alone doesn't fence an older, still-unfired generation.
                    newer.c.wait_id == wait_id, newer.c.deadline_type == "due",
                    newer.c.generation > generation,
                ).exists(),
            ).values(fired_at=now).returning(wait_deadlines))).mappings().first()
            if row is None:
                return None
            source = (await active.execute(select(wait_records).where(
                wait_records.c.id == wait_id,
            ))).mappings().one()
            seq = await record_source_event(
                active, source_key=source["source_key"],
                generation=source["source_generation"], revision=f"due:{generation}",
                outcome_ref=f"deadline:{row['id']}", occurred_at=row["due_at"],
                payload={"wait_id": wait_id, "deadline_id": row["id"],
                         "due_at": row["due_at"], "generation": generation},
            )
            return seq
        if conn is not None:
            return await fire(conn)
        async with self._engine.begin() as owned:
            return await fire(owned)
