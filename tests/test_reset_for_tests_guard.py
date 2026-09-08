"""PostgreSQL reset_for_tests requires an exact test DSN or an explicit override."""

from __future__ import annotations

import pytest

from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
from tests.db_fixtures import lease_dsn


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

async def test_live_leased_database_reset_requires_matching_dsn(monkeypatch):
    monkeypatch.delenv("AQ_ALLOW_DB_RESET", raising=False)
    dsn = lease_dsn("reset_guard")
    db = PostgreSQLDatabaseAdapter(dsn)
    await db.initialize()
    try:
        monkeypatch.setenv("POSTGRES_TEST_DSN", dsn + "_different")
        with pytest.raises(RuntimeError, match="reset_for_tests refused"):
            await db.reset_for_tests()
        monkeypatch.setenv("POSTGRES_TEST_DSN", dsn)
        await db.reset_for_tests()
    finally:
        await db.close()
