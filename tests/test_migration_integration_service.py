"""Integration service schema: catch-up state and the retired delivery journal."""

import json

import pytest
from sqlalchemy import inspect, text

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


def _columns(connection) -> set[str]:
    return {
        column["name"]
        for column in inspect(connection).get_columns("project_integration_schedules")
    }


def _seed_schedule(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO project_integration_schedules "
            "(project_id, enabled, interval_seconds, next_due_at, request_sequence, "
            "outstanding_request_id, outstanding_trigger, outstanding_requested_at, updated_at) "
            "VALUES ('project-z', TRUE, 30, 30, 4, 'request-4', 'manual', 10, 10)"
        )
    )


def _assert_catchup_contract(connection) -> None:
    _seed_schedule(connection)
    catchup_columns = {
        "catchup_trigger",
        "catchup_requested_at",
        "catchup_after_sequence",
    }
    assert catchup_columns <= _columns(connection)
    assert "ck_project_integration_schedules_catchup" in {
        constraint["name"]
        for constraint in inspect(connection).get_check_constraints("project_integration_schedules")
    }
    row = connection.execute(
        text(
            "SELECT catchup_trigger, catchup_requested_at, catchup_after_sequence "
            "FROM project_integration_schedules WHERE project_id = 'project-z'"
        )
    ).one()
    assert row == (None, None, None)
    connection.execute(
        text(
            "UPDATE project_integration_schedules SET catchup_trigger = 'periodic', "
            "catchup_requested_at = 20, catchup_after_sequence = 4 "
            "WHERE project_id = 'project-z'"
        )
    )


async def test_baseline_catchup_contract():
    database = Database(lease_dsn("catchup"))
    await database.initialize()
    try:
        async with database._engine.begin() as conn:
            await conn.run_sync(_assert_catchup_contract)
    finally:
        await database.close()


async def test_baseline_has_no_development_delivery_journal():
    """Git answers development delivery; the retired receipt table never returns."""
    database = Database(lease_dsn("retired-journal"))
    await database.initialize()
    try:
        async with database._engine.connect() as conn:
            tables = await conn.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        assert "development_deliveries" not in tables
    finally:
        await database.close()


