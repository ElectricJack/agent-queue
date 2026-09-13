"""The retired-class revision repoints live pins and spares the evidence rows."""

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
RETIREMENT_REVISION = "a0000000000f"
PRECEDING_REVISION = previous_revision(RETIREMENT_REVISION)


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


async def test_live_pins_are_repointed_and_attempt_history_is_left_alone():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("retiredclassmigration")
    before = alembic(dsn, "upgrade", PRECEDING_REVISION)
    assert before.returncode == 0, before.stderr

    import asyncpg

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute(
            """
            INSERT INTO projects (id, name, created_at)
            VALUES ('p', 'Project', 1.0)
            """
        )
        for task_id, class_id in (("t-retired", "standard-medium"), ("t-current", "deep-high")):
            await conn.execute(
                """
                INSERT INTO tasks (id, project_id, title, description, status,
                                   intelligence_class, created_at, updated_at)
                VALUES ($1, 'p', 'T', 'D', 'READY', $2, 1.0, 1.0)
                """,
                task_id, class_id,
            )
        # Evidence of what actually ran: never rewritten.
        await conn.execute(
            """
            INSERT INTO task_session_attempts (
                id, session_id, task_id, profile_id, name, lifecycle,
                intelligence_class, harness, provider, state, work_dir,
                started_at, session_started_at)
            VALUES ('a1', 's1', 't-retired', 'worker-claude', 'n1', 'task',
                    'standard-medium', 'claude', 'anthropic', 'running', '/wd',
                    1.0, 1.0)
            """
        )
    finally:
        await conn.close()

    upgraded = alembic(dsn, "upgrade", RETIREMENT_REVISION)
    assert upgraded.returncode == 0, upgraded.stderr

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        pins = {
            row["id"]: row["intelligence_class"]
            for row in await conn.fetch("SELECT id, intelligence_class FROM tasks")
        }
        assert pins["t-retired"] == "standard-high"
        assert pins["t-current"] == "deep-high"
        assert await conn.fetchval(
            "SELECT intelligence_class FROM task_session_attempts WHERE id = 'a1'"
        ) == "standard-medium"
    finally:
        await conn.close()

    # Idempotent: a second application changes nothing and does not fail.
    again = alembic(dsn, "stamp", PRECEDING_REVISION)
    assert again.returncode == 0, again.stderr
    replay = alembic(dsn, "upgrade", RETIREMENT_REVISION)
    assert replay.returncode == 0, replay.stderr
