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
import sys

#: Databases that must never be touched by the kit, whatever is passed in.
#: ``agent_queue`` is the developer's real daemon; the ``_gwN`` databases
#: belong to the parallel unit suite.
PROTECTED = {"postgres", "template0", "template1", "agent_queue", "agent_queue_master"}


async def main(admin_dsn: str, db_name: str, reset: bool) -> int:
    import asyncpg

    if db_name in PROTECTED or db_name.startswith("agent_queue_gw"):
        print(f"refusing to manage protected database '{db_name}'", file=sys.stderr)
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
