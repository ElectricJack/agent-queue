"""Current PostgreSQL schema contracts retained after migration squashing."""

from importlib import import_module

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.database import Database
from src.database.engine import create_postgres_engine
from tests.db_fixtures import lease_dsn
from tests.pg_dsn import create_scratch_database

pytestmark = pytest.mark.migration


def _assert_schema(connection) -> None:
    schema = inspect(connection)
    assert "integration_candidate_ref_mutations" in schema.get_table_names()
    assert "target_branch" in {
        column["name"] for column in schema.get_columns("integration_candidate_resolutions")
    }
    assert {"repair_workspace_path", "target_kind", "handoff_owner_id", "handoff_fence_token"} <= {
        column["name"] for column in schema.get_columns("integration_candidate_resolutions")
    }
    assert {
        "fk_integration_candidate_ref_mutations_revision",
        "fk_integration_candidate_ref_mutations_resolution",
    } == {fk["name"] for fk in schema.get_foreign_keys("integration_candidate_ref_mutations")}
    guards = "\n".join(
        row[0]
        for row in connection.execute(
            text(
                "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname IN "
                "('integration_candidate_publication_is_monotone', "
                "'integration_candidate_resolution_is_monotone', "
                "'integration_candidate_mutation_is_monotone')"
            )
        )
    )
    assert "candidate PR identity is immutable" in guards or "OLD.state = 'pr_published'" in guards
    assert "target_branch" in guards
    assert "repair_workspace_path" in guards
    assert "target_kind" in guards
    assert "applied candidate mutation is immutable" in guards
    check = next(item for item in schema.get_check_constraints(
        "integration_candidate_ref_mutations"
    ) if item["name"] == "ck_integration_candidate_ref_mutations_remote")
    assert "purpose" not in check["sqltext"]


async def test_baseline_candidate_mutation_claims_schema():
    database = Database(lease_dsn("candidate_mutation_claims"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            await conn.run_sync(_assert_schema)
    finally:
        await database.close()


async def test_candidate_supersession_migration_replays_and_preserves_downgrade_evidence():
    migration = import_module("migrations.versions.a00000000078_candidate_mutation_reclaim")
    engine = create_postgres_engine(await create_scratch_database("mutation_reclaim"))

    def migrate(conn, direction):
        with Operations.context(MigrationContext.configure(conn)):
            getattr(migration, direction)()

    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                f"CREATE TABLE {migration.TABLE} (id text PRIMARY KEY, purpose text, "
                "state text, desired_sha text, remote_sha text, "
                f"CONSTRAINT {migration.CONSTRAINT} CHECK ({migration.PREVIOUS}))"
            ))
            await conn.execute(text(
                f"INSERT INTO {migration.TABLE} VALUES "
                "('retained', 'repair_handoff', 'reserved', 'desired', NULL)"
            ))
            await conn.run_sync(lambda sync: migrate(sync, "upgrade"))
            await conn.run_sync(lambda sync: migrate(sync, "upgrade"))
            await conn.execute(text(
                f"UPDATE {migration.TABLE} SET state='superseded' WHERE id='retained'"
            ))
        async with engine.begin() as conn:
            with pytest.raises(RuntimeError, match="superseded candidate mutation evidence"):
                await conn.run_sync(lambda sync: migrate(sync, "downgrade"))
            assert (await conn.execute(text(
                f"SELECT state FROM {migration.TABLE} WHERE id='retained'"
            ))).scalar_one() == "superseded"
        async with engine.begin() as conn:
            with pytest.raises(DBAPIError, match="superseded candidate mutation is immutable"):
                async with conn.begin_nested():
                    await conn.execute(text(
                        f"UPDATE {migration.TABLE} SET desired_sha='rewritten' WHERE id='retained'"
                    ))
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text(
                        f"INSERT INTO {migration.TABLE} VALUES "
                        "('invalid', 'repair_handoff', 'superseded', 'desired', 'other')"
                    ))
            # Drop the scratch evidence to exercise an admissible downgrade.
            await conn.execute(text(f"DELETE FROM {migration.TABLE}"))
            await conn.run_sync(lambda sync: migrate(sync, "downgrade"))
            await conn.run_sync(lambda sync: migrate(sync, "downgrade"))
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text(
                        f"INSERT INTO {migration.TABLE} VALUES "
                        "('refused', 'repair_handoff', 'superseded', 'desired', NULL)"
                    ))
            await conn.execute(text(
                f"INSERT INTO {migration.TABLE} VALUES "
                "('root', 'root_main', 'superseded', 'desired', NULL)"
            ))
    finally:
        await engine.dispose()