async def test_batch_ejection_record_migration_is_additive_and_ignores_legacy_metadata():
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.dialects.postgresql import JSONB

    from src.models import Project, Task

    migration = import_module("migrations.versions.a00000000082_batch_ejection_record")
    database = Database(lease_dsn("batch-ejection"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Ejection migration"))
    await database.create_task(Task(id="forged", project_id="p", title="Forged marker",
                                    description=""))

    def exercise(conn):
        # The live baseline already contains the new column. Replay is a no-op.
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.downgrade()
            migration.downgrade()
        assert "ejection_record" not in {
            column["name"] for column in inspect(conn).get_columns("integration_batches")
        }
        conn.execute(text("INSERT INTO task_metadata (task_id, key, value) "
                          "VALUES ('forged', 'integration_train_ejection:batch', '{}')"))
        # A real pre-revision row remains an ordinary abort after upgrade.
        conn.execute(text("INSERT INTO integration_batches (id, project_id, repository_id, "
                          "request_id, source_manifest_digest, base_sha, lifecycle, "
                          "integration_branch, policy_snapshot, artifact_snapshot, "
                          "cleanup_state, intent, created_at, updated_at) VALUES "
                          "('batch', 'p', 'r', 'batch', 'digest', :sha, 'aborted', "
                          "'refs/heads/aq/batches/old', '{}', '{}', 'pending', 'aborted', 1, 1)"),
                     {"sha": "a" * 40})
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
        [column] = [column for column in inspect(conn).get_columns("integration_batches")
                    if column["name"] == "ejection_record"]
        assert isinstance(column["type"], JSONB) and column["nullable"]
        assert conn.scalar(text("SELECT ejection_record FROM integration_batches "
                                "WHERE id='batch'")) is None
        assert conn.scalar(text("SELECT intent FROM integration_batches WHERE id='batch'")) == "aborted"

    try:
        async with database._engine.begin() as conn:
            await conn.run_sync(exercise)
    finally:
        await database.close()


# -- a00000000038: retire development_deliveries ------------------------------

RETIRE_REVISION = "a00000000038"
SOURCE = "a" * 40
OTHER = "b" * 40


def _journal(row_id, state, manifest, evidence, *, at, reason="published"):
    return {
        "id": row_id, "project_id": "p", "repository_id": "r", "target_ref": "refs/heads/main",
        "expected_sha": "c" * 40, "prepared_sha": "d" * 40, "state": state,
        "manifest": json.dumps(manifest), "evidence": json.dumps(evidence), "reason": reason,
        "created_at": at, "updated_at": at + 1,
    }


def _seed_history(connection) -> None:
    """Representative pending, adopted, archived and branchless legacy history."""
    execute = connection.execute
    execute(text("INSERT INTO projects (id, name, created_at) VALUES ('p', 'P', 1) "
                 "ON CONFLICT DO NOTHING"))
    for task_id, branch in (
        ("coded", "aq/coded"), ("branchless", None), ("recorded", None),
        ("reclosed", None), ("bound", None), ("open-branchless", None),
    ):
        execute(text(
            "INSERT INTO tasks (id, project_id, title, description, status, branch_name, "
            "created_at, updated_at) VALUES (:id, 'p', :id, '', :status, :branch, 1, 1) "
            "ON CONFLICT DO NOTHING"
        ), {"id": task_id, "branch": branch,
            "status": "IN_PROGRESS" if task_id == "open-branchless" else "COMPLETED"})
    execute(text(
        "INSERT INTO archived_tasks (id, project_id, title, description, status, created_at, "
        "updated_at, archived_at) VALUES ('gone', 'p', 'gone', '', 'COMPLETED', 1, 1, 2) "
        "ON CONFLICT DO NOTHING"
    ))
    for completion_id, task_id, commits, at in (
        ("coded-close", "coded", [SOURCE], 5), ("recorded-close", "recorded", [OTHER], 5),
        ("reclosed-close", "reclosed", [], 50), ("bound-close", "bound", [], 50),
    ):
        execute(text(
            "INSERT INTO task_completion_records (id, task_id, outcome, commits, completed_at) "
            "VALUES (:id, :task, 'pass', :commits, :at) ON CONFLICT DO NOTHING"
        ), {"id": completion_id, "task": task_id, "commits": json.dumps(commits), "at": at})
    checks = {"validation": "focused", "conclusion": "passed", "head_sha": "d" * 40,
              "checks": [{"command": "pytest", "exit_code": 0, "output": "ok"}]}
    rows = [
        # Outstanding: a parked conflict with its test evidence.
        _journal("parked", "parked", [{"task_id": "coded", "source_sha": SOURCE,
                                       "parent_task_id": None}],
                 {"kind": "merge_conflict", "detail": "conflict", "conflicting_files": ["x"],
                  "completion_sources": [{"task_id": "coded", "completion_id": "coded-close",
                                          "source_sha": SOURCE}]}, at=10),
        _journal("publishing", "publishing", [{"task_id": "coded", "source_sha": SOURCE}],
                 {"kind": "local"}, at=11),
        # Terminal receipts: provenance only, never their state or conclusions.
        _journal("delivered", "delivered",
                 [{"task_id": "branchless", "source_sha": SOURCE, "parent_task_id": "x",
                   "unrelated": 1},
                  {"task_id": "gone", "source_sha": OTHER},
                  {"task_id": "recorded", "source_sha": OTHER},
                  {"task_id": "reclosed", "source_sha": OTHER},
                  {"task_id": "open-branchless", "source_sha": OTHER}],
                 {"kind": "local", **checks, "resolved_by_main_ancestry": True,
                  "branch_cleanup": {"state": "done"},
                  "resolved_by_delivered_repair": {"task_id": "repair", "completion_id": "r1"},
                  "completion_sources": [{"task_id": "bound", "completion_id": "bound-close",
                                          "source_sha": OTHER}]}, at=20),
        _journal("adopted", "adopted", [{"task_id": "coded", "source_sha": SOURCE,
                                         "acceptance": "operator_equivalent"}],
                 {"kind": "operator_accepted", "operator_id": "human:op"}, at=30,
                 reason="operator adoption"),
        # A cancelled validation deferral still owes recovery work.
        _journal("deferred", "cancelled", [], {"kind": "validation_deferred", "open": True},
                 at=40),
        # Configuration history names no source: nothing to retain.
        _journal("configured", "delivered", [], {"kind": "configuration"}, at=41),
    ]
    for row in rows:
        execute(text(
            "INSERT INTO development_deliveries (id, project_id, repository_id, target_ref, "
            "expected_sha, prepared_sha, state, manifest, evidence, reason, created_at, "
            "updated_at) VALUES (:id, :project_id, :repository_id, :target_ref, :expected_sha, "
            ":prepared_sha, :state, CAST(:manifest AS json), CAST(:evidence AS json), :reason, "
            ":created_at, :updated_at)"
        ), row)


def _retained(connection, event_type):
    rows = connection.execute(text(
        "SELECT payload FROM events WHERE event_type = :type ORDER BY id"
    ), {"type": event_type}).scalars()
    return [json.loads(row) for row in rows]


async def _alembic(engine, direction, target):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    def run(conn):
        config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
        config.attributes["connection"] = conn
        getattr(command, direction)(config, target)

    async with engine.begin() as conn:
        await conn.run_sync(run)


async def test_retirement_keeps_true_events_idempotently_and_exposes_unknown_provenance(tmp_path):
    from types import SimpleNamespace

    from sqlalchemy import select

    from src.database.engine import create_postgres_engine
    from src.database.tables import tasks
    from src.git.manager import GitManager
    from src.integration.delivery_truth import (
        MISSING_PROVENANCE,
        DeliverySnapshot,
        DeliveryState,
        load_delivery_requests,
    )
    from src.integration.development import operation_rows_on
    from src.integration.provenance import LEGACY_PROVENANCE_EVENT
    from src.integration.publishable_artifact import LEGACY_ARTIFACT_KEY, legacy_artifact
    from tests.alembic_revisions import previous_revision
    from tests.pg_dsn import create_scratch_database

    engine = create_postgres_engine(await create_scratch_database("retiredeliveries"))
    try:
        await _alembic(engine, "upgrade", previous_revision(RETIRE_REVISION))
        async with engine.begin() as conn:
            await conn.run_sync(_seed_history)
            # migrate-provenance --apply already retained one action before upgrade.
            await conn.execute(text(
                "INSERT INTO events (event_type, project_id, payload, timestamp) VALUES "
                "('development.operation', 'p', :payload, 1)"
            ), {"payload": json.dumps({"id": "legacy-operation:parked", "state": "parked",
                                       "project_id": "p", "manifest": [], "evidence": {},
                                       "created_at": 10, "updated_at": 11})})
        await _alembic(engine, "upgrade", RETIRE_REVISION)

        async with engine.connect() as conn:
            assert "development_deliveries" not in await conn.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
            operations = await conn.run_sync(_retained, "development.operation")
            provenance = await conn.run_sync(_retained, LEGACY_PROVENANCE_EVENT)
            summary = await conn.run_sync(_retained, "development.legacy_retirement")
            markers = dict((await conn.execute(text(
                "SELECT task_id, value FROM task_metadata WHERE key = :key ORDER BY task_id"
            ), {"key": LEGACY_ARTIFACT_KEY})).all())
            live = await operation_rows_on(conn, ["p"])
            marked = set((await conn.execute(
                select(tasks.c.id).where(legacy_artifact(tasks))
            )).scalars())

        # Outstanding actions only, once each; receipt bindings never ride along.
        assert sorted(op["id"] for op in operations) == [
            "legacy-operation:deferred", "legacy-operation:parked", "legacy-operation:publishing",
        ]
        assert {row["id"]: row["state"] for row in live} == {
            "legacy-operation:deferred": "cancelled", "legacy-operation:parked": "parked",
            "legacy-operation:publishing": "publishing",
        }
        assert all("completion_sources" not in op["evidence"] for op in operations)

        # Provenance for every row naming a source; state and conclusions dropped.
        by_legacy = {row["legacy_id"]: row for row in provenance}
        assert set(by_legacy) == {"parked", "publishing", "delivered", "adopted"}
        for row in provenance:
            assert "state" not in row and "resolved_by_main_ancestry" not in row
            assert "branch_cleanup" not in json.dumps(row)
        delivered = by_legacy["delivered"]
        assert delivered["manifest"][0] == {"task_id": "branchless", "source_sha": SOURCE,
                                            "parent_task_id": "x"}
        assert delivered["tests"]["checks"][0]["command"] == "pytest"
        assert delivered["resolved_by_delivered_repair"] == {"task_id": "repair",
                                                             "completion_id": "r1"}
        assert by_legacy["parked"]["conflict"]["conflicting_files"] == ["x"]
        assert by_legacy["parked"]["completion_sources"][0]["completion_id"] == "coded-close"
        assert by_legacy["adopted"]["manifest"][0]["acceptance"] == "operator_equivalent"

        # Branchless generations a manifest located stay unknown, fenced to the
        # generation; recorded, re-closed, open and archived tasks are not marked.
        assert set(markers) == marked == {"branchless", "bound"}
        assert json.loads(markers["bound"])["completion_id"] == "bound-close"
        assert json.loads(markers["branchless"])["completion_id"] is None
        (report,) = summary
        assert report["rows"] == 6
        assert report["unresolved_artifacts"] == ["bound", "branchless"]
        assert report["unresolved_archived"] == ["gone"]
        assert sorted(report["operations_retained"]) == ["deferred", "parked", "publishing"]

        db = SimpleNamespace(_engine=engine, _row_to_task_completion=Database._row_to_task_completion)
        requests = await load_delivery_requests(
            db, {"branchless", "open-branchless", "coded"}, repository_id="r",
            target_ref="refs/heads/main",
        )
        # Missing legacy generations now consult Git. An empty repository has
        # no retained provenance, even though the migration kept a manifest.
        git = GitManager()
        initialized = await git.arun_git_result(["init", "--bare"], cwd=str(tmp_path))
        assert initialized.returncode == 0, initialized.stderr
        snapshot = DeliverySnapshot(
            git, str(tmp_path), "p", "r", "u", "refs/heads/main", "e" * 40, {},
        )
        branchless = await snapshot.evaluate(requests["branchless"])
        assert (branchless.state, branchless.reason) == (DeliveryState.UNKNOWN, MISSING_PROVENANCE)
        organizational = await snapshot.evaluate(requests["open-branchless"])
        assert organizational.state is DeliveryState.NO_ARTIFACT

        # Idempotent: a recreated journal replaying the same history adds nothing.
        await _alembic(engine, "downgrade", previous_revision(RETIRE_REVISION))
        async with engine.begin() as conn:
            await conn.run_sync(_seed_history)
        await _alembic(engine, "upgrade", RETIRE_REVISION)

        def rerun(sync):
            # The body itself is guarded: with the journal gone it does nothing.
            from importlib import import_module

            from alembic.migration import MigrationContext
            from alembic.operations import Operations

            revision = import_module(
                "migrations.versions.a00000000038_retire_development_deliveries"
            )
            with Operations.context(MigrationContext.configure(sync)):
                revision.upgrade()

        async with engine.begin() as conn:
            await conn.run_sync(rerun)
        async with engine.connect() as conn:
            assert len(await conn.run_sync(_retained, "development.operation")) == 3
            assert len(await conn.run_sync(_retained, LEGACY_PROVENANCE_EVENT)) == 4
            assert len(await conn.run_sync(_retained, "development.legacy_retirement")) == 1
            assert (await conn.execute(text(
                "SELECT count(*) FROM task_metadata WHERE key = :key"
            ), {"key": LEGACY_ARTIFACT_KEY})).scalar_one() == 2
    finally:
        await engine.dispose()
