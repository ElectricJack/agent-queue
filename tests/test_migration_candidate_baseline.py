"""Revision a00000000080: a red candidate's target baseline counters, additive.

The squashed baseline is built from live metadata, so a fresh database already
has these columns; the first test drops them to exercise the real additive path
over a pre-revision table, and the second proves the revision body is replayable
on a database that already carries them.
"""

from __future__ import annotations

import os
import subprocess
import sys

import asyncpg
import pytest

from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.migration, pytest.mark.integration]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTGRES_DSN = ensure_worker_postgres_dsn()
REVISION = "a00000000080"
PRECEDING = previous_revision(REVISION)
TABLE = "integration_batches"
COLUMNS = (
    "baseline_candidate_sha",
    "baseline_generation",
    "baseline_observations",
    "baseline_reruns",
    "baseline_rerun_at",
)
CONSTRAINTS = (
    "ck_integration_batches_baseline_generation",
    "ck_integration_batches_baseline_observations",
    "ck_integration_batches_baseline_reruns",
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


async def _shape(dsn: str) -> tuple[set[str], set[str], set[str]]:
    """The columns and check constraints this revision owns, as PostgreSQL sees them."""
    conn = await connect(dsn)
    try:
        columns = {
            row["column_name"]
            for row in await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
                TABLE,
            )
        }
        constraints = {
            row["conname"]
            for row in await conn.fetch(
                "SELECT conname FROM pg_constraint WHERE conrelid = $1::regclass", TABLE
            )
        }
        nullability = {
            row["column_name"]
            for row in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = $1 AND is_nullable = 'NO'",
                TABLE,
            )
        }
    finally:
        await conn.close()
    return columns & set(COLUMNS), constraints & set(CONSTRAINTS), nullability & set(COLUMNS)


async def _drop_baseline_columns(dsn: str) -> None:
    """Put the table back into its pre-revision shape."""
    conn = await connect(dsn)
    try:
        for name in CONSTRAINTS:
            await conn.execute(f'ALTER TABLE "{TABLE}" DROP CONSTRAINT IF EXISTS "{name}"')
        for name in COLUMNS:
            await conn.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN IF EXISTS "{name}"')
    finally:
        await conn.close()


async def _insert(dsn: str, batch_id: str) -> None:
    """One pre-revision batch row, in the zero state of its own defaults."""
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
            f'INSERT INTO "{TABLE}" (id, project_id, repository_id, target_ref, intent, '
            "request_id, source_manifest_digest, base_sha, integration_branch, lifecycle, "
            "policy_snapshot, artifact_snapshot, cleanup_state, created_at, updated_at) "
            "VALUES ($1, 'p', 'r', 'refs/heads/main', 'open', $1, 'm', $2, "
            "'refs/heads/aq/batches/b', 'sealed', '{}'::json, '{}'::json, 'pending', 1, 1)",
            batch_id, "a" * 40,
        )
    finally:
        await conn.close()


async def _row(dsn: str, batch_id: str) -> tuple:
    conn = await connect(dsn)
    try:
        return await conn.fetchrow(
            f'SELECT baseline_candidate_sha, baseline_generation, baseline_observations, '
            f'baseline_reruns, baseline_rerun_at FROM "{TABLE}" WHERE id = $1',
            batch_id,
        )
    finally:
        await conn.close()


async def test_revision_adds_the_baseline_counters_to_a_pre_revision_table():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("candidatebaseline")
    assert alembic(dsn, "upgrade", PRECEDING).returncode == 0
    await _drop_baseline_columns(dsn)
    await _insert(dsn, "batch-old")
    assert (await _shape(dsn)) == (set(), set(), set())

    upgraded = alembic(dsn, "upgrade", REVISION)
    assert upgraded.returncode == 0, upgraded.stderr
    columns, constraints, not_null = await _shape(dsn)
    assert columns == set(COLUMNS) and constraints == set(CONSTRAINTS)
    # The counters are NOT NULL: a legacy NULL would read as None in the
    # comparison path rather than as the zero state.
    assert not_null == {"baseline_generation", "baseline_observations", "baseline_reruns"}
    # A row that predates the revision reads as no candidate and zero
    # observations: nothing about its past is invented.
    assert tuple(await _row(dsn, "batch-old")) == (None, 0, 0, 0, None)

    conn = await connect(dsn)
    try:
        # A negative counter is refused by the named check constraint.
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                f'UPDATE "{TABLE}" SET baseline_observations = -1 WHERE id = $1', "batch-old"
            )
    finally:
        await conn.close()

    downgraded = alembic(dsn, "downgrade", PRECEDING)
    assert downgraded.returncode == 0, downgraded.stderr
    assert (await _shape(dsn)) == (set(), set(), set())


async def test_revision_is_a_no_op_when_run_twice():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("candidatebaselinetwice")
    assert alembic(dsn, "upgrade", PRECEDING).returncode == 0
    await _drop_baseline_columns(dsn)
    assert alembic(dsn, "upgrade", REVISION).returncode == 0
    assert alembic(dsn, "stamp", PRECEDING).returncode == 0
    again = alembic(dsn, "upgrade", REVISION)
    assert again.returncode == 0, again.stderr
    columns, constraints, not_null = await _shape(dsn)
    assert columns == set(COLUMNS) and constraints == set(CONSTRAINTS)
    assert not_null == {"baseline_generation", "baseline_observations", "baseline_reruns"}
    assert alembic(dsn, "upgrade", "head").returncode == 0