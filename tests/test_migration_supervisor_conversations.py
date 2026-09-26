"""The supervisor-conversation revision upgrades idempotently on PostgreSQL."""

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
# The revision that creates the conversation tables, and whatever it currently
# chains onto -- deriving the predecessor keeps the pair correct when a later
# revision is inserted ahead of it.
CONVERSATIONS_REVISION = "a00000000027"
PRECEDING_REVISION = previous_revision(CONVERSATIONS_REVISION)
TABLES = (
    "supervisor_conversations",
    "conversation_inputs",
    "conversation_backfill_cursors",
    "conversation_intake_gaps",
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


async def _present(dsn: str) -> set[str]:
    import asyncpg

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        return {
            name
            for name in TABLES
            if await conn.fetchval("SELECT to_regclass($1)", f"public.{name}") is not None
        }
    finally:
        await conn.close()


async def _constraints(dsn: str) -> set[str]:
    import asyncpg

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        rows = await conn.fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid = ANY($1::regclass[])",
            [f"public.{name}" for name in TABLES],
        )
        indexes = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = ANY($1::text[])", list(TABLES)
        )
    finally:
        await conn.close()
    return {row["conname"] for row in rows} | {row["indexname"] for row in indexes}


async def _drop(dsn: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        for name in reversed(TABLES):
            await conn.execute(f'DROP TABLE IF EXISTS "{name}" CASCADE')
    finally:
        await conn.close()


async def test_revision_creates_the_four_tables_and_downgrade_removes_them():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("conversationmigration")
    before = alembic(dsn, "upgrade", PRECEDING_REVISION)
    assert before.returncode == 0, before.stderr
    # The squashed baseline builds from live metadata, so the predecessor
    # already has the tables; drop them to exercise the create path.
    await _drop(dsn)
    assert await _present(dsn) == set()

    upgraded = alembic(dsn, "upgrade", CONVERSATIONS_REVISION)
    assert upgraded.returncode == 0, upgraded.stderr
    assert await _present(dsn) == set(TABLES)
    assert {
        "uq_supervisor_conversations_root",
        "uq_supervisor_conversations_thread",
        "uq_supervisor_conversations_external_thread",
        "ck_supervisor_conversations_state",
        "uq_conversation_inputs_external",
        "ck_conversation_inputs_state",
        "ck_conversation_inputs_char_count",
        "ck_conversation_inputs_source",
        "ck_conversation_intake_gaps_reason",
        "idx_conversation_inputs_history",
        "idx_conversation_intake_gaps_channel",
    } <= await _constraints(dsn)

    downgraded = alembic(dsn, "downgrade", PRECEDING_REVISION)
    assert downgraded.returncode == 0, downgraded.stderr
    assert await _present(dsn) == set()


async def test_revision_is_a_no_op_when_the_tables_already_exist():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("conversationmigrationtwice")
    before = alembic(dsn, "upgrade", PRECEDING_REVISION)
    assert before.returncode == 0, before.stderr
    assert await _present(dsn) == set(TABLES)

    first = alembic(dsn, "upgrade", CONVERSATIONS_REVISION)
    assert first.returncode == 0, first.stderr
    # Re-run the revision itself over the tables it created.
    stamped = alembic(dsn, "stamp", PRECEDING_REVISION)
    assert stamped.returncode == 0, stamped.stderr
    second = alembic(dsn, "upgrade", CONVERSATIONS_REVISION)
    assert second.returncode == 0, second.stderr
    assert await _present(dsn) == set(TABLES)

    head = alembic(dsn, "upgrade", "head")
    assert head.returncode == 0, head.stderr
