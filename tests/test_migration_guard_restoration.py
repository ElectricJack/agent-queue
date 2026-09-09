"""Guard repair covers both the original squash and the legacy stamp bridge."""

import asyncpg
import pytest
from sqlalchemy import text

from migrations.integration_guards import TRIGGERS, install_integration_guards
from migrations.versions.a00000000001_squashed_baseline import LEGACY_HEAD
from src.database.engine import create_postgres_engine, run_schema_setup
from src.database.schema_key import alembic_head_revisions
from src.database.tables import metadata
from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.migration, pytest.mark.integration]
POSTGRES_DSN = ensure_worker_postgres_dsn()


@pytest.mark.parametrize("initial_revision", ["a00000000001", LEGACY_HEAD])
async def test_existing_database_receives_guard_repair(initial_revision):
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")

    dsn = await create_scratch_database(f"guard_restore_{initial_revision}")
    engine = create_postgres_engine(dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            if initial_revision == LEGACY_HEAD:
                # A fully migrated pre-squash database already had these guards.
                await conn.run_sync(install_integration_guards)
            await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            await conn.execute(
                text("INSERT INTO alembic_version VALUES (:revision)"),
                {"revision": initial_revision},
            )

        raw = await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))
        try:
            await raw.execute(
                "INSERT INTO integration_review_evidence "
                "(id, source_task_id, repository_id, source_base, reviewed_head_sha, "
                "reviewed_tree_sha, reviewer_task_id, review_kind, generation, verdict, "
                "evidence, created_at) VALUES "
                "('keep', 'task', 'repo', $1, $2, $3, 'review', 'leaf', 0, "
                "'approved', '{}'::json, 1)",
                "a" * 40,
                "b" * 40,
                "c" * 40,
            )
            if initial_revision == "a00000000001":
                assert (
                    await raw.fetchval("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal")
                    == 0
                )
                # Prove the broken baseline permits changing durable evidence.
                await raw.execute("UPDATE integration_review_evidence SET created_at=2")

            await run_schema_setup(engine)

            assert await raw.fetchval("SELECT version_num FROM alembic_version") == (
                alembic_head_revisions()[0]
            )
            installed = {
                (row["tgname"], row["relname"])
                for row in await raw.fetch(
                    "SELECT tgname, relname FROM pg_trigger JOIN pg_class c ON c.oid=tgrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE NOT tgisinternal AND n.nspname='public'"
                )
            }
            assert installed == {(name, table) for name, table, _ in TRIGGERS}
            for statement in (
                "UPDATE integration_review_evidence SET verdict='rejected' WHERE id='keep'",
                "DELETE FROM integration_review_evidence WHERE id='keep'",
            ):
                with pytest.raises(asyncpg.PostgresError, match="append-only"):
                    await raw.execute(statement)
            assert (
                await raw.fetchval(
                    "SELECT verdict FROM integration_review_evidence WHERE id='keep'"
                )
                == "approved"
            )
        finally:
            await raw.close()
    finally:
        await engine.dispose()


async def test_unstamped_legacy_database_is_refused_without_changing_data():
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")

    dsn = await create_scratch_database("guard_restore_unstamped")
    engine = create_postgres_engine(dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(
                text("INSERT INTO projects (id, name, created_at) VALUES ('keep', 'Keep', 1)")
            )

        with pytest.raises(RuntimeError, match="pre-squash head.*previous release"):
            await run_schema_setup(engine)

        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT id, name FROM projects"))).all() == [
                ("keep", "Keep")
            ]
            assert (
                await conn.execute(text("SELECT to_regclass('public.alembic_version')"))
            ).scalar_one() is None
            assert (
                await conn.execute(text("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"))
            ).scalar_one() == 0
    finally:
        await engine.dispose()
