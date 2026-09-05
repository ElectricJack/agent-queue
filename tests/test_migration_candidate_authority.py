"""Dialect round trips for candidate publication authority revision 69416e65ee21."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "9b3e5a7c1d20"
REVISION = "69416e65ee21"
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def _assert_schema(connection) -> None:
    schema = inspect(connection)
    assert {
        "integration_candidate_publications",
        "integration_candidate_resolutions",
    } <= set(schema.get_table_names())
    publication_fks = {
        fk["name"] for fk in schema.get_foreign_keys("integration_candidate_publications")
    }
    resolution_fks = {
        fk["name"] for fk in schema.get_foreign_keys("integration_candidate_resolutions")
    }
    assert publication_fks == {"fk_integration_candidate_publications_revision"}
    assert {
        "fk_integration_candidate_resolutions_member",
        "fk_integration_candidate_resolutions_stage",
        "fk_integration_candidate_resolutions_task",
        "fk_integration_candidate_resolutions_session",
        "fk_integration_candidate_resolutions_workspace",
    } <= resolution_fks


async def test_sqlite_candidate_authority_upgrade_downgrade_upgrade(tmp_path):
    path = tmp_path / "candidate-authority.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            _assert_schema(conn)
            triggers = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='trigger' "
                        "AND name LIKE 'trg_candidate_%'"
                    )
                )
            }
            assert len(triggers) == 5
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
            _migrate(conn, REVISION)
        with engine.connect() as conn:
            _assert_schema(conn)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_candidate_authority_upgrade_downgrade_upgrade():
    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("task9b1_candidate_authority")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:

        async def migrate(revision: str, *, downgrade: bool = False) -> None:
            async with engine.connect() as conn:
                await conn.run_sync(lambda sync: _migrate(sync, revision, downgrade=downgrade))
                await conn.commit()

        await migrate(PRIOR, downgrade=True)
        await migrate(REVISION)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_schema)
        await migrate(PRIOR, downgrade=True)
        await migrate(REVISION)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_schema)
    finally:
        await engine.dispose()
