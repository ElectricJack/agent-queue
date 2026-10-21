"""The delegation migration adds the owner-lease binding on old and current schemas."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

pytestmark = pytest.mark.migration

TABLE = "integration_branch_owners"


def _revision():
    path = Path("migrations/versions/a00000000109_publication_delegations.py")
    spec = importlib.util.spec_from_file_location("publication_delegation_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


def _delegation(connection):
    columns = {c["name"]: c for c in sa.inspect(connection).get_columns(TABLE)}
    return columns.get("delegation")


async def test_delegation_migration_is_idempotent(reuse_database):
    db = await reuse_database("publication-delegation-migration")
    revision = _revision()

    def exercise(connection):
        # Only the fixture's disposable database: simulate a pre-delegation install.
        with Operations.context(MigrationContext.configure(connection)) as op:
            # Already carries the column: a no-op.
            revision.upgrade()
            assert _delegation(connection) is not None
            # Existing table without the column: added once, then a no-op.
            op.drop_column(TABLE, "delegation")
            revision.upgrade()
            revision.upgrade()
        delegation = _delegation(connection)
        assert delegation["nullable"]
        assert type(delegation["type"]).__name__ == "JSONB"
        assert not sa.inspect(connection).has_table("integration_publication_delegations")

    async with db._engine.begin() as connection:
        await connection.run_sync(exercise)


async def test_delegation_migration_skips_an_absent_owner_table(reuse_database):
    # A sparse restore (tests/test_db_cli.py restores revision 84 without the owner
    # table) must upgrade through this revision rather than fail reflection.
    db = await reuse_database("publication-delegation-migration-absent")
    revision = _revision()

    def exercise(connection):
        savepoint = connection.begin_nested()
        connection.execute(sa.text(f"DROP TABLE {TABLE} CASCADE"))
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
        assert not sa.inspect(connection).has_table(TABLE)
        savepoint.rollback()
        assert _delegation(connection) is not None

    async with db._engine.begin() as connection:
        await connection.run_sync(exercise)
