"""The conversation-kind revision upgrades idempotently on PostgreSQL.

Chat-extension spec §2.2 needs "one durable conversation row per channel",
which is a discriminator plus a partial unique index. The squashed baseline is
built from live metadata, so a fresh database already has both; these tests drop
them first to exercise the real additive path over a pre-P2 table.
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
REVISION = "a00000000069"
PRECEDING = previous_revision(REVISION)
TABLE = "supervisor_conversations"


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


async def _column(dsn: str) -> str | None:
    conn = await connect(dsn)
    try:
        return await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = 'kind'",
            TABLE,
        )
    finally:
        await conn.close()


async def _objects(dsn: str) -> set[str]:
    conn = await connect(dsn)
    try:
        constraints = await conn.fetch(
            "SELECT conname FROM pg_constraint WHERE conrelid = $1::regclass", TABLE
        )
        indexes = await conn.fetch("SELECT indexname FROM pg_indexes WHERE tablename = $1", TABLE)
    finally:
        await conn.close()
    return {row["conname"] for row in constraints} | {row["indexname"] for row in indexes}


async def _insert(
    dsn: str, conversation_id: str, *, state: str = "opening", root: str = "1"
) -> None:
    conn = await connect(dsn)
    try:
        await conn.execute(
            f'INSERT INTO "{TABLE}" (id, transport, guild_id, channel_id, '
            "external_root_message_id, thread_id, created_by, audience, state, "
            "created_at, updated_at) "
            "VALUES ($1, 'discord', '1', 'channel-1', $2, $3, 'human:discord:9', "
            "'[]'::json, $4, 1000, 1000)",
            conversation_id,
            root,
            f"conversation:{conversation_id}",
            state,
        )
    finally:
        await conn.close()


async def _kinds(dsn: str) -> list[tuple[str, str, str]]:
    conn = await connect(dsn)
    try:
        return [
            (row["id"], row["kind"], row["state"])
            for row in await conn.fetch(f'SELECT id, kind, state FROM "{TABLE}" ORDER BY id')
        ]
    finally:
        await conn.close()


async def _drop_p2_objects(dsn: str) -> None:
    """Put the table back into its pre-P2 shape."""
    conn = await connect(dsn)
    try:
        await conn.execute('DROP INDEX IF EXISTS "uq_supervisor_conversations_channel"')
        await conn.execute(
            f'ALTER TABLE "{TABLE}" DROP CONSTRAINT IF EXISTS "ck_supervisor_conversations_kind"'
        )
        await conn.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN IF EXISTS "kind"')
    finally:
        await conn.close()


async def test_revision_adds_the_kind_column_and_leaves_existing_rows_thread_kind():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("convkind")
    assert alembic(dsn, "upgrade", PRECEDING).returncode == 0
    await _drop_p2_objects(dsn)
    await _insert(dsn, "conv-legacy")
    assert await _column(dsn) is None

    upgraded = alembic(dsn, "upgrade", REVISION)
    assert upgraded.returncode == 0, upgraded.stderr
    assert await _column(dsn) == "text"
    # An existing conversation is a thread conversation: nothing about the
    # mention-routing model changes under it.
    assert await _kinds(dsn) == [("conv-legacy", "thread", "opening")]
    assert {
        "ck_supervisor_conversations_kind",
        "uq_supervisor_conversations_channel",
    } <= await _objects(dsn)

    downgraded = alembic(dsn, "downgrade", PRECEDING)
    assert downgraded.returncode == 0, downgraded.stderr
    assert await _column(dsn) is None
    assert "ck_supervisor_conversations_kind" not in await _objects(dsn)


async def test_revision_is_a_no_op_when_run_twice():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("convkindtwice")
    assert alembic(dsn, "upgrade", PRECEDING).returncode == 0
    await _drop_p2_objects(dsn)
    assert alembic(dsn, "upgrade", REVISION).returncode == 0
    assert alembic(dsn, "stamp", PRECEDING).returncode == 0
    again = alembic(dsn, "upgrade", REVISION)
    assert again.returncode == 0, again.stderr
    assert {
        "ck_supervisor_conversations_kind",
        "uq_supervisor_conversations_channel",
    } <= await _objects(dsn)
    assert alembic(dsn, "upgrade", "head").returncode == 0


async def test_one_live_channel_conversation_and_a_closed_one_outside_the_index():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("convkindindex")
    assert alembic(dsn, "upgrade", REVISION).returncode == 0
    await _insert(dsn, "conv-channel", root="10")
    conn = await connect(dsn)
    try:
        await conn.execute(f"UPDATE \"{TABLE}\" SET kind = 'channel' WHERE id = 'conv-channel'")
        with pytest.raises(Exception) as rejected:
            await conn.execute(
                f'INSERT INTO "{TABLE}" (id, transport, guild_id, channel_id, '
                "external_root_message_id, thread_id, created_by, audience, state, kind, "
                "created_at, updated_at) VALUES ('conv-second', 'discord', '1', 'channel-1', "
                "'11', 'conversation:conv-second', 'human:discord:9', '[]'::json, 'open', "
                "'channel', 1000, 1000)"
            )
        assert "uq_supervisor_conversations_channel" in str(rejected.value)
        # A thread conversation in the same channel is unaffected.
        await conn.execute(
            f'INSERT INTO "{TABLE}" (id, transport, guild_id, channel_id, '
            "external_root_message_id, thread_id, created_by, audience, state, kind, "
            "created_at, updated_at) VALUES ('conv-thread', 'discord', '1', 'channel-1', "
            "'12', 'conversation:conv-thread', 'human:discord:9', '[]'::json, 'open', "
            "'thread', 1000, 1000)"
        )
        # Closing the channel conversation frees the channel for a new one.
        await conn.execute(f"UPDATE \"{TABLE}\" SET state = 'closed' WHERE id = 'conv-channel'")
        await conn.execute(
            f'INSERT INTO "{TABLE}" (id, transport, guild_id, channel_id, '
            "external_root_message_id, thread_id, created_by, audience, state, kind, "
            "created_at, updated_at) VALUES ('conv-third', 'discord', '1', 'channel-1', "
            "'13', 'conversation:conv-third', 'human:discord:9', '[]'::json, 'opening', "
            "'channel', 1000, 1000)"
        )
    finally:
        await conn.close()
    assert await _kinds(dsn) == [
        ("conv-channel", "channel", "closed"),
        ("conv-third", "channel", "opening"),
        ("conv-thread", "thread", "open"),
    ]


async def test_an_unknown_kind_is_refused_by_the_check_constraint():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN is not set")
    dsn = await create_scratch_database("convkindcheck")
    assert alembic(dsn, "upgrade", REVISION).returncode == 0
    conn = await connect(dsn)
    try:
        with pytest.raises(Exception) as rejected:
            await conn.execute(
                f'INSERT INTO "{TABLE}" (id, transport, guild_id, channel_id, '
                "external_root_message_id, thread_id, created_by, audience, state, kind, "
                "created_at, updated_at) VALUES ('conv-bad', 'discord', '1', 'channel-1', "
                "'14', 'conversation:conv-bad', 'human:discord:9', '[]'::json, 'open', "
                "'dm', 1000, 1000)"
            )
        assert "ck_supervisor_conversations_kind" in str(rejected.value)
    finally:
        await conn.close()
