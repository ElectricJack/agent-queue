"""Named advisory locks held across slow work without an open transaction.

``pg_advisory_xact_lock`` on a connection that then awaits work done on other
connections leaves that backend ``idle in transaction`` until the work ends.
On 2026-10-27 one source-CI observation sat that way for ~20 minutes across
Git I/O (brisk-glacier-94). A session lock on a dedicated AUTOCOMMIT
connection gives the same exclusion while the backend stays plain ``idle``:
no transaction, no snapshot. The key is ``hashtextextended(name, 0)``, the key
space the transaction locks used, so a daemon still running the old code
excludes and is excluded by this one.

Holders are registered in-process so ``aq doctor --check
db.idle_advisory_locks`` can name the work behind a key.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from sqlalchemy import text

#: (backend pid, int8 key) -> (lock name, acquired at) for this process.
_HELD: dict[tuple[int, int], tuple[str, float]] = {}


def held_advisory_locks() -> dict[tuple[int, int], tuple[str, float]]:
    """Snapshot of the named session locks this process holds."""
    return dict(_HELD)


@asynccontextmanager
async def session_advisory_lock(engine, name: str):
    """Serialize on ``name`` across daemons without holding a transaction."""
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        held = None
        try:
            row = (await conn.execute(text(
                "SELECT pg_backend_pid(), hashtextextended(:name, 0)"), {"name": name})).one()
            held = (int(row[0]), int(row[1]))
            # Cleanup also covers cancellation while acquisition returns: the
            # server may already have granted the session lock.
            await conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": held[1]})
            _HELD[held] = (name, time.time())
            yield
        finally:
            if held is not None:
                _HELD.pop(held, None)
            # Only this scope uses the dedicated connection.  A lock that
            # cannot be released must not return to the pool with it.
            try:
                await conn.execute(text("SELECT pg_advisory_unlock_all()"))
            except BaseException:
                await conn.invalidate()
                raise
