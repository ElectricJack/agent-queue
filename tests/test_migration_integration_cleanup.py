"""Dual-dialect migration coverage for normalized integration cleanup."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "f0a1b2c3d4e5"
REVISION = "18cd4540cd0d"
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def _exercise_round_trip(connection) -> None:
    assert "integration_cleanup_items" in inspect(connection).get_table_names()
    connection.execute(
        text(
            "INSERT INTO projects (id, name, created_at) "
            "VALUES ('cleanup-project', 'cleanup project', 1)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO repos (id, project_id, url, default_branch, checkout_base_path, "
            "source_type, source_path) VALUES ('cleanup-repo', 'cleanup-project', "
            "'https://github.com/acme/widgets.git', 'main', '/daemon/repos', 'clone', '')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO integration_batches (id, project_id, repository_id, request_id, "
            "source_manifest_digest, base_sha, lifecycle, current_revision, integration_branch, "
            "final_main_sha, policy_snapshot, artifact_snapshot, cleanup_state, created_at, "
            "updated_at) VALUES ('cleanup-batch', 'cleanup-project', 'cleanup-repo', 'request', "
            "'digest', :sha, 'promoted', 0, 'refs/heads/aq/integration/test', :sha, '{}', '{}', "
            "'pending', 1, 1)"
        ),
        {"sha": "a" * 40},
    )
    connection.execute(
        text(
            "INSERT INTO integration_cleanup_items (batch_id, kind, identity, domain_key, "
            "project_id, repository_id, repository_numeric_id, repository_full_name, revision, "
            "target_ref, expected_sha, state, attempts, next_attempt_at, created_at, updated_at) "
            "VALUES ('cleanup-batch', 'remote_ref', 'refs/heads/aq/integration/test', "
            "'cleanup:batch:remote', 'cleanup-project', 'cleanup-repo', 99, 'acme/widgets', 0, "
            "'refs/heads/aq/integration/test', :sha, 'pending', 0, 1, 1, 1)"
        ),
        {"sha": "a" * 40},
    )
    with pytest.raises(RuntimeError, match="cleanup-batch:remote_ref"):
        _migrate(connection, PRIOR, downgrade=True)
    connection.execute(text("DELETE FROM integration_cleanup_items"))
    with pytest.raises(RuntimeError, match="cleanup-batch"):
        _migrate(connection, PRIOR, downgrade=True)
    connection.execute(
        text(
            "UPDATE integration_batches SET cleanup_state = 'complete' "
            "WHERE id = 'cleanup-batch'"
        )
    )
    _migrate(connection, PRIOR, downgrade=True)
    assert "integration_cleanup_items" not in inspect(connection).get_table_names()
    _migrate(connection, REVISION)
    assert "integration_cleanup_items" in inspect(connection).get_table_names()
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text(
                "INSERT INTO integration_cleanup_items (batch_id, kind, identity, domain_key, "
                "project_id, repository_id, repository_numeric_id, repository_full_name, "
                "revision, target_pr_url, expected_sha, state, attempts, next_attempt_at, "
                "created_at, updated_at) VALUES ('cleanup-batch', 'audit_pr', 'missing-number', "
                "'cleanup:batch:audit:missing', 'cleanup-project', 'cleanup-repo', 99, "
                "'acme/widgets', 0, 'https://github.com/acme/widgets/pull/9', :sha, "
                "'pending', 0, 1, 1, 1)"
            ),
            {"sha": "a" * 40},
        )


async def test_sqlite_cleanup_migration_guarded_round_trip(tmp_path):
    path = tmp_path / "cleanup.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            _exercise_round_trip(connection)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_cleanup_migration_guarded_round_trip():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("task10c_cleanup")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_exercise_round_trip)
    finally:
        await engine.dispose()
        prefix, _, name = dsn.rpartition("/")
        admin = await asyncpg.connect(
            prefix.replace("postgresql+asyncpg://", "postgresql://") + "/postgres"
        )
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()
