"""The delegation migration creates durable authority on old and current schemas."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.migration


async def test_delegation_migration_is_idempotent(reuse_database):
    db = await reuse_database("publication-delegation-migration")
    path = Path("migrations/versions/a00000000109_publication_delegations.py")
    spec = importlib.util.spec_from_file_location("publication_delegation_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)

    def exercise(connection):
        # Only the fixture's disposable database: simulate a pre-delegation install.
        sa.Table("integration_publication_delegations", sa.MetaData(),
                 autoload_with=connection).drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            revision.upgrade()
        inspector = sa.inspect(connection)
        assert {c["name"] for c in inspector.get_columns("integration_publication_delegations")} >= {
            "session_instance_token", "claim_epoch", "expected_target_oid", "owner_fence",
            "published_oid", "revoked_by",
        }
        assert {i["name"] for i in inspector.get_indexes("integration_publication_delegations")} == {
            "idx_integration_publication_delegations_task",
        }

    async with db._engine.begin() as connection:
        await connection.run_sync(exercise)
