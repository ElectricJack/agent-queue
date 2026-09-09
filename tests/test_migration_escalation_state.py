"""The post-squash escalation revision upgrades and rolls back on PostgreSQL."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.migration, pytest.mark.integration]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTGRES_DSN = ensure_worker_postgres_dsn()
TABLES = ("escalations", "escalation_messages", "escalation_deliveries", "digest_windows")


def alembic(dsn: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=dsn),
        capture_output=True,
        text=True,
        check=False,
    )


async def connection(dsn: str):
    import asyncpg

    return await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))


async def test_upgrade_from_a4_creates_relations_and_downgrade_removes_them():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("escalationmigration")
    before = alembic(dsn, "upgrade", "a00000000004")
    assert before.returncode == 0, before.stderr

    conn = await connection(dsn)
    try:
        # The live-metadata baseline already knows about post-baseline tables.
        # Remove them to reproduce a real existing a4 installation.
        for table in reversed(TABLES):
            await conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    finally:
        await conn.close()

    upgraded = alembic(dsn, "upgrade", "a00000000005")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await connection(dsn)
    try:
        assert {
            row["tablename"]
            for row in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename = ANY($1::text[])",
                list(TABLES),
            )
        } == set(TABLES)
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('p','P',0)")
        await conn.execute(
            "INSERT INTO escalations "
            "(id, project_id, source_kind, source_identity, incident_key, supervisor_owner, "
            "summary, investigation, decision_requested, severity, created_at, updated_at) "
            "VALUES ('e','p','task_attempt','attempt-1','incident-1','supervisor-p',"
            "'blocked','checked','choose','high',0,0)"
        )
        with pytest.raises(Exception, match="uq_escalations_incident"):
            await conn.execute(
                "INSERT INTO escalations "
                "(id, project_id, source_kind, source_identity, incident_key, supervisor_owner, "
                "summary, investigation, decision_requested, severity, created_at, updated_at) "
                "VALUES ('e2','p','task_attempt','attempt-1','incident-1','supervisor-p',"
                "'blocked','checked','choose','high',0,0)"
            )
    finally:
        await conn.close()

    downgraded = alembic(dsn, "downgrade", "a00000000004")
    assert downgraded.returncode == 0, downgraded.stderr
    conn = await connection(dsn)
    try:
        remaining = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename = ANY($1::text[])",
            list(TABLES),
        )
        assert remaining == []
    finally:
        await conn.close()
