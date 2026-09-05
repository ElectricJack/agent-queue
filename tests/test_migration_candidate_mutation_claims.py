"""Dialect round trips for durable candidate mutation claims."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from src.database import Database
from src.database.tables import (
    integration_candidate_member_results,
    integration_candidate_resolutions,
    integration_candidate_revisions,
    integration_repair_stages,
    playbook_artifacts,
    workspaces,
)
from src.models import AgentProfile, Project, RepoConfig, RepoSourceType, SessionRecord, Task
from tests.test_integration_candidates import _artifact, _policy, _seed_batch
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.perf, pytest.mark.migration]

PRIOR = "69416e65ee21"
REVISION = "e1eab6dbc186"
HEAD = "46f910d0dce6"
POSTGRES_DSN = ensure_worker_postgres_dsn()


async def _seed_legacy_resolution(database, workspace_path: str) -> None:
    await database.create_profile(AgentProfile(id="repairer", name="Repairer"))
    await database.create_project(Project(id="p", name="project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
            url="https://github.com/example/repo.git",
            default_branch="main",
        )
    )
    await database.update_project(
        "p",
        hierarchical_integration_mode="train",
        integration_repository_id="repo",
        hierarchical_integration_policy=_policy(),
    )
    for task_id in ("root-0", "reviewer-0", "repair-task"):
        await database.create_task(
            Task(id=task_id, project_id="p", title=task_id, description=task_id)
        )
    async with database.immediate() as conn:
        await conn.execute(
            playbook_artifacts.insert().values(
                **_artifact().model_dump(),
                scope="project",
                scope_identifier="p",
                profile_fingerprint="",
                path="/tmp/task9b1-artifact",
                size_bytes=1,
                validation="{}",
                created_at=1.0,
            )
        )
    await _seed_batch(database, members=[("a" * 40, "b" * 40, "c" * 40)])
    async with database.immediate() as conn:
        await conn.execute(
            integration_candidate_revisions.insert().values(
                batch_id="batch",
                revision=0,
                construction_base_sha="a" * 40,
                next_member_ordinal=0,
                head_sha="d" * 40,
                state="constructing",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            integration_candidate_member_results.insert().values(
                batch_id="batch",
                revision=0,
                member_ordinal=0,
                input_head_sha="b" * 40,
                input_tree_sha="c" * 40,
                result="conflict",
                conflict_evidence={"partial_head_sha": "d" * 40},
                created_at=1.0,
                updated_at=1.0,
            )
        )
        await conn.execute(
            integration_repair_stages.insert().values(
                operation_id="repair-batch-batch",
                ordinal=0,
                policy={},
                repair_task_id="repair-task",
                writer_kind="repair_delegate",
                starting_sha="d" * 40,
                started_at=1.0,
                deadline_at=999.0,
                attempts=1,
                state="active",
            )
        )
        await conn.execute(
            workspaces.insert().values(
                id="workspace",
                project_id="p",
                workspace_path=workspace_path,
                source_type="link",
                locked_by_task_id="repair-task",
                enabled=True,
                created_at=1.0,
            )
        )
    await database.create_session(
        SessionRecord(
            id="session",
            task_id="repair-task",
            project_id="p",
            profile_id="repairer",
            harness="fake",
            provider="fake",
            name="repair",
            lifecycle="task",
            state="stopped",
            work_dir=workspace_path,
            epoch="test",
            instance_token="instance",
            started_at=1.0,
        )
    )
    branch = "refs/heads/aq/integration/p-" + "5" * 32 + "/r-" + "6" * 32
    async with database.immediate() as conn:
        await conn.execute(
            integration_candidate_resolutions.insert().values(
                id="legacy-resolution",
                batch_id="batch",
                revision=0,
                member_ordinal=0,
                operation_id="repair-batch-batch",
                operation_episode_id="batch",
                stage_ordinal=0,
                stage_deadline_at=999.0,
                project_id="p",
                repair_task_id="repair-task",
                repair_session_id="session",
                repair_session_instance_token="instance",
                repair_workspace_id="workspace",
                repair_workspace_path=workspace_path,
                repository_id="repo",
                branch=branch,
                target_branch=branch,
                target_kind="legacy_integration",
                fence_owner_id="repair-task",
                fence_token=1,
                partial_head_sha="d" * 40,
                source_base_sha="a" * 40,
                source_head_sha="b" * 40,
                resolved_head_sha="e" * 40,
                resolved_tree_sha="f" * 40,
                repair_commit_shas=["e" * 40],
                push_evidence={"remote_sha": "e" * 40},
                state="pushed",
                created_at=1.0,
                updated_at=1.0,
            )
        )


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    (command.downgrade if downgrade else command.upgrade)(config, revision)


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
    } == {
        fk["name"] for fk in schema.get_foreign_keys("integration_candidate_ref_mutations")
    }
    if connection.dialect.name == "sqlite":
        guards = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'trg_candidate_%'"
                )
            )
        )
    else:
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


def _bind_live_handoff(connection) -> None:
    connection.execute(
        text(
            "UPDATE integration_candidate_resolutions "
            "SET handoff_owner_id='repair-batch-batch', handoff_fence_token=2 "
            "WHERE id='legacy-resolution'"
        )
    )


async def test_sqlite_candidate_mutation_claim_upgrade_downgrade_upgrade(tmp_path):
    path = tmp_path / "candidate-mutations.db"
    database = Database(str(path))
    await database.initialize()
    workspace = tmp_path / "legacy-workspace"
    workspace.mkdir()
    await _seed_legacy_resolution(database, str(workspace.resolve()))
    await database.close()
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
            _migrate(conn, HEAD)
        with engine.connect() as conn:
            _assert_schema(conn)
        with engine.connect() as conn:
            legacy = conn.execute(
                text(
                    "SELECT branch, target_branch, target_kind, repair_workspace_path "
                    "FROM integration_candidate_resolutions WHERE id='legacy-resolution'"
                )
            ).one()
            assert legacy.target_branch == legacy.branch
            assert legacy.target_kind == "legacy_integration"
            assert legacy.repair_workspace_path == str(workspace.resolve())
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
            conn.execute(
                text(
                    "UPDATE integration_repair_stages SET deadline_at=NULL "
                    "WHERE operation_id='repair-batch-batch' AND ordinal=0"
                )
            )
            with pytest.raises(RuntimeError, match="irreconstructible legacy authority"):
                _migrate(conn, REVISION)
            conn.execute(
                text(
                    "UPDATE integration_repair_stages SET deadline_at=999 "
                    "WHERE operation_id='repair-batch-batch' AND ordinal=0"
                )
            )
            _migrate(conn, HEAD)
        with engine.begin() as conn:
            _migrate(conn, PRIOR, downgrade=True)
            _migrate(conn, HEAD)
        with engine.connect() as conn:
            _assert_schema(conn)
        with engine.begin() as conn:
            _bind_live_handoff(conn)
        with engine.begin() as conn:
            with pytest.raises(RuntimeError, match="live candidate handoff provenance"):
                _migrate(conn, REVISION, downgrade=True)
        with engine.connect() as conn:
            handoff = conn.execute(
                text(
                    "SELECT handoff_owner_id, handoff_fence_token "
                    "FROM integration_candidate_resolutions WHERE id='legacy-resolution'"
                )
            ).one()
            assert handoff == ("repair-batch-batch", 2)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_DSN, reason="POSTGRES_TEST_DSN not set")
async def test_postgres_candidate_mutation_claim_upgrade_downgrade_upgrade():
    import asyncpg

    from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter
    from src.database.engine import create_postgres_engine

    dsn = await create_scratch_database("task9b1_candidate_mutations")
    database = PostgreSQLDatabaseAdapter(dsn, 0, 1)
    await database.initialize()
    await _seed_legacy_resolution(database, "/tmp/task9b1-legacy-workspace")
    await database.close()
    engine = create_postgres_engine(dsn, 0, 1)
    try:
        async def migrate(revision: str, *, downgrade: bool = False) -> None:
            async with engine.connect() as conn:
                await conn.run_sync(lambda sync: _migrate(sync, revision, downgrade=downgrade))
                await conn.commit()

        await migrate(PRIOR, downgrade=True)
        await migrate(HEAD)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_schema)
        async with engine.connect() as conn:
            legacy = (
                await conn.execute(
                    text(
                        "SELECT branch, target_branch, target_kind, repair_workspace_path "
                        "FROM integration_candidate_resolutions WHERE id='legacy-resolution'"
                    )
                )
            ).one()
            assert legacy.target_branch == legacy.branch
            assert legacy.target_kind == "legacy_integration"
            assert legacy.repair_workspace_path == "/tmp/task9b1-legacy-workspace"
        await migrate(PRIOR, downgrade=True)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE integration_repair_stages SET deadline_at=NULL "
                    "WHERE operation_id='repair-batch-batch' AND ordinal=0"
                )
            )
        with pytest.raises(RuntimeError, match="irreconstructible legacy authority"):
            await migrate(REVISION)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE integration_repair_stages SET deadline_at=999 "
                    "WHERE operation_id='repair-batch-batch' AND ordinal=0"
                )
            )
        await migrate(HEAD)
        await migrate(PRIOR, downgrade=True)
        await migrate(HEAD)
        async with engine.connect() as conn:
            await conn.run_sync(_assert_schema)
        async with engine.begin() as conn:
            await conn.run_sync(_bind_live_handoff)
        with pytest.raises(RuntimeError, match="live candidate handoff provenance"):
            await migrate(REVISION, downgrade=True)
        async with engine.connect() as conn:
            handoff = (
                await conn.execute(
                    text(
                        "SELECT handoff_owner_id, handoff_fence_token "
                        "FROM integration_candidate_resolutions WHERE id='legacy-resolution'"
                    )
                )
            ).one()
            assert handoff == ("repair-batch-batch", 2)
    finally:
        await engine.dispose()
        _, _, scratch_name = dsn.rpartition("/")
        admin = await asyncpg.connect(
            POSTGRES_DSN.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
        finally:
            await admin.close()
