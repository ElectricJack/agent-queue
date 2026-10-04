"""The digest revision follows the collected siblings and preserves author requests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.migration, pytest.mark.integration]

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_DSN = ensure_worker_postgres_dsn()
REVISION = "a00000000070"
PRECEDING = previous_revision(REVISION)
TABLE = "supervisor_report_requests"
CONSTRAINT = "ck_supervisor_report_requests_kind"


def alembic(dsn: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=dsn),
        capture_output=True,
        text=True,
        check=False,
    )


async def connect(dsn: str):
    return await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://", 1))


async def legacy_database(suffix: str) -> str:
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database(suffix)
    result = alembic(dsn, "upgrade", PRECEDING)
    assert result.returncode == 0, result.stderr
    # The squashed baseline uses live metadata. Restore the pre-digest
    # constraint so this exercises an installed database's incremental upgrade.
    conn = await connect(dsn)
    try:
        await conn.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {CONSTRAINT}")
        await conn.execute(
            f"ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT} CHECK (kind IN ('hourly','morning'))"
        )
    finally:
        await conn.close()
    return dsn


async def insert_request(dsn: str, kind: str) -> None:
    conn = await connect(dsn)
    try:
        await conn.execute(
            f"INSERT INTO {TABLE} (id, kind, owner_ref, destination, visibility, brief, "
            "brief_hash, fallback_text, author_session_id, deadline, created_at, updated_at) "
            "VALUES ($1, $1, $1, 'discord:1', '[]'::jsonb, '{}'::jsonb, "
            "'hash', 'fallback', 'supervisor', 1000, 100, 100)",
            kind,
        )
    finally:
        await conn.close()


async def test_upgrade_preserves_reports_and_admits_digest_requests():
    dsn = await legacy_database("digestkind")
    await insert_request(dsn, "hourly")
    await insert_request(dsn, "morning")
    with pytest.raises(asyncpg.CheckViolationError, match=CONSTRAINT):
        await insert_request(dsn, "digest")

    result = alembic(dsn, "upgrade", REVISION)
    assert result.returncode == 0, result.stderr
    await insert_request(dsn, "digest")
    conn = await connect(dsn)
    try:
        assert await conn.fetchval(f"SELECT count(*) FROM {TABLE}") == 3
    finally:
        await conn.close()


async def test_upgrade_is_idempotent_and_downgrade_restores_report_kinds():
    dsn = await legacy_database("digestkindtwice")
    for command in (("upgrade", REVISION), ("stamp", PRECEDING), ("upgrade", "head")):
        result = alembic(dsn, *command)
        assert result.returncode == 0, result.stderr
    result = alembic(dsn, "downgrade", PRECEDING)
    assert result.returncode == 0, result.stderr
    await insert_request(dsn, "hourly")
    await insert_request(dsn, "morning")
    with pytest.raises(asyncpg.CheckViolationError, match=CONSTRAINT):
        await insert_request(dsn, "digest")


async def test_downgrade_refuses_to_discard_existing_digest_requests():
    dsn = await legacy_database("digestkindrollback")
    result = alembic(dsn, "upgrade", REVISION)
    assert result.returncode == 0, result.stderr
    await insert_request(dsn, "digest")
    result = alembic(dsn, "downgrade", PRECEDING)
    assert result.returncode != 0
    assert "supervisor-authored digest requests exist" in result.stderr
    conn = await connect(dsn)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == REVISION
        assert await conn.fetchval(f"SELECT kind FROM {TABLE}") == "digest"
    finally:
        await conn.close()
