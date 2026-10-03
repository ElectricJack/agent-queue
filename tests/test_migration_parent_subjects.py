"""Additive episode binding migration on disposable PostgreSQL."""

from importlib import import_module

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.database.engine import create_postgres_engine
from tests.alembic_revisions import previous_revision
from tests.pg_dsn import create_scratch_database
from tests.test_migration_integration_subjects import _alembic

pytestmark = pytest.mark.migration
REVISION = "a00000000058"
ARTIFACT = "sha256:" + "1" * 64


async def seed(engine):
    async with engine.begin() as conn:
        for sql, params in (
            ("INSERT INTO projects (id,name,created_at) VALUES ('p','p',1)", {}),
            (
                ("INSERT INTO repos (id,project_id,url,source_type,checkout_base_path) "
                "VALUES ('repo','p','/repo','clone','/checkout')"),
                {},
            ),
            (
                (
                    "INSERT INTO playbook_artifacts (artifact_sha256,playbook_id,source_digest,"
                    "contract_fingerprint,compiler_build,path,created_at) "
                    "VALUES (:sha,'train',:sha,:sha,'fixture','/fixture',1)"
                ),
                {"sha": ARTIFACT},
            ),
            (
                (
                    "INSERT INTO integration_parent_episodes "
                    "(id,parent_task_id,repository_id,generation,pre_collection_checkpoint_sha,created_at) "
                    "VALUES ('episode','parent','repo',1,:head,1),('other','parent','repo',2,:head,2)"
                ),
                {"head": "a" * 40},
            ),
            (
                (
                    "INSERT INTO integration_subjects "
                    "(id,project_id,repository_id,kind,subject_key,task_id,phase,policy_playbook_id,"
                    "policy_artifact_sha256,due_set_at,next_due_at,max_wait_seconds,created_at,updated_at) "
                    "VALUES ('subject','p','repo','parent_episode','key','parent','building','train',"
                    ":sha,1,1,300,1,1)"
                ),
                {"sha": ARTIFACT},
            ),
        ):
            await conn.execute(text(sql), params)


async def assert_guards(engine):
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE integration_subjects SET parent_episode_id='episode'"))
    for statement in (
        "UPDATE integration_subjects SET parent_episode_id='other'",
        "UPDATE integration_subjects SET parent_episode_id=NULL",
        "DELETE FROM integration_parent_episodes WHERE id='episode'",
    ):
        with pytest.raises((DBAPIError, IntegrityError)):
            async with engine.begin() as conn:
                await conn.execute(text(statement))
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM integration_subjects"))).mappings().one()
    assert row["engine"] == "legacy" and row["head_sha"] is None and row["version"] == 0


@pytest.mark.parametrize("deployed", [True, False])
async def test_additive_parent_binding_upgrade_replay_and_immutable_identity(deployed):
    engine = create_postgres_engine(await create_scratch_database("parent-subject58"))
    try:
        await _alembic(engine, "upgrade", previous_revision(REVISION))
        if deployed:
            async with engine.begin() as conn:
                await conn.execute(
                    text("ALTER TABLE integration_subjects DROP COLUMN parent_episode_id")
                )
        await seed(engine)
        await _alembic(engine, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert (
                await conn.scalar(text("SELECT parent_episode_id FROM integration_subjects"))
                is None
            )
        revision = import_module("migrations.versions.a00000000058_parent_subject_episode")

        def replay(sync):
            from alembic.migration import MigrationContext
            from alembic.operations import Operations

            with Operations.context(MigrationContext.configure(sync)):
                revision.upgrade()
                revision.upgrade()
            inspector = inspect(sync)
            assert revision.COLUMN in {
                column["name"] for column in inspector.get_columns(revision.TABLE)
            }
            assert revision.FK in {fk["name"] for fk in inspector.get_foreign_keys(revision.TABLE)}

        async with engine.begin() as conn:
            await conn.run_sync(replay)
        # Repository qualification and the parent-only constraint apply even
        # before a binding is pinned.
        for statement in (
            "UPDATE integration_subjects SET parent_episode_id='missing'",
            "UPDATE integration_subjects SET repository_id='foreign',parent_episode_id='episode'",
        ):
            with pytest.raises((DBAPIError, IntegrityError)):
                async with engine.begin() as conn:
                    await conn.execute(text(statement))
        await assert_guards(engine)
        await _alembic(engine, "downgrade", previous_revision(REVISION))
        await _alembic(engine, "upgrade", REVISION)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM integration_subjects")) == 1
            assert await conn.scalar(text("SELECT count(*) FROM integration_parent_episodes")) == 2
        await assert_guards(engine)
    finally:
        await engine.dispose()
