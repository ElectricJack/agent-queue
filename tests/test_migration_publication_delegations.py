"""The delegation migration adds the owner-lease binding on old and current schemas."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

pytestmark = pytest.mark.migration


async def test_delegation_migration_is_idempotent(reuse_database):
    db = await reuse_database("publication-delegation-migration")
    path = Path("migrations/versions/a00000000109_publication_delegations.py")
    spec = importlib.util.spec_from_file_location("publication_delegation_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    def exercise(connection):
        # Only the fixture's disposable database: simulate a pre-delegation install.
        with Operations.context(MigrationContext.configure(connection)) as op:
            op.drop_column("integration_branch_owners", "delegation")
            revision.upgrade()
            revision.upgrade()
        inspector = sa.inspect(connection)
        columns = {c["name"]: c for c in inspector.get_columns("integration_branch_owners")}
        assert columns["delegation"]["nullable"]
        assert type(columns["delegation"]["type"]).__name__ == "JSONB"
        assert not inspector.has_table("integration_publication_delegations")

    async with db._engine.begin() as connection:
        await connection.run_sync(exercise)
