"""Dual-dialect migration coverage for Task 10c cleanup hardening."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn


pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "a10c5e1e4f02"
REVISION = "a10c5e1e4f03"
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def _check_names(connection, table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(connection).get_check_constraints(table)
    }


def _exercise_upgrade_downgrade_upgrade(connection) -> None:
    _migrate(connection, PRIOR, downgrade=True)
    assert "source_ref" not in {
        column["name"] for column in inspect(connection).get_columns("integration_batch_members")
    }

    _migrate(connection, REVISION)
    assert {"source_ref", "source_ref_retention"} <= {
        column["name"] for column in inspect(connection).get_columns("integration_batch_members")
    }

    assert (
        "ck_integration_batch_members_source_retention"
        in _check_names(connection, "integration_batch_members")
    )
    connection.execute(
        text(
            "INSERT INTO projects (id, name, created_at) "
            "VALUES ('hardening-project', 'hardening project', 1)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO repos (id, project_id, url, default_branch, checkout_base_path, "
            "source_type, source_path) VALUES ('hardening-repo', 'hardening-project', "
            "'https://github.com/acme/widgets.git', 'main', '/daemon/repos', 'clone', '')"
        )
    )
    connection.execute(
        text(
            "INSERT INTO integration_batches (id, project_id, repository_id, request_id, "
            "source_manifest_digest, base_sha, lifecycle, current_revision, integration_branch, "
            "final_main_sha, policy_snapshot, artifact_snapshot, cleanup_state, created_at, "
            "updated_at) VALUES ('hardening-batch', 'hardening-project', 'hardening-repo', "
            "'request', 'digest', :sha, 'promoted', 0, 'refs/heads/aq/integration/test', "
            ":sha, '{}', '{}', 'pending', 1, 1)"
        ),
        {"sha": "a" * 40},
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            text(
                "INSERT INTO integration_cleanup_items (batch_id, kind, identity, domain_key, "
                "project_id, repository_id, repository_numeric_id, repository_full_name, "
                "revision, target_pr_url, expected_sha, state, attempts, next_attempt_at, "
                "created_at, updated_at) VALUES ('hardening-batch', 'audit_pr', "
                "'missing-number', 'cleanup:hardening:audit:missing', 'hardening-project', "
                "'hardening-repo', 99, 'acme/widgets', 0, "
                "'https://github.com/acme/widgets/pull/9', :sha, 'pending', 0, 1, 1, 1)"
            ),
            {"sha": "a" * 40},
        )

    _migrate(connection, PRIOR, downgrade=True)
    connection.execute(
        text(
            "INSERT INTO integration_cleanup_items (batch_id, kind, identity, domain_key, "
            "project_id, repository_id, repository_numeric_id, repository_full_name, revision, "
            "workspace_path, expected_sha, state, attempts, next_attempt_at, irreversible_nonce, "
            "irreversible_prewrite_at, created_at, updated_at) VALUES ('hardening-batch', "
            "'worktree', 'retained-workspace', 'cleanup:hardening:worktree:retained', "
            "'hardening-project', 'hardening-repo', 99, 'acme/widgets', 0, '/daemon/retained', "
            ":sha, 'retryable', 1, 1, 'irreversible-owner', 2, 1, 2)"
        ),
        {"sha": "a" * 40},
    )
    with pytest.raises(
        RuntimeError,
        match=(
            "drain irreversible cleanup reservation "
            "hardening-batch:worktree:retained-workspace before downgrade"
        ),
    ):
        _migrate(connection, "a10c5e1e4f01", downgrade=True)
    marked = connection.execute(
        text(
            "SELECT irreversible_nonce, irreversible_prewrite_at "
            "FROM integration_cleanup_items WHERE batch_id = 'hardening-batch'"
        )
    ).one()
    assert tuple(marked) == ("irreversible-owner", 2.0)

    connection.execute(
        text("DELETE FROM integration_cleanup_items WHERE batch_id = 'hardening-batch'")
    )
    _migrate(connection, "a10c5e1e4f01", downgrade=True)
    assert "irreversible_nonce" not in {
        column["name"] for column in inspect(connection).get_columns("integration_cleanup_items")
    }
    _migrate(connection, REVISION)
    assert "irreversible_nonce" in {
        column["name"] for column in inspect(connection).get_columns("integration_cleanup_items")
    }
    _migrate(connection, PRIOR, downgrade=True)
    assert "source_ref" not in {
        column["name"] for column in inspect(connection).get_columns("integration_batch_members")
    }
    _migrate(connection, REVISION)
    assert {"source_ref", "source_ref_retention"} <= {
        column["name"] for column in inspect(connection).get_columns("integration_batch_members")
    }


async def test_sqlite_cleanup_hardening_migration_round_trip(tmp_path):
    path = tmp_path / "cleanup-hardening.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            _exercise_upgrade_downgrade_upgrade(connection)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_cleanup_hardening_migration_round_trip():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("task10c_cleanup_hardening")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    engine = None
    try:
        await database.initialize()
        await database.close()
        engine = create_postgres_engine(dsn, 0, 1)
        async with engine.begin() as connection:
            await connection.run_sync(_exercise_upgrade_downgrade_upgrade)
    finally:
        await database.close()
        if engine is not None:
            await engine.dispose()
        prefix, _, name = dsn.rpartition("/")
        admin = await asyncpg.connect(
            prefix.replace("postgresql+asyncpg://", "postgresql://") + "/postgres"
        )
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()
