# tests/test_migration_postgres_upgrade_head.py
"""Fresh PostgreSQL baseline creation, usable defaults, and schema parity.

The pre-squash revision paths are retired. These tests run the supported
baseline from an empty scratch database, independently of the template cache.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from tests.pg_dsn import create_scratch_database, ensure_worker_postgres_dsn

pytestmark = [pytest.mark.migration, pytest.mark.integration]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTGRES_DSN = ensure_worker_postgres_dsn()


async def _assert_integration_guards(conn):
    from migrations.integration_guards import FUNCTIONS, TRIGGERS

    expected_functions = {
        re.search(r"FUNCTION\s+(\w+)\(", statement).group(1) for statement in FUNCTIONS
    }
    installed_functions = {
        row["proname"]
        for row in await conn.fetch(
            "SELECT proname FROM pg_proc JOIN pg_namespace n ON n.oid=pronamespace "
            "WHERE n.nspname='public'"
        )
    }
    installed_triggers = {
        (row["tgname"], row["relname"])
        for row in await conn.fetch(
            "SELECT tgname, relname FROM pg_trigger JOIN pg_class c ON c.oid=tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE NOT tgisinternal AND n.nspname='public'"
        )
    }
    assert expected_functions <= installed_functions
    assert {(name, table) for name, table, _ in TRIGGERS} <= installed_triggers


def _alembic_pg(dsn: str, *args: str) -> subprocess.CompletedProcess:
    # Alembic's async env uses SQLAlchemy's async dialect.  Scratch DSNs are
    # intentionally plain PostgreSQL URLs so asyncpg can administer them.
    alembic_dsn = _async_dsn(dsn)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=alembic_dsn),
        capture_output=True,
        text=True,
        check=False,
    )


def _async_dsn(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


async def _pg_conn(dsn: str):
    import asyncpg

    return await asyncpg.connect(dsn.replace("postgresql+asyncpg://", "postgresql://"))


async def test_upgrade_head_applies_the_baseline_on_postgres():
    """Empty database -> head, on real PostgreSQL, with no manual repair."""
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")

    dsn = await create_scratch_database("uphead")
    res = _alembic_pg(dsn, "upgrade", "head")
    assert res.returncode == 0, res.stderr

    heads = _alembic_pg(dsn, "heads")
    assert heads.returncode == 0, heads.stderr
    head_revision = heads.stdout.split()[0]

    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == head_revision
        assert await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'agent_profiles' "
            "AND column_name = 'codex_service_tier'"
        ) == "YES"
        assert "rejected" in await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'doc_reviews'::regclass AND conname = 'ck_doc_reviews_state'"
        )
        await _assert_integration_guards(conn)
    finally:
        await conn.close()


@pytest.mark.parametrize("tier_already_present", [False, True])
async def test_service_tier_upgrade_preserves_revision_24(tier_already_present):
    """Keep review rejection at 24; apply 25 with or without the tier column.

    The squashed baseline includes current metadata, so remove the tier column
    to reproduce a deployed review-state database. Also cover a database that
    already received the tier DDL while the two files shared revision 24.
    """
    dsn = await create_scratch_database("revision24")
    before = _alembic_pg(dsn, "upgrade", "a00000000024")
    assert before.returncode == 0, before.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "a00000000024"
        await conn.execute(
            "INSERT INTO doc_reviews "
            "(id, project_id, kind, title, vault_path, state, created_at, updated_at) "
            "VALUES ('keep', 'p', 'spec', 'Keep', 'specs/keep.md', 'rejected', 0, 0)"
        )
        await conn.execute(
            "INSERT INTO agent_profiles (id, name, codex_service_tier, created_at, updated_at) "
            "VALUES ('keep', 'Keep', 'fast', 0, 0)"
        )
        if not tier_already_present:
            await conn.execute("ALTER TABLE agent_profiles DROP COLUMN codex_service_tier")
    finally:
        await conn.close()

    for _ in range(2):  # A repeat upgrade must leave both schema and data intact.
        upgraded = _alembic_pg(dsn, "upgrade", "a00000000025")
        assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "a00000000025"
        assert await conn.fetchval("SELECT state FROM doc_reviews WHERE id = 'keep'") == "rejected"
        assert await conn.fetchval(
            "SELECT codex_service_tier FROM agent_profiles WHERE id = 'keep'"
        ) == ("fast" if tier_already_present else None)
    finally:
        await conn.close()


async def test_upgrade_preserves_review_revision_before_adding_codex_service_tier():
    """The two formerly colliding revisions both run on a pre-change database."""
    dsn = await create_scratch_database("reviewcodexchain")
    before = _alembic_pg(dsn, "upgrade", "a00000000023")
    assert before.returncode == 0, before.stderr
    conn = await _pg_conn(dsn)
    try:
        # The squashed baseline uses today's metadata. Recreate the old schema
        # so neither incremental revision can silently skip its actual DDL.
        await conn.execute("ALTER TABLE agent_profiles DROP COLUMN codex_service_tier")
        await conn.execute("ALTER TABLE doc_reviews DROP CONSTRAINT ck_doc_reviews_state")
        await conn.execute(
            "ALTER TABLE doc_reviews ADD CONSTRAINT ck_doc_reviews_state "
            "CHECK (state IN ('in_review', 'changes_requested', 'approved', 'withdrawn'))"
        )
        await conn.execute(
            "INSERT INTO agent_profiles (id, name, created_at, updated_at) "
            "VALUES ('keep', 'Keep', 0, 0)"
        )
    finally:
        await conn.close()

    review = _alembic_pg(dsn, "upgrade", "a00000000024")
    assert review.returncode == 0, review.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "a00000000024"
        constraint = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_doc_reviews_state'"
        )
        assert "'rejected'" in constraint
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'agent_profiles' "
            "AND column_name = 'codex_service_tier')"
        )
    finally:
        await conn.close()

    upgraded = _alembic_pg(dsn, "upgrade", "a00000000025")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "a00000000025"
        row = await conn.fetchrow(
            "SELECT name, codex_service_tier FROM agent_profiles WHERE id = 'keep'"
        )
        assert dict(row) == {"name": "Keep", "codex_service_tier": None}
        await conn.execute(
            "UPDATE agent_profiles SET codex_service_tier = 'fast' WHERE id = 'keep'"
        )
        assert await conn.fetchval(
            "SELECT codex_service_tier FROM agent_profiles WHERE id = 'keep'"
        ) == "fast"
    finally:
        await conn.close()


async def test_resolution_push_fence_upgrade_marks_legacy_reservation_unknown():
    """A pre-marker reservation is not proof that no external push started."""
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")

    dsn = await create_scratch_database("resolutionfencelegacy")
    before = _alembic_pg(dsn, "upgrade", "a00000000004")
    assert before.returncode == 0, before.stderr
    conn = await _pg_conn(dsn)
    try:
        from importlib import import_module

        migration = import_module(
            "migrations.versions.a00000000006_legacy_resolution_recovery_evidence"
        )
        # Reproduce the deployed constraint, not the current metadata that
        # a fresh squashed baseline happens to install.
        await conn.execute(
            "ALTER TABLE integration_promotion_intents DROP CONSTRAINT "
            "ck_integration_promotion_intents_resolution_binding"
        )
        await conn.execute(
            "ALTER TABLE integration_promotion_intents ADD CONSTRAINT "
            "ck_integration_promotion_intents_resolution_binding CHECK ("
            + migration._PREVIOUS_BINDING + ")"
        )
        # This is deliberately a complete frozen reservation, not a malformed
        # fixture: it represents receipt-71755ac2-3bca-5bfe-8221-11fd243860fc
        # before a00000000005 had an opportunity to record a pre-push marker.
        await conn.execute(
            """
            INSERT INTO integration_promotion_intents (
                id, domain_key, receipt_id, source_head, source_base,
                repository_id, target_branch, expected_target,
                fence_owner_id, fence_token, state,
                resolution_head_sha, resolution_tree_sha, resolution_commit_shas,
                resolution_operation_id, resolution_stage_ordinal, resolution_task_id,
                resolution_session_id, resolution_session_instance_token,
                resolution_workspace_id, resolution_fence_owner_id,
                resolution_fence_token, created_at, updated_at, intent_kind
            ) VALUES (
                'receipt-71755ac2-3bca-5bfe-8221-11fd243860fc', 'legacy-resolution-domain',
                'receipt-71755ac2-3bca-5bfe-8221-11fd243860fc',
                repeat('b', 40), repeat('a', 40), 'repo', 'aq/parent', repeat('c', 40),
                'operation', 11, 'resolution_reserved',
                repeat('e', 40), repeat('f', 40), json_build_array(repeat('e', 40)),
                'operation', 1, 'repair-81d0aaee-0c3a-482c-b04c-d3afe6631cbe-1',
                'session690a3a54-5016-49c5-b981-be3a9e25a523', 'legacy-instance',
                'ws-upper-dock', 'repair-81d0aaee-0c3a-482c-b04c-d3afe6631cbe-1',
                11, 10.0, 10.0, 'child'
            )
            """
        )
        assert await conn.fetchval(
            "SELECT resolution_push_started_at FROM integration_promotion_intents "
            "WHERE id = 'receipt-71755ac2-3bca-5bfe-8221-11fd243860fc'"
        ) is None
    finally:
        await conn.close()

    upgraded = _alembic_pg(dsn, "upgrade", "a00000000006")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        row = await conn.fetchrow(
            "SELECT state, resolution_push_started_at, resolution_recovery_evidence, "
            "resolution_head_sha, "
            "resolution_fence_token FROM integration_promotion_intents "
            "WHERE id = 'receipt-71755ac2-3bca-5bfe-8221-11fd243860fc'"
        )
        constraint = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_integration_promotion_intents_resolution_binding'"
        )
        assert "resolution_push_started_at IS NULL" in constraint
        assert "resolution_recovery_evidence IS NULL" in constraint
        assert dict(row) == {
            "state": "resolution_reserved",
            "resolution_push_started_at": 0.0,
            "resolution_recovery_evidence": None,
            "resolution_head_sha": "e" * 40,
            "resolution_fence_token": 11,
        }
    finally:
        await conn.close()


async def test_boolean_columns_added_by_migrations_default_correctly_on_postgres():
    """The migrated schema is usable: boolean server defaults actually apply.

    A ``BOOLEAN DEFAULT 0`` column is rejected outright by Postgres, so
    this both re-proves the upgrade and pins the semantics of the
    defaults it installs (``false``, not "some integer that happens to
    be falsy on SQLite").
    """
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")

    dsn = await create_scratch_database("upheadbool")
    res = _alembic_pg(dsn, "upgrade", "head")
    assert res.returncode == 0, res.stderr

    conn = await _pg_conn(dsn)
    try:
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('p','P',0)")
        await conn.execute(
            "INSERT INTO tasks (id, project_id, title, description, status, "
            "created_at, updated_at) VALUES ('t','p','T','T','READY',0,0)"
        )
        # Every column the default is meant to cover is omitted here.
        await conn.execute(
            "INSERT INTO sessions (id, task_id, project_id, profile_id, harness, "
            "provider, name, lifecycle, work_dir, epoch, instance_token, started_at) "
            "VALUES ('s','t','p','prof','claude','tmux','n-s','task','/w','e','tok',0)"
        )
        assert (await conn.fetchval("SELECT hooks_provisioned FROM sessions WHERE id='s'")) is False
    finally:
        await conn.close()


async def test_autogenerate_against_a_fresh_head_database_is_empty():
    """``tables.py`` and the migrated schema agree — no autogenerate drift.

    The documented workflow for a schema change is "edit ``tables.py``,
    then ``alembic revision --autogenerate``".  That only works if a
    database at head already matches the metadata exactly: any standing
    drift is silently folded into the *next* developer's revision.  One
    such drift shipped for real — ``task_dependencies``' self-dependency
    guard was declared *unnamed* in ``tables.py``, which makes it
    invisible to autogenerate's comparison, so every run wanted to drop
    the ``task_dependencies_check`` it found in the live schema.  A later
    merge adopted the migration that creates ``agent_profiles.overlay_config``
    without adopting its metadata declaration, so autogenerate then wanted
    to remove that live column.

    This runs the same comparison ``alembic revision --autogenerate``
    runs (same ``compare_type`` opt as ``migrations/env.py``) and
    demands it produce nothing.
    """
    if not POSTGRES_DSN:
        pytest.skip("POSTGRES_TEST_DSN not set")
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.database.tables import metadata

    dsn = await create_scratch_database("updrift")
    res = _alembic_pg(dsn, "upgrade", "head")
    assert res.returncode == 0, res.stderr

    def _diff(sync_conn) -> list:
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(ctx, metadata)

    engine = create_async_engine(_async_dsn(dsn))
    try:
        async with engine.connect() as conn:
            diffs = await conn.run_sync(_diff)
    finally:
        await engine.dispose()

    assert diffs == [], (
        "alembic autogenerate is not empty against a database at head — "
        "src/database/tables.py has drifted from the migration chain:\n"
        + "\n".join(f"  {d}" for d in diffs)
    )


async def test_baseline_downgrade_is_refused_without_losing_data():
    dsn = await create_scratch_database("baselineguard")
    upgraded = _alembic_pg(dsn, "upgrade", "a00000000001")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('keep','Keep',0)")
        original_revision = await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()

    refused = _alembic_pg(dsn, "downgrade", "base")
    assert refused.returncode != 0
    assert "the squashed baseline has no downgrade" in refused.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT name FROM projects WHERE id='keep'") == "Keep"
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == original_revision
    finally:
        await conn.close()


async def test_upgrade_repairs_guards_on_original_squashed_database():
    from migrations.integration_guards import TRIGGERS

    dsn = await create_scratch_database("repairguards")
    upgraded = _alembic_pg(dsn, "upgrade", "a00000000001")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        # Reproduce the first baseline release: tables existed without triggers.
        for name, table, _ in TRIGGERS:
            await conn.execute(f'DROP TRIGGER "{name}" ON "{table}"')
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('keep','Keep',0)")
    finally:
        await conn.close()
    upgraded = _alembic_pg(dsn, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        await _assert_integration_guards(conn)
        assert await conn.fetchval("SELECT name FROM projects WHERE id='keep'") == "Keep"
    finally:
        await conn.close()
