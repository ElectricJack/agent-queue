"""Focused migration-chain versus table-metadata regression tests."""

import importlib

import pytest
import sqlalchemy as sa

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.database import Database
from src.database.tables import metadata
from tests.db_fixtures import lease_dsn


async def test_agent_profiles_overlay_config_has_no_autogenerate_drift(tmp_path):
    """The overlay column exists in both the migrated schema and metadata."""
    database = Database(lease_dsn("overlay-metadata.db"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            diffs = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn, opts={"compare_type": True}),
                    metadata,
                )
            )
    finally:
        await database.close()

    overlay_diffs = [
        diff
        for diff in diffs
        if len(diff) >= 4
        and diff[2] == "agent_profiles"
        and getattr(diff[3], "name", None) == "overlay_config"
    ]
    assert overlay_diffs == []


@pytest.mark.migration
async def test_latest_attempt_index_upgrade_preserves_existing_index_and_reruns():
    """Both old installs and the live-metadata baseline get the ordered lookup."""
    revision = importlib.import_module("migrations.versions.a00000000095_latest_task_attempt_index")
    database = Database(lease_dsn("latest-attempt-index.db"))
    await database.initialize()
    try:
        async with database._engine.begin() as conn:
            def verify(bind):
                with Operations.context(MigrationContext.configure(bind)):
                    # The baseline creates current metadata, so reproduce the
                    # old deployed schema before exercising the upgrade.
                    revision.downgrade()
                    revision.downgrade()
                    before = sa.inspect(bind).get_indexes("task_session_attempts")
                    assert "idx_task_session_attempts_latest" not in {
                        index["name"] for index in before
                    }
                    revision.upgrade()
                    revision.upgrade()
                    after = sa.inspect(bind).get_indexes("task_session_attempts")
                    assert all(index in after for index in before)
                    latest = next(
                        index for index in after
                        if index["name"] == "idx_task_session_attempts_latest"
                    )
                    assert latest["column_names"] == ["task_id", "started_at", "id"]
                    assert not compare_metadata(MigrationContext.configure(bind), metadata)

            await conn.run_sync(verify)
    finally:
        await database.close()
