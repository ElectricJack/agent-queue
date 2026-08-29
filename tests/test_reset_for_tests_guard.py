"""``reset_for_tests()`` refuses to run against anything but a test target.

Both adapters' ``reset_for_tests()`` truncate every table -- a caller with
the wrong DSN/path (a slipped env var, a copy-pasted fixture) would destroy a
real database. The guard check runs before any engine access, so these tests
exercise it without a live Postgres server or a real SQLite file.
"""

from __future__ import annotations

import pytest

from src.database import Database
from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

#: Deliberately outside any temp directory -- a stand-in for "someone's real
#: database file", never actually opened (the guard raises first).
NOT_A_TEMP_PATH = "/etc/definitely-not-a-tempdir/agentqueue.db"


class TestSQLiteResetGuard:
    async def test_refuses_path_outside_tempdir(self, monkeypatch):
        monkeypatch.delenv("AQ_ALLOW_DB_RESET", raising=False)
        db = Database(NOT_A_TEMP_PATH)
        with pytest.raises(RuntimeError, match="reset_for_tests refused"):
            await db.reset_for_tests()

    async def test_allows_path_under_tempdir(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AQ_ALLOW_DB_RESET", raising=False)
        db = Database(str(tmp_path / "ok.db"))
        await db.initialize()
        await db.reset_for_tests()  # must not raise
        await db.close()

    async def test_allow_db_reset_env_overrides_path_check(self, monkeypatch):
        monkeypatch.setenv("AQ_ALLOW_DB_RESET", "1")
        db = Database(NOT_A_TEMP_PATH)
        # Guard passes; no engine was ever initialized, so this returns
        # quietly rather than touching a real file.
        await db.reset_for_tests()


class TestPostgresResetGuard:
    async def test_refuses_mismatched_dsn(self, monkeypatch):
        monkeypatch.delenv("AQ_ALLOW_DB_RESET", raising=False)
        monkeypatch.delenv("POSTGRES_TEST_DSN", raising=False)
        db = PostgreSQLDatabaseAdapter("postgresql+asyncpg://user:pass@localhost/prod")
        with pytest.raises(RuntimeError, match="reset_for_tests refused"):
            await db.reset_for_tests()

    async def test_allows_dsn_matching_postgres_test_dsn(self, monkeypatch):
        dsn = "postgresql+asyncpg://user:pass@localhost/agent_queue_test"
        monkeypatch.setenv("POSTGRES_TEST_DSN", dsn)
        db = PostgreSQLDatabaseAdapter(dsn)
        await db.reset_for_tests()  # must not raise; no engine to touch

    async def test_allow_db_reset_env_overrides_dsn_check(self, monkeypatch):
        monkeypatch.delenv("POSTGRES_TEST_DSN", raising=False)
        monkeypatch.setenv("AQ_ALLOW_DB_RESET", "1")
        db = PostgreSQLDatabaseAdapter("postgresql+asyncpg://user:pass@localhost/prod")
        await db.reset_for_tests()
