"""Conditional selection-table upgrade and downgrade on disposable PostgreSQL."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.database.engine import create_postgres_engine
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database

pytestmark = pytest.mark.migration

ROOT = Path(__file__).resolve().parent.parent
REVISION = "a00000000028"
PRECEDING_REVISION = previous_revision(REVISION)
TABLES = ("test_selections", "test_selection_observations", "test_selection_promotions")


async def migrate(engine, direction, revision):
    def run(conn):
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = conn
        getattr(command, direction)(config, revision)

    async with engine.begin() as conn:
        await conn.run_sync(run)


def inspect_selection_schema(conn):
    inspector = sa.inspect(conn)
    checks = {
        name: {check["name"] for check in inspector.get_check_constraints(name)} for name in TABLES
    }
    assert checks["test_selections"] == {
        "ck_test_selections_mode",
        "ck_test_selections_jev_status",
        "ck_test_selections_marker_policy",
    }
    assert checks["test_selection_observations"] == {"ck_test_selection_observations_kind"}
    indexes = {index["name"]: index for index in inspector.get_indexes("test_selection_promotions")}
    active = indexes["uq_test_selection_promotions_active"]
    assert active["unique"]
    assert active["column_names"] == ["project_id"]
    assert "revoked_at IS NULL" in active["dialect_options"]["postgresql_where"]
    observation_fk = inspector.get_foreign_keys("test_selection_observations")[0]
    assert observation_fk["referred_table"] == "test_selections"
    assert observation_fk["options"]["ondelete"] == "CASCADE"
    return {
        name: conn.execute(
            sa.text("SELECT oid FROM pg_class WHERE relname = :name"), {"name": name}
        ).scalar_one()
        for name in TABLES
    }


@pytest.mark.parametrize("drop_tables", [TABLES, (), ("test_selection_observations",)])
async def test_upgrade_creates_missing_tables_is_idempotent_and_downgrades(drop_tables):
    dsn = await create_scratch_database("testselectionmigration")
    engine = create_postgres_engine(dsn)
    try:
        await migrate(engine, "upgrade", PRECEDING_REVISION)
        # The squashed baseline uses live metadata, so exercise both an old
        # database missing the new relations and a fresh/partially present one.
        async with engine.begin() as conn:
            for name in reversed(drop_tables):
                await conn.execute(sa.text(f'DROP TABLE "{name}"'))

        await migrate(engine, "upgrade", REVISION)
        async with engine.begin() as conn:
            oids = await conn.run_sync(inspect_selection_schema)

        await migrate(engine, "upgrade", REVISION)
        # Calling the revision body again proves its conditional creates too;
        # Alembic's second upgrade alone would skip an already-stamped body.
        revision = import_module("migrations.versions.a00000000028_test_selections")

        def run_upgrade_twice(conn):
            with Operations.context(MigrationContext.configure(conn)):
                revision.upgrade()
                revision.upgrade()
            assert inspect_selection_schema(conn) == oids

        async with engine.begin() as conn:
            await conn.run_sync(run_upgrade_twice)

        await migrate(engine, "downgrade", PRECEDING_REVISION)

        def assert_dropped_and_repeat_downgrade(conn):
            assert not set(TABLES) & set(sa.inspect(conn).get_table_names())
            with Operations.context(MigrationContext.configure(conn)):
                revision.downgrade()

        async with engine.begin() as conn:
            await conn.run_sync(assert_dropped_and_repeat_downgrade)
    finally:
        await engine.dispose()
