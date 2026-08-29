"""Shared fixtures for statement-count/latency perf tests (spec §15).

``any_db`` parametrises over SQLite (always) and Postgres (only when
``POSTGRES_TEST_DSN`` is set) so the same budget assertions run against both
backends — SQLite proves the statement count; Postgres additionally proves
the CAS semantics under a real second connection/backend.
"""

from __future__ import annotations

import os

import pytest

POSTGRES_TEST_DSN = os.environ.get("POSTGRES_TEST_DSN")


@pytest.fixture(params=["sqlite", "postgres"])
async def any_db(request, tmp_path):
    if request.param == "postgres":
        if not POSTGRES_TEST_DSN:
            pytest.skip("POSTGRES_TEST_DSN not set")
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        db = PostgreSQLDatabaseAdapter(POSTGRES_TEST_DSN)
        await db.initialize()
        await db.reset_for_tests()
    else:
        from src.database import Database

        db = Database(str(tmp_path / "perf.db"))
        await db.initialize()
    yield db
    await db.close()
