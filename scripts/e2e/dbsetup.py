#!/usr/bin/env python3
"""Create (or drop and recreate) the isolated e2e PostgreSQL database.

``psql`` is not assumed to be on the host — the daemon already depends on
``asyncpg``, so this uses it directly against the port docker-compose
publishes.

Usage::

    python3 scripts/e2e/dbsetup.py <admin-dsn> <db-name> [--reset]

The admin DSN must point at a database other than *db-name* (``postgres``
is the natural choice) because a database cannot be dropped while anything
is connected to it — including this script.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

#: Databases that must never be touched by the kit, whatever is passed in.
#: ``agent_queue`` is the developer's real daemon.
PROTECTED = {"postgres", "template0", "template1", "agent_queue"}

#: The per-xdist-worker databases the unit suite creates.
#: ``tests/pg_dsn.py`` derives ``{base}_{worker}`` where *worker* is
#: ``master`` outside ``-n`` and ``gwN`` under it — so the shape, not a
#: hardcoded ``agent_queue_`` prefix, is what identifies them.  Dropping one
#: mid-run corrupts whichever worker owns it.
WORKER_DB_RE = re.compile(r"_(master|gw\d+)$")

#: A database name goes straight into ``DROP DATABASE "<name>"`` — there is
#: no bind-parameter form of that statement.  Quoting makes injection hard
#: rather than impossible (a name containing ``"`` would close the quote),
#: so the name is validated as a plain identifier first and the quoting is
#: belt-and-braces.
SAFE_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def refuse(db_name: str) -> str | None:
    """Why *db_name* must not be managed by this kit, or ``None``."""
    if not SAFE_NAME_RE.match(db_name):
        return "not a plain identifier (^[a-z0-9_]+$)"
    if db_name in PROTECTED:
        return "protected database"
    if WORKER_DB_RE.search(db_name):
        return "looks like a pytest-xdist worker database (tests/pg_dsn.py)"
    # Whatever POSTGRES_TEST_DSN currently names, and its worker databases.
    test_dsn = os.environ.get("POSTGRES_TEST_DSN", "")
    if test_dsn:
        base = test_dsn.rpartition("/")[2].partition("?")[0]
        if base and db_name == base:
            return "this is POSTGRES_TEST_DSN's database"
    return None


async def main(admin_dsn: str, db_name: str, reset: bool) -> int:
    import asyncpg

    reason = refuse(db_name)
    if reason is not None:
        print(f"refusing to manage '{db_name}': {reason}", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(admin_dsn)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if exists and reset:
            # Terminate stragglers first: a leftover daemon connection would
            # make DROP DATABASE fail with "is being accessed by other users".
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                db_name,
            )
            await conn.execute(f'DROP DATABASE "{db_name}"')
            exists = None
            print(f"dropped database {db_name}")
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f"created database {db_name}")
        else:
            print(f"database {db_name} already exists")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--reset"]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(args[0], args[1], "--reset" in sys.argv[1:])))
