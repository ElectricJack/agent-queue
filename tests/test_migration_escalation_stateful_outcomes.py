"""The stateful-escalations columns revision upgrades and rolls back on PostgreSQL.

``a00000000062`` adds ``escalations.outcome`` and ``escalations.collapsed_at``
plus two named check constraints.  Both the adds and the constraints are
inspector-guarded, because the squashed baseline builds ``escalations`` from the
live ``src.database.tables.metadata`` — a database created after these columns
were declared already has them, so an unconditional ``add_column`` would fail on
every fresh database.  This test proves the guard both ways: from a database
that genuinely lacks the columns, and from one the baseline already built.
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
# The revision that adds the stateful-escalation columns, and whatever it
# currently chains onto — deriving the predecessor keeps the pair correct when a
# later revision is inserted ahead of it.
STATEFUL_REVISION = "a00000000062"
PRECEDING_REVISION = previous_revision(STATEFUL_REVISION)
NEW_COLUMNS = frozenset({"outcome", "collapsed_at"})
NEW_CONSTRAINTS = frozenset({"ck_escalations_outcome", "ck_escalations_collapsed"})


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


async def _columns(conn) -> set[str]:
    return {
        row["attname"]
        for row in await conn.fetch(
            "SELECT attname FROM pg_attribute "
            "WHERE attrelid = 'escalations'::regclass AND attnum > 0 AND NOT attisdropped"
        )
    }


async def _constraints(conn) -> set[str]:
    return {
        row["conname"]
        for row in await conn.fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'escalations'::regclass"
        )
    }


async def _drop_the_phase_columns(conn) -> None:
    for name in NEW_CONSTRAINTS:
        await conn.execute(f'ALTER TABLE "escalations" DROP CONSTRAINT IF EXISTS "{name}"')
    for name in NEW_COLUMNS:
        await conn.execute(f'ALTER TABLE "escalations" DROP COLUMN IF EXISTS "{name}"')


async def test_the_revision_adds_the_columns_and_the_named_constraints():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("escalationstatefulmigration")
    before = alembic(dsn, "upgrade", PRECEDING_REVISION)
    assert before.returncode == 0, before.stderr

    import asyncpg

    plain = dsn.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(plain)
    try:
        # Stand in for a database that predates the phase: the columns and the
        # constraints are genuinely absent.
        await _drop_the_phase_columns(conn)
        assert not NEW_COLUMNS.intersection(await _columns(conn))
    finally:
        await conn.close()

    upgraded = alembic(dsn, "upgrade", STATEFUL_REVISION)
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await asyncpg.connect(plain)
    try:
        assert NEW_COLUMNS.issubset(await _columns(conn))
        assert NEW_CONSTRAINTS.issubset(await _constraints(conn))
        # Both columns are nullable and unbacked, so every pre-phase row reads
        # NULL and keeps the create-only post behaviour.
        assert (
            await conn.fetchval("SELECT count(*) FROM escalations WHERE outcome IS NOT NULL")
        ) == 0
        assert (
            await conn.fetchval("SELECT count(*) FROM escalations WHERE collapsed_at IS NOT NULL")
        ) == 0
    finally:
        await conn.close()

    downgraded = alembic(dsn, "downgrade", PRECEDING_REVISION)
    assert downgraded.returncode == 0, downgraded.stderr
    conn = await asyncpg.connect(plain)
    try:
        assert not NEW_COLUMNS.intersection(await _columns(conn))
        assert not NEW_CONSTRAINTS.intersection(await _constraints(conn))
    finally:
        await conn.close()


async def test_the_guard_is_idempotent_against_a_baseline_built_schema():
    """A fresh database already has both columns; the revision must not fail.

    The squashed baseline creates ``escalations`` from the live metadata, so
    every new database reaches head with the columns present.  An unguarded
    ``add_column``/``create_check_constraint`` would make *every* fresh test
    database fail, which is why this arm exists and why the guard is the shape
    it is.
    """
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("escalationstatefulguard")
    upgraded = alembic(dsn, "upgrade", STATEFUL_REVISION)
    assert upgraded.returncode == 0, upgraded.stderr

    # Re-running the same DDL is what a re-stamp or a squashed baseline replays.
    again = alembic(dsn, "upgrade", STATEFUL_REVISION)
    assert again.returncode == 0, again.stderr

    import asyncpg

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        assert NEW_COLUMNS.issubset(await _columns(conn))
        assert NEW_CONSTRAINTS.issubset(await _constraints(conn))
        await conn.execute(
            "INSERT INTO projects (id, name, status, created_at) "
            "VALUES ('p-migration', 'p', 'ACTIVE', 1.0) ON CONFLICT DO NOTHING"
        )
        # And the constraints are enforced, not merely present.
        with pytest.raises(asyncpg.PostgresError, match="ck_escalations_outcome"):
            await conn.execute(
                "INSERT INTO escalations ("
                "id, project_id, source_kind, source_identity, incident_key, "
                "supervisor_owner, summary, investigation, decision_requested, "
                "severity, state, revision, outcome, created_at, updated_at"
                ") SELECT 'esc-bad-outcome', id, 'gate', 'g', 'k', 'o', 's', 'i', 'd', "
                "'low', 'resolved', 0, 'because-i-said-so', 1.0, 1.0 FROM projects LIMIT 1"
            )
        with pytest.raises(asyncpg.PostgresError, match="ck_escalations_collapsed"):
            # An open incident cannot carry a collapsed post.
            await conn.execute(
                "INSERT INTO escalations ("
                "id, project_id, source_kind, source_identity, incident_key, "
                "supervisor_owner, summary, investigation, decision_requested, "
                "severity, state, revision, collapsed_at, created_at, updated_at"
                ") SELECT 'esc-bad-collapse', id, 'gate', 'g2', 'k2', 'o', 's', 'i', 'd', "
                "'low', 'needs_human', 0, 1.0, 1.0, 1.0 FROM projects LIMIT 1"
            )
    finally:
        await conn.close()