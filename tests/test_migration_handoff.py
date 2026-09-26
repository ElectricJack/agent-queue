"""Upgrade legacy handoffs in a disposable database, not the operator database."""

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine

from tests.pg_dsn import create_scratch_database

pytestmark = [pytest.mark.migration, pytest.mark.integration]


async def test_legacy_timestamp_backfill_replay_and_downgrade():
    from migrations.versions import compact_handoff_v1 as migration

    dsn = await create_scratch_database("handoffmigration")
    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("""
                CREATE TABLE task_context (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, type TEXT NOT NULL,
                    label TEXT, content TEXT NOT NULL
                )
            """)
            )
            await conn.execute(
                sa.text("""
                INSERT INTO task_context VALUES
                    ('a', 't', 'handoff', 'handoff', '{"ts": 20, "subject": "first"}'),
                    ('b', 't', 'handoff', 'handoff', '{"ts": 20, "subject": "second"}'),
                    ('c', 't', 'handoff', 'handoff', 'broken'),
                    ('d', 't', 'note', 'note', 'plain')
            """)
            )

            def upgrade(sync_conn):
                with Operations.context(MigrationContext.configure(sync_conn)):
                    migration.upgrade()
                    migration.upgrade()  # Fresh baseline and retry are safe.

            await conn.run_sync(upgrade)
            rows = (
                await conn.execute(
                    sa.text("SELECT id, created_at FROM task_context ORDER BY created_at, id")
                )
            ).all()
            assert rows == [("c", 0.0), ("d", 0.0), ("a", 20.0), ("b", 20.0)]
            await conn.execute(
                sa.text("""
                INSERT INTO task_context (id, task_id, type, content) VALUES ('new', 't', 'note', 'new')
            """)
            )
            assert (
                await conn.execute(sa.text("SELECT created_at FROM task_context WHERE id='new'"))
            ).scalar_one() > 20
            await conn.execute(
                sa.text("""
                INSERT INTO task_context (id, task_id, type, content, claim_epoch, idempotency_key)
                VALUES ('keyed', 't', 'handoff', '{}', 1, 'retry')
            """)
            )
            async with conn.begin_nested() as savepoint:
                with pytest.raises(sa.exc.IntegrityError):
                    await conn.execute(
                        sa.text("""
                        INSERT INTO task_context (id, task_id, type, content, claim_epoch, idempotency_key)
                        VALUES ('duplicate', 't', 'handoff', '{}', 1, 'retry')
                    """)
                    )
                await savepoint.rollback()

            def downgrade(sync_conn):
                with Operations.context(MigrationContext.configure(sync_conn)):
                    migration.downgrade()

            await conn.run_sync(downgrade)
            columns = await conn.run_sync(
                lambda c: {i["name"] for i in sa.inspect(c).get_columns("task_context")}
            )
            assert columns == {"id", "task_id", "type", "label", "content"}
            assert (
                await conn.execute(sa.text("SELECT content FROM task_context WHERE id='a'"))
            ).scalar_one()
    finally:
        await engine.dispose()
