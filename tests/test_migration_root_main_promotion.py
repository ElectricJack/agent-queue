"""Dialect coverage for normalized root-to-main promotion state."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from src.database import Database
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "d4a81f0c9e72"
REVISION = "e9b2f1b7c3d5"
#: ``Database.initialize()`` migrates to the current head.  On PostgreSQL a
#: refused downgrade inside ``engine.begin()`` rolls the whole chain back to
#: HEAD (transactional DDL); on SQLite the steps above REVISION have already
#: committed, so the refusal leaves the database at REVISION itself.
HEAD = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
POSTGRES_DSN = ensure_worker_postgres_dsn()


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


def _assert_root_schema(connection) -> None:
    inspector = inspect(connection)
    assert "integration_root_intent_members" in inspector.get_table_names()
    intent_columns = {
        column["name"] for column in inspector.get_columns("integration_promotion_intents")
    }
    assert {
        "intent_kind",
        "root_batch_id",
        "root_candidate_revision",
        "project_lease_owner_id",
        "project_lease_fence_token",
        "branch_fence_owner_id",
        "branch_fence_token",
        "ci_evidence_id",
    } <= intent_columns
    mutation_columns = {
        column["name"]
        for column in inspector.get_columns("integration_candidate_ref_mutations")
    }
    assert "prewrite_at" in mutation_columns
    unique_names = {
        item["name"]
        for table in (
            "integration_promotion_intents",
            "integration_batch_members",
            "integration_candidate_member_results",
            "integration_review_evidence",
        )
        for item in (
            inspector.get_unique_constraints(table) + inspector.get_indexes(table)
        )
    }
    assert {
        "uq_integration_promotion_intents_root_identity",
        "uq_integration_batch_members_root_identity",
        "uq_integration_candidate_results_root_identity",
        "uq_integration_review_evidence_root_identity",
    } <= unique_names
    root_member_fks = {
        item["name"] for item in inspector.get_foreign_keys("integration_root_intent_members")
    }
    assert {
        "fk_integration_root_intent_members_exact_intent",
        "fk_integration_root_intent_members_exact_member",
        "fk_integration_root_intent_members_exact_result",
        "fk_integration_root_intent_members_exact_review",
    } <= root_member_fks
    receipt_indexes = {
        item["name"] for item in inspector.get_indexes("task_delivery_receipts")
    }
    assert "uq_task_delivery_receipts_root_tuple" in receipt_indexes


def _seed_child_receipt(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO task_delivery_receipts "
            "(id, domain_key, repository_id, target_branch, disposition, created_at) VALUES "
            "('child-receipt', 'child-domain', 'repo', 'refs/heads/main', 'code', 1)"
        )
    )


def _assert_receipt_append_only(connection) -> None:
    for statement in (
        "UPDATE task_delivery_receipts SET target_branch = 'changed' "
        "WHERE id = 'child-receipt'",
        "DELETE FROM task_delivery_receipts WHERE id = 'child-receipt'",
    ):
        with pytest.raises(Exception, match="task delivery receipts are append-only"):
            with connection.begin_nested():
                connection.execute(text(statement))


def _seed_root_history(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO integration_batches "
            "(id, project_id, repository_id, request_id, source_manifest_digest, base_sha, "
            "lifecycle, current_revision, integration_branch, policy_snapshot, artifact_snapshot, "
            "cleanup_state, created_at, updated_at) VALUES "
            "('batch', 'project', 'repo', 'request', 'digest', :base, 'promoting', 0, "
            "'refs/heads/integration/batch', '{}', '{}', 'pending', 1, 1)"
        ),
        {"base": "a" * 40},
    )
    connection.execute(
        text(
            "INSERT INTO integration_candidate_revisions "
            "(batch_id, revision, construction_base_sha, next_member_ordinal, head_sha, "
            "state, created_at, updated_at) VALUES "
            "('batch', 0, :base, 0, :sha, 'green', 1, 1)"
        ),
        {"base": "a" * 40, "sha": "b" * 40},
    )
    connection.execute(
        text(
            "INSERT INTO integration_promotion_intents "
            "(id, domain_key, receipt_id, source_head, source_base, repository_id, "
            "target_branch, expected_target, prepared_sha, recovery_ref, fence_owner_id, "
            "fence_token, state, created_at, updated_at, intent_kind, root_batch_id, "
            "root_candidate_revision, project_lease_owner_id, project_lease_fence_token, "
            "branch_fence_owner_id, branch_fence_token, ci_evidence_id) VALUES "
            "('root-intent', 'root:batch:0', 'root-receipt', :sha, :base, 'repo', "
            "'refs/heads/main', :base, :sha, 'refs/aq/root/root-intent', 'legacy', 1, "
            "'prepared', 1, 1, 'root', 'batch', 0, 'lease-owner', 3, "
            "'branch-owner', 7, 'ci-green')"
        ),
        {"sha": "b" * 40, "base": "a" * 40},
    )


def _seed_legacy_root_reservation(connection, invalid: str) -> None:
    batch_id = "batch"
    intent_batch_id = "other" if invalid == "cross_intent" else batch_id
    member_task = "member-task"
    reserved_task = "wrong-task" if invalid == "member" else member_task
    member_head = "c" * 40 if invalid == "candidate" else "b" * 40
    result_head = "b" * 40
    review_task = "other-task" if invalid == "review" else reserved_task
    candidate_evidence = '{"accepted": true}' if invalid == "result_evidence" else "{}"
    connection.execute(
        text(
            "INSERT INTO integration_batches "
            "(id, project_id, repository_id, request_id, source_manifest_digest, base_sha, "
            "lifecycle, current_revision, integration_branch, policy_snapshot, artifact_snapshot, "
            "cleanup_state, created_at, updated_at) VALUES "
            "(:batch, 'project', 'repo', :request, 'digest', :base, 'sealing', 0, "
            "'refs/heads/integration/batch', '{}', '{}', 'pending', 1, 1)"
        ),
        {"batch": batch_id, "request": "request", "base": "a" * 40},
    )
    if intent_batch_id != batch_id:
        connection.execute(
            text(
                "INSERT INTO integration_batches "
                "(id, project_id, repository_id, request_id, source_manifest_digest, base_sha, "
                "lifecycle, current_revision, integration_branch, policy_snapshot, "
                "artifact_snapshot, cleanup_state, created_at, updated_at) VALUES "
                "(:batch, 'other-project', 'repo', 'other-request', 'other-digest', :base, 'sealing', "
                "0, 'refs/heads/integration/other', '{}', '{}', 'pending', 1, 1)"
            ),
            {"batch": intent_batch_id, "base": "a" * 40},
        )
    connection.execute(
        text(
            "INSERT INTO integration_review_evidence "
            "(id, source_task_id, repository_id, source_base, reviewed_head_sha, "
            "reviewed_tree_sha, reviewer_task_id, review_kind, generation, verdict, evidence, "
            "created_at) VALUES ('review', :task, 'repo', :base, :head, :tree, 'reviewer', "
            "'independent', 0, 'approved', '{}', 1)"
        ),
        {"task": review_task, "base": "a" * 40, "head": member_head, "tree": "d" * 40},
    )
    connection.execute(
        text(
            "INSERT INTO integration_batch_members "
            "(batch_id, ordinal, task_id, repository_id, source_base_sha, reviewed_head_sha, "
            "reviewed_tree_sha, review_evidence_id, review_evidence) VALUES "
            "(:batch, 0, :task, 'repo', :base, :head, :tree, 'review', '{}')"
        ),
        {
            "batch": batch_id,
            "task": member_task if invalid == "member" else reserved_task,
            "base": "a" * 40,
            "head": member_head,
            "tree": "d" * 40,
        },
    )
    for revision_batch in {batch_id, intent_batch_id}:
        connection.execute(
            text(
                "INSERT INTO integration_candidate_revisions "
                "(batch_id, revision, construction_base_sha, next_member_ordinal, head_sha, "
                "state, created_at, updated_at) VALUES "
                "(:batch, 0, :base, 1, :head, 'green', 1, 1)"
            ),
            {"batch": revision_batch, "base": "a" * 40, "head": "e" * 40},
        )
    connection.execute(
        text(
            "INSERT INTO integration_candidate_member_results "
            "(batch_id, revision, member_ordinal, input_head_sha, input_tree_sha, "
            "generated_squash_sha, result, conflict_evidence, created_at, updated_at) VALUES "
            "(:batch, 0, 0, :head, :tree, :squash, 'applied', :evidence, 1, 1)"
        ),
        {
            "batch": batch_id,
            "head": result_head,
            "tree": "d" * 40,
            "squash": "f" * 40,
            "evidence": candidate_evidence,
        },
    )
    connection.execute(
        text(
            "INSERT INTO integration_promotion_intents "
            "(id, domain_key, receipt_id, source_head, source_base, repository_id, "
            "target_branch, expected_target, prepared_sha, recovery_ref, fence_owner_id, "
            "fence_token, state, created_at, updated_at, intent_kind, root_batch_id, "
            "root_candidate_revision, project_lease_owner_id, project_lease_fence_token, "
            "branch_fence_owner_id, branch_fence_token, ci_evidence_id) VALUES "
            "('root-intent', 'root-domain', 'root-receipt', :head, :base, 'repo', "
            "'refs/heads/main', :base, :head, 'refs/aq/root/root-intent', 'legacy', 1, "
            "'prepared', 1, 1, 'root', :batch, 0, 'lease-owner', 3, "
            "'branch-owner', 7, 'ci-green')"
        ),
        {"batch": intent_batch_id, "head": "e" * 40, "base": "a" * 40},
    )
    connection.execute(
        text(
            "INSERT INTO integration_root_intent_members "
            "(intent_id, member_ordinal, receipt_id, batch_id, candidate_revision, "
            "source_task_id, repository_id, reviewed_head_sha, reviewed_tree_sha, "
            "generated_squash_sha, result_evidence, review_evidence_id, created_at) VALUES "
            "('root-intent', 0, 'root-receipt', :batch, 0, :task, 'repo', :head, :tree, "
            ":squash, :evidence, 'review', 1)"
        ),
        {
            "batch": batch_id,
            "task": reserved_task,
            "head": member_head,
            "tree": "d" * 40,
            "squash": "f" * 40,
            "evidence": "{}",
        },
    )
    if invalid == "duplicate_receipt":
        for suffix in ("a", "b"):
            connection.execute(
                text(
                    "INSERT INTO task_delivery_receipts "
                    "(id, domain_key, repository_id, target_branch, batch_id, member_ordinal, "
                    "candidate_revision, disposition, created_at) VALUES "
                    "(:id, :domain, 'repo', 'refs/heads/main', :batch, 0, 0, 'code', 1)"
                ),
                {"id": f"duplicate-{suffix}", "domain": f"duplicate-{suffix}", "batch": batch_id},
            )


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        ("cross_intent", "cross-intent identity"),
        ("member", "mismatches sealed member"),
        ("candidate", "mismatches candidate result"),
        ("review", "mismatches review subject"),
        ("result_evidence", "result-evidence drift"),
        ("duplicate_receipt", "duplicate root receipt tuple"),
    ],
)
async def test_sqlite_upgrade_rejects_incompatible_legacy_root_identity(
    tmp_path, invalid, message
):
    path = tmp_path / f"legacy-{invalid}.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            _migrate(connection, PRIOR, downgrade=True)
            _seed_legacy_root_reservation(connection, invalid)
        with engine.begin() as connection, pytest.raises(RuntimeError, match=message):
            _migrate(connection, REVISION)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == PRIOR
            assert connection.execute(
                text("SELECT intent_id, batch_id FROM integration_root_intent_members")
            ).one() == ("root-intent", "batch")
        with engine.begin() as connection, connection.begin_nested():
            with pytest.raises(Exception, match="root intent member reservations are append-only"):
                connection.execute(
                    text("UPDATE integration_root_intent_members SET source_task_id = 'changed'")
                )
    finally:
        engine.dispose()


async def test_sqlite_root_promotion_schema_and_guarded_round_trip(tmp_path):
    path = tmp_path / "root-promotion.db"
    database = Database(str(path))
    await database.initialize()
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            _assert_root_schema(connection)
        with engine.begin() as connection:
            _seed_child_receipt(connection)
        with engine.begin() as connection:
            _assert_receipt_append_only(connection)
        with engine.begin() as connection:
            _seed_root_history(connection)
        with engine.begin() as connection:
            with pytest.raises(RuntimeError, match="drain or reconcile root promotion history"):
                _migrate(connection, PRIOR, downgrade=True)
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT intent_kind FROM integration_promotion_intents")
            ).scalar_one() == "root"
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == REVISION
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM integration_promotion_intents"))
            _migrate(connection, PRIOR, downgrade=True)
            _migrate(connection, REVISION)
        with engine.connect() as connection:
            _assert_root_schema(connection)
        with engine.begin() as connection:
            _assert_receipt_append_only(connection)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_root_promotion_schema_and_guarded_round_trip():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("root_promotion_d4")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_assert_root_schema)
        async with engine.begin() as connection:
            await connection.run_sync(_seed_child_receipt)
        async with engine.begin() as connection:
            await connection.run_sync(_assert_receipt_append_only)
        async with engine.begin() as connection:
            await connection.run_sync(_seed_root_history)
        with pytest.raises(RuntimeError, match="drain or reconcile root promotion history"):
            async with engine.begin() as connection:
                await connection.run_sync(lambda sync: _migrate(sync, PRIOR, downgrade=True))
        async with engine.connect() as connection:
            assert (
                await connection.execute(text("SELECT intent_kind FROM integration_promotion_intents"))
            ).scalar_one() == "root"
            assert (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == HEAD
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM integration_promotion_intents"))
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: _migrate(sync, PRIOR, downgrade=True))
            await connection.run_sync(lambda sync: _migrate(sync, REVISION))
        async with engine.connect() as connection:
            await connection.run_sync(_assert_root_schema)
        async with engine.begin() as connection:
            await connection.run_sync(_assert_receipt_append_only)
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


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_upgrade_rejects_cross_intent_root_reservation_without_ddl():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("root_receipt_guard_e9")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: _migrate(sync, PRIOR, downgrade=True))
            await connection.run_sync(
                lambda sync: _seed_legacy_root_reservation(sync, "cross_intent")
            )
        with pytest.raises(RuntimeError, match="cross-intent identity"):
            async with engine.begin() as connection:
                await connection.run_sync(lambda sync: _migrate(sync, REVISION))
        async with engine.connect() as connection:
            assert (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == PRIOR
            assert (
                await connection.execute(
                    text("SELECT intent_id, batch_id FROM integration_root_intent_members")
                )
            ).one() == ("root-intent", "batch")
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: _assert_root_member_append_only(sync)
            )
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


def _assert_root_member_append_only(connection) -> None:
    with pytest.raises(Exception, match="root intent member reservations are append-only"):
        with connection.begin_nested():
            connection.execute(
                text("UPDATE integration_root_intent_members SET source_task_id = 'changed'")
            )
