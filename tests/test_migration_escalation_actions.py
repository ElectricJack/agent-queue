"""The verified-reply action reservation revision upgrades on PostgreSQL."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.migration, pytest.mark.integration]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTGRES_DSN = ensure_worker_postgres_dsn()


def alembic(dsn: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=dsn),
        capture_output=True,
        text=True,
        check=False,
    )


async def test_upgrade_from_a6_creates_action_relation_and_downgrade_removes_it():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("escalationactionmigration")
    before = alembic(dsn, "upgrade", "a00000000006")
    assert before.returncode == 0, before.stderr

    import asyncpg

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute('DROP TABLE IF EXISTS "escalation_actions" CASCADE')
    finally:
        await conn.close()

    upgraded = alembic(dsn, "upgrade", "a00000000007")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        assert await conn.fetchval("SELECT to_regclass('public.escalation_actions')")
        constraints = {
            row["conname"]
            for row in await conn.fetch(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'escalation_actions'::regclass"
            )
        }
        assert "uq_escalation_actions_idempotency" in constraints
        assert "ck_escalation_actions_completion" in constraints
    finally:
        await conn.close()

    downgraded = alembic(dsn, "downgrade", "a00000000006")
    assert downgraded.returncode == 0, downgraded.stderr
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        assert await conn.fetchval("SELECT to_regclass('public.escalation_actions')") is None
    finally:
        await conn.close()
