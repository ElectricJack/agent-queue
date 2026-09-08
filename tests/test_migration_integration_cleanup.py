"""Current cleanup schema accepts valid work and rejects incomplete PR identity."""

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from src.database import Database
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.migration


def _assert_cleanup_contract(connection) -> None:
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


async def test_baseline_cleanup_contract():
    database = Database(lease_dsn("cleanup"))
    await database.initialize()
    try:
        async with database._engine.begin() as conn:
            await conn.run_sync(_assert_cleanup_contract)
    finally:
        await database.close()
