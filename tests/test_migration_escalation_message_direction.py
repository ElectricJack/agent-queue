"""The ``direction=system`` widening upgrades, is enforced, and refuses to roll back.

``a00000000071`` replaces ``ck_escalation_messages_direction`` so a thread's
history can carry a daemon-authored audit note (spec §5.6's sweep trail) beside
the two conversational directions.  Three things have to hold, and each arm proves
one: from a database that genuinely carries the two-value constraint the revision
replaces it; against a fresh database the baseline already built with the
three-value one it must not churn; and ``downgrade`` must refuse while a system
row exists rather than delete the record of why an incident closed.
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
SYSTEM_DIRECTION_REVISION = "a00000000071"
PRECEDING_REVISION = previous_revision(SYSTEM_DIRECTION_REVISION)
CONSTRAINT = "ck_escalation_messages_direction"
NARROWED = "direction IN ('inbound','outbound')"


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


async def _constraint(conn) -> str | None:
    return await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'escalation_messages'::regclass AND conname = $1",
        CONSTRAINT,
    )


async def _narrow_to_two_values(conn) -> None:
    await conn.execute(f'ALTER TABLE "escalation_messages" DROP CONSTRAINT "{CONSTRAINT}"')
    await conn.execute(
        f'ALTER TABLE "escalation_messages" ADD CONSTRAINT "{CONSTRAINT}" CHECK ({NARROWED})'
    )


async def _seed_project(conn) -> None:
    await conn.execute(
        "INSERT INTO projects (id, name, status, created_at) "
        "VALUES ('p-sysdir', 'p', 'ACTIVE', 1.0) ON CONFLICT DO NOTHING"
    )


async def _seed_escalation(conn, escalation_id: str) -> None:
    await conn.execute(
        "INSERT INTO escalations ("
        "id, project_id, source_kind, source_identity, incident_key, "
        "supervisor_owner, summary, investigation, decision_requested, "
        "severity, state, revision, created_at, updated_at"
        ") SELECT $1, id, 'core', 'src', $2, 'supervisor-p', 's', 'i', 'd', "
        "'low', 'needs_human', 0, 1.0, 1.0 FROM projects WHERE id = 'p-sysdir'",
        escalation_id,
        f"key-{escalation_id}",
    )


async def test_the_revision_widens_the_direction_constraint_and_enforces_it():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("escalationsysdir")
    before = alembic(dsn, "upgrade", PRECEDING_REVISION)
    assert before.returncode == 0, before.stderr

    import asyncpg

    plain = dsn.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(plain)
    try:
        # Stand in for a database that predates the widening.
        await _narrow_to_two_values(conn)
        assert "outbound" in (await _constraint(conn))
    finally:
        await conn.close()

    upgraded = alembic(dsn, "upgrade", SYSTEM_DIRECTION_REVISION)
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await asyncpg.connect(plain)
    try:
        assert "system" in (await _constraint(conn))
        await _seed_project(conn)
        # The conversational directions still fit.
        await _seed_escalation(conn, "esc-inbound")
        await conn.execute(
            "INSERT INTO escalation_messages ("
            "id, escalation_id, direction, transport, verified_actor, text, "
            "received_at, created_at"
            ") VALUES ('em-in', 'esc-inbound', 'inbound', 'discord', 'human:jack', "
            "'keep the trial', 1.0, 1.0), "
            "('em-sys', 'esc-inbound', 'system', 'sweep', 'sweep', "
            "'sweep: task_terminal', 1.0, 1.0)"
        )
        # And the vocabulary is still closed.
        with pytest.raises(asyncpg.PostgresError, match=CONSTRAINT):
            await conn.execute(
                "INSERT INTO escalation_messages ("
                "id, escalation_id, direction, transport, verified_actor, text, "
                "received_at, created_at"
                ") VALUES ('em-bad', 'esc-inbound', 'sideways', 'discord', 'human:jack', "
                "'x', 1.0, 1.0)"
            )
    finally:
        await conn.close()

    # Narrowing again would reject the system row, so the downgrade refuses.
    refused = alembic(dsn, "downgrade", PRECEDING_REVISION)
    assert refused.returncode != 0
    assert "system audit note" in (refused.stdout + refused.stderr)

    conn = await asyncpg.connect(plain)
    try:
        await conn.execute("DELETE FROM escalation_messages WHERE id = 'em-sys'")
    finally:
        await conn.close()
    downgraded = alembic(dsn, "downgrade", PRECEDING_REVISION)
    assert downgraded.returncode == 0, downgraded.stderr
    conn = await asyncpg.connect(plain)
    try:
        assert "system" not in (await _constraint(conn))
    finally:
        await conn.close()


async def test_the_guard_is_idempotent_against_a_baseline_built_schema():
    """A fresh database already has the widened constraint; do not churn it.

    The squashed baseline creates ``escalation_messages`` from the live metadata,
    so every new database reaches head already carrying ``system``.  Dropping and
    recreating the constraint unconditionally would rewrite it on every fresh
    database for no reason.
    """
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("escalationsysdirguard")
    upgraded = alembic(dsn, "upgrade", SYSTEM_DIRECTION_REVISION)
    assert upgraded.returncode == 0, upgraded.stderr
    again = alembic(dsn, "upgrade", SYSTEM_DIRECTION_REVISION)
    assert again.returncode == 0, again.stderr

    import asyncpg

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        assert "system" in (await _constraint(conn))
    finally:
        await conn.close()
