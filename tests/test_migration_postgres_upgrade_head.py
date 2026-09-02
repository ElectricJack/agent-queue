"""PostgreSQL smoke test for the complete Alembic revision chain.

SQLite accepts ``BOOLEAN DEFAULT 0`` but PostgreSQL rejects it. Running the
whole chain on an empty PostgreSQL database catches dialect-specific DDL before
PostgreSQL adapter fixtures attempt to create their schemas.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.pg_dsn import ensure_worker_postgres_dsn

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _alembic_pg(dsn: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=dsn),
        capture_output=True,
        text=True,
        check=False,
    )


async def _version_num(dsn: str) -> str:
    import asyncpg

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        return await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()


async def test_upgrade_head_applies_the_whole_chain_on_an_empty_postgres_database():
    """An empty production-backend database upgrades to the current head."""
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")
    from tests.pg_dsn import create_scratch_database

    dsn = await create_scratch_database("upgrade_head")
    result = _alembic_pg(dsn, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    heads = _alembic_pg(dsn, "heads")
    assert heads.returncode == 0, heads.stderr
    assert await _version_num(dsn) == heads.stdout.split()[0]
