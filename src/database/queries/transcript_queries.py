"""Durable read position for each on-disk transcript file.

The transcript watcher's byte offset used to live only in process, keyed by
session id.  That is the wrong key: a session that dies and is relaunched on
the same workspace adopts the *same* transcript file, and the fresh session
id starts at offset 0 — so the file's whole history is re-emitted as agent
output and re-charged to the token ledger under the new id.  Three
consecutive supervisor incarnations each recorded an identical 133 ledger
rows for one window before this existed.

Keying the mark by transcript path is what makes it survive the session that
set it.  ``last_entry_uuid`` is the second half of the dedupe key: the newest
assistant entry whose usage was charged, so a reader resuming exactly on a
record boundary cannot charge it twice.
"""

from __future__ import annotations

import time

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from src.database.tables import transcript_checkpoints


class TranscriptQueryMixin:
    """Query mixin for transcript checkpoints.  Expects ``self._engine``."""

    async def get_transcript_checkpoint(self, transcript_path: str) -> dict | None:
        """The stored mark for *transcript_path*, or ``None`` if unseen."""
        stmt = select(
            transcript_checkpoints.c.byte_offset,
            transcript_checkpoints.c.last_entry_uuid,
            transcript_checkpoints.c.session_id,
            transcript_checkpoints.c.updated_at,
        ).where(transcript_checkpoints.c.transcript_path == str(transcript_path))
        async with self._engine.connect() as conn:
            row = (await conn.execute(stmt)).mappings().first()
        if row is None:
            return None
        return {
            "byte_offset": int(row["byte_offset"] or 0),
            "last_entry_uuid": row["last_entry_uuid"],
            "session_id": row["session_id"],
            "updated_at": float(row["updated_at"] or 0.0),
        }

    async def set_transcript_checkpoint(
        self,
        transcript_path: str,
        *,
        byte_offset: int,
        last_entry_uuid: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Advance the mark for *transcript_path*.

        Monotonic: an update never moves the offset backwards, so two live
        sessions pointed at one file cannot undo each other's progress.  The
        one exception is truncation, which the caller detects (the file is
        shorter than the mark) and signals by passing ``byte_offset=0`` —
        expressed here as "a zero offset always wins", because a rewritten
        file genuinely has to be read from its start again.
        """
        offset = max(0, int(byte_offset))
        now = time.time()
        values = {
            "byte_offset": offset,
            "last_entry_uuid": last_entry_uuid,
            "session_id": session_id,
            "updated_at": now,
        }
        async with self._engine.begin() as conn:
            stmt = update(transcript_checkpoints).where(
                transcript_checkpoints.c.transcript_path == str(transcript_path)
            )
            if offset > 0:
                stmt = stmt.where(transcript_checkpoints.c.byte_offset <= offset)
            result = await conn.execute(stmt.values(**values))
            if result.rowcount:
                return
        # No row updated: either the path is new, or the stored offset is
        # already ahead of this one.  Only the first case needs an insert,
        # and a racing writer makes that insert conflict — which is itself
        # proof the row now exists, so there is nothing left to do.
        try:
            async with self._engine.begin() as conn:
                await conn.execute(
                    insert(transcript_checkpoints).values(
                        transcript_path=str(transcript_path), **values
                    )
                )
        except IntegrityError:
            pass

    async def delete_transcript_checkpoint(self, transcript_path: str) -> None:
        """Forget the mark for *transcript_path* (test and repair surface)."""
        async with self._engine.begin() as conn:
            await conn.execute(
                delete(transcript_checkpoints).where(
                    transcript_checkpoints.c.transcript_path == str(transcript_path)
                )
            )
