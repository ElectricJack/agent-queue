"""Focused migration-chain versus table-metadata regression tests."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from src.database import Database
from src.database.tables import metadata


async def test_agent_profiles_overlay_config_has_no_autogenerate_drift(tmp_path):
    """The overlay column exists in both the migrated schema and metadata."""
    database = Database(str(tmp_path / "overlay-metadata.db"))
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
