"""Revision a00000000078: source-CI infrastructure bookkeeping, additive.

The squashed baseline is built from live metadata, so a fresh database already
has these columns; the first test drops them to exercise the real additive path
over a pre-revision table, and the second proves the revision body is replayable
on a database that already carries them.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.migration, pytest.mark.integration]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTGRES_DSN = ensure_worker_postgres_dsn()
REVISION = "a00000000078"
PRECEDING = previous_revision(REVISION)
TABLE = "integration_source_ci"
COLUMNS = ("infra_observations", "infra_rerun_at", "infra_reruns")
CONSTRAINTS = (
    "ck_integration_source_ci_infra_observations",
    "ck_integration_source_ci_infra_reruns",
)


def alembic(dsn: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(
            os.environ, AGENT_QUEUE_DB_URL=dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        ),
        capture_output=True,
        text=True,
        check=False,
    )


async def connect(dsn: str):
    import asyncpg

    return await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))


async def _shape(dsn: str) -> tuple[set[str], set[str]]:
    """The columns and check constraints this revision owns, as PostgreSQL sees them."""
    conn = await connect(dsn)
    try:
        columns = {
            row["column_name"]: row
            for row in await conn.fetch(
                "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = $1", TABLE,
            )
        }
        constraints = {
            row["conname"]
            for row in await conn.fetch(
                "SELECT conname FROM pg_constraint WHERE conrelid = $1::regclass", TABLE,
            )
        }
    finally:
        await conn.close()
    return {name for name in columns if name in COLUMNS}, constraints & set(CONSTRAINTS)


async def _drop_infra_columns(dsn: str) -> None:
    """Put the table back into its pre-revision shape."""
    conn = await connect(dsn)
    try:
        for name in CONSTRAINTS:
            await conn.execute(f'ALTER TABLE "{TABLE}" DROP CONSTRAINT IF EXISTS "{name}"')
        for name in COLUMNS:
            await conn.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN IF EXISTS "{name}"')
    finally:
        await conn.close()


async def _insert(dsn: str, task_id: str) -> None:
    """One pre-revision source-CI row, in the zero state of its own defaults."""
    conn = await connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO projects (id, name, created_at) VALUES ('p', 'P', 1) "
            "ON CONFLICT DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO repos (id, project_id, url, checkout_base_path) "
            "VALUES ('r', 'p', 'https://example.test/r.git', '/tmp/r') ON CONFLICT DO NOTHING"
        )
        await conn.execute(
            f'INSERT INTO "{TABLE}" (task_id, repository_id, source_base, source_head, '
            "generation, policy_generation, state, evidence, observed_at) "
            "VALUES ($1, 'r', $2, $3, 1, 0, 'cancelled', '{}'::jsonb, 1)",
            task_id, "b" * 40, "a" * 40,
        )
    finally:
        await conn.close()


async def _row(dsn: str, task_id: str) -> tuple:
    conn = await connect(dsn)
    try:
        return await conn.fetchrow(
            f'SELECT infra_observations, infra_rerun_at, infra_reruns FROM "{TABLE}" '
            "WHERE task_id = $1",
            task_id,
        )
    finally:
        await conn.close()


async def test_revision_adds_the_infrastructure_counters_to_a_pre_revision_table():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("sourceciinfra")
    assert alembic(dsn, "upgrade", PRECEDING).returncode == 0
    await _drop_infra_columns(dsn)
    await _insert(dsn, "e1")
    assert (await _shape(dsn)) == (set(), set())

    upgraded = alembic(dsn, "upgrade", REVISION)
    assert upgraded.returncode == 0, upgraded.stderr
    columns, constraints = await _shape(dsn)
    assert columns == set(COLUMNS) and constraints == set(CONSTRAINTS)
    # A row that predates the revision reads as zero observations and no
    # re-run: nothing about its past is invented.
    assert await _row(dsn, "e1") == (0, None, 0)

    downgraded = alembic(dsn, "downgrade", PRECEDING)
    assert downgraded.returncode == 0, downgraded.stderr
    assert (await _shape(dsn)) == (set(), set())


async def test_revision_is_a_no_op_when_run_twice():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("sourceciinfratwice")
    assert alembic(dsn, "upgrade", PRECEDING).returncode == 0
    await _drop_infra_columns(dsn)
    assert alembic(dsn, "upgrade", REVISION).returncode == 0
    assert alembic(dsn, "stamp", PRECEDING).returncode == 0
    again = alembic(dsn, "upgrade", REVISION)
    assert again.returncode == 0, again.stderr
    assert await _shape(dsn) == (set(COLUMNS), set(CONSTRAINTS))
    assert alembic(dsn, "upgrade", "head").returncode == 0
