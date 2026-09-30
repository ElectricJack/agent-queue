"""PostgreSQL integration controls retain their append-only audit contract."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration

CONTROL_TABLES = {
    "integration_history_waivers",
    "integration_rollout_transitions",
    "integration_history_waiver_consumptions",
    "integration_legacy_gate_applicability",
    "integration_legacy_suppression",
}
IMMUTABLE_TABLES = CONTROL_TABLES - {"integration_legacy_suppression"}


def _exercise_live_guard_and_immutability(connection) -> None:
    connection.execute(
        text("INSERT INTO projects (id, name, created_at) VALUES ('controlled', 'controlled', 1)")
    )
    connection.execute(
        text(
            "INSERT INTO integration_history_waivers "
            "(id, project_id, operator_id, reason, blocker_digest, created_at) "
            "VALUES ('waiver', 'controlled', 'operator:local', 'history migration', "
            ":digest, 2)"
        ),
        {"digest": "sha256:" + "a" * 64},
    )
    connection.execute(
        text(
            "UPDATE projects SET hierarchical_integration_desired_mode = 'observe', "
            "hierarchical_integration_generation = 1 WHERE id = 'controlled'"
        )
    )
    connection.execute(
        text(
            "INSERT INTO integration_rollout_transitions "
            "(id, project_id, generation, old_effective_mode, new_effective_mode, "
            "old_desired_mode, new_desired_mode, draining, operator_id, reason, "
            "blocker_digest, old_legacy_policy, new_legacy_policy, waiver_id, created_at) "
            "VALUES ('transition', 'controlled', 1, 'disabled', 'disabled', 'disabled', "
            "'observe', false, 'operator:local', 'observe', :digest, '{}', '{}', "
            "'waiver', 3)"
        ),
        {"digest": "sha256:" + "a" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO integration_history_waiver_consumptions "
            "(waiver_id, transition_id, project_id, blocker_digest, consumed_by, consumed_at) "
            "VALUES ('waiver', 'transition', 'controlled', :digest, 'operator:local', 3)"
        ),
        {"digest": "sha256:" + "a" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO gates (id, project_id, gate_type, title, status, created_at) "
            "VALUES ('legacy-gate', 'controlled', 'pr-merged', 'legacy', 'open', 1)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO integration_legacy_gate_applicability "
            "(project_id, gate_id, waiver_id, transition_id, blocker_digest, applicable, "
            "created_at) VALUES ('controlled', 'legacy-gate', 'waiver', 'transition', "
            ":digest, false, 3)"
        ),
        {"digest": "sha256:" + "a" * 64},
    )
    timestamp_columns = {
        "integration_history_waivers": "created_at",
        "integration_rollout_transitions": "created_at",
        "integration_history_waiver_consumptions": "consumed_at",
        "integration_legacy_gate_applicability": "created_at",
    }
    for table in IMMUTABLE_TABLES:
        with pytest.raises(SQLAlchemyError), connection.begin_nested():
            connection.execute(text(f"UPDATE {table} SET {timestamp_columns[table]} = 9"))
        with pytest.raises(SQLAlchemyError), connection.begin_nested():
            connection.execute(text(f"DELETE FROM {table}"))


async def test_baseline_controls_are_append_only():
    database = Database(lease_dsn("integration-controls"))
    await database.initialize()
    try:
        async with database._engine.begin() as conn:
            await conn.run_sync(_exercise_live_guard_and_immutability)
    finally:
        await database.close()


async def test_repair_ejection_upgrade_backfills_existing_candidates_and_is_idempotent(
    reuse_database,
):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from importlib import import_module
    from sqlalchemy import select
    from src.database.tables import integration_candidate_revisions
    from src.integration.controls import IntegrationControlService
    from src.integration.scheduler import TrainService
    from src.models import Project, RepoConfig, RepoSourceType
    from tests.test_integration_sealing import _enable_train, _request, _seed_leaf
    from tests.test_integration_eject import _start_repair

    db = await reuse_database("repair-ejection-upgrade")
    await db.create_project(Project(id="p", name="P"))
    await db.create_repo(RepoConfig(id="repo", project_id="p", source_type=RepoSourceType.LINK))
    await _enable_train(db)
    await _seed_leaf(db, "first", "1" * 40)
    await _seed_leaf(db, "second", "2" * 40)
    request = await _request(db)
    batch = await TrainService(db).seal("p", request["request_id"], 20.0)
    await _start_repair(db, batch)

    def upgrade_existing(connection):
        # Recreate revision 43's non-empty candidate membership shape. This
        # is the disposable database leased for this test, never the operator DB.
        connection.execute(
            text("ALTER TABLE integration_candidate_revisions DROP COLUMN source_manifest")
        )
        connection.execute(
            text(
                "ALTER TABLE integration_candidate_member_results ADD CONSTRAINT "
                "fk_integration_candidate_member_results_member FOREIGN KEY "
                "(batch_id, member_ordinal) REFERENCES integration_batch_members "
                "(batch_id, ordinal)"
            )
        )
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            module = import_module("migrations.versions.a00000000044_repair_ejection")
            module.upgrade()
            module.upgrade()

    async with db._engine.begin() as conn:
        await conn.run_sync(upgrade_existing)
        snapshot = (
            await conn.execute(select(integration_candidate_revisions.c.source_manifest))
        ).scalar_one()
        assert [member["task_id"] for member in snapshot] == ["first", "second"]
    result = await IntegrationControlService(db, clock=lambda: 30.0).eject(
        batch["batch_id"],
        task_id="first",
        reason="migration collision",
        operator_id="supervisor",
    )
    assert result["outcome"] == "ejected"
