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


async def test_promotion_flow_upgrade_preserves_projects_and_uses_nullable_jsonb():
    """Exercise an old install as well as the live-metadata baseline path."""
    dsn = await create_scratch_database("promotionflow81")
    before = _alembic_pg(dsn, "upgrade", "a00000000080")
    assert before.returncode == 0, before.stderr
    conn = await _pg_conn(dsn)
    try:
        # The squashed baseline uses today's metadata. Reproduce revision
        # 080 as actually deployed, with no promotion_flow column.
        await conn.execute("ALTER TABLE projects DROP COLUMN promotion_flow")
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('keep','Keep',0)")
    finally:
        await conn.close()

    upgraded = _alembic_pg(dsn, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        column = await conn.fetchrow(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name='projects' AND column_name='promotion_flow'"
        )
        assert dict(column) == {"data_type": "jsonb", "is_nullable": "YES", "column_default": None}
        assert await conn.fetchval("SELECT promotion_flow FROM projects WHERE id='keep'") is None
        await conn.execute("UPDATE projects SET promotion_flow='[]'::jsonb WHERE id='keep'")
        assert await conn.fetchval("SELECT promotion_flow FROM projects WHERE id='keep'") == "[]"
        assert await conn.fetchval("SELECT name FROM projects WHERE id='keep'") == "Keep"
    finally:
        await conn.close()


async def test_agent_cron_upgrade_adds_global_schedule_storage_without_losing_projects():
    dsn = await create_scratch_database("agentcron85")
    before = _alembic_pg(dsn, "upgrade", "a00000000085")
    assert before.returncode == 0, before.stderr
    conn = await _pg_conn(dsn)
    try:
        # Live-metadata baseline already has the table. Reproduce the old install.
        await conn.execute("DROP TABLE agent_cron")
        await conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('keep','Keep',0)")
    finally:
        await conn.close()
    upgraded = _alembic_pg(dsn, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT name FROM projects WHERE id='keep'") == "Keep"
        assert await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='agent_cron' AND column_name='recurrence'"
        ) == "jsonb"
        await conn.execute(
            "INSERT INTO agent_cron (id, session_id, session_instance_token, idempotency_key, "
            "prompt, recurrence, created_at, next_fire_at) "
            "VALUES ('cron', 'global', 'instance', 'patrol', 'Prompt', '{}', 0, 900)"
        )
        row = await conn.fetchrow("SELECT * FROM agent_cron WHERE id='cron'")
        assert row["project_id"] is None
        assert row["state"] == "active" and row["delivery_attempts"] == row["tick_count"] == 0
    finally:
        await conn.close()
    again = _alembic_pg(dsn, "upgrade", "head")
    assert again.returncode == 0, again.stderr


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


async def test_continuous_repair_upgrade_from_two_stage_schema():
    dsn = await create_scratch_database("continuous48")
    before = _alembic_pg(dsn, "upgrade", "a00000000047")
    assert before.returncode == 0, before.stderr
    conn = await _pg_conn(dsn)
    try:
        # Simulate an existing installation, not the live-metadata baseline.
        await conn.execute("DROP TABLE integration_source_ci")
        await conn.execute("ALTER TABLE integration_repair_stages DROP CONSTRAINT ck_integration_repair_stages_ordinal")
        await conn.execute("ALTER TABLE integration_repair_stages ADD CONSTRAINT ck_integration_repair_stages_ordinal CHECK (ordinal IN (0, 1))")
    finally:
        await conn.close()
    upgraded = _alembic_pg(dsn, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        constraint = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_integration_repair_stages_ordinal'")
        assert "ordinal >= 0" in constraint
        assert await conn.fetchval("SELECT data_type FROM information_schema.columns WHERE table_name='integration_source_ci' AND column_name='evidence'") == "jsonb"
        assert await conn.fetchval("SELECT column_default FROM information_schema.columns WHERE table_name='integration_source_ci' AND column_name='repair_attempt'") == "0"
    finally:
        await conn.close()


_STAGE_CHECKS = {
    "ck_integration_candidate_resolutions_stage": ("integration_candidate_resolutions", "stage_ordinal"),
    "ck_integration_candidate_ref_mutations_stage": (
        "integration_candidate_ref_mutations",
        "operation_stage",
    ),
}


async def _stage_check_definitions(conn) -> dict[str, str]:
    rows = await conn.fetch(
        "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conname = ANY($1::text[])",
        list(_STAGE_CHECKS),
    )
    return {row["conname"]: row["definition"] for row in rows}


async def _insert_stage_rows(conn, stage: int, suffix: str) -> None:
    """One resolution (for member *stage*) and one mutation at *stage*.

    Only CHECK constraints are under test: ``session_replication_role =
    replica`` suspends the FK and guard triggers (not CHECK constraints), so
    the rows need no parent graph.
    """
    oid = "a" * 40
    await conn.execute("SET session_replication_role = replica")
    try:
        await conn.execute(
            "INSERT INTO integration_candidate_resolutions (id, batch_id, revision, "
            "member_ordinal, operation_id, operation_episode_id, stage_ordinal, "
            "stage_deadline_at, project_id, repair_task_id, repair_session_id, "
            "repair_session_instance_token, repair_workspace_id, repair_workspace_path, "
            "repository_id, branch, target_branch, target_kind, fence_owner_id, fence_token, "
            "partial_head_sha, source_base_sha, source_head_sha, resolved_head_sha, "
            "resolved_tree_sha, repair_commit_shas, state, created_at, updated_at) VALUES "
            "($1, 'batch', 0, $2, 'op', 'batch', $2, 1.0, 'p', 'repair', 'session', 'token', "
            "'workspace', '/w', 'repo', 'refs/heads/b', 'refs/heads/t', 'qualified', "
            "'repair', 1, $3, $3, $3, $3, $3, '[]', 'reserved', 1.0, 1.0)",
            f"resolution-{suffix}", stage, oid,
        )
        await conn.execute(
            "INSERT INTO integration_candidate_ref_mutations (id, batch_id, revision, purpose, "
            "repository_id, branch, target_branch, expected_old_sha, desired_sha, "
            "operation_id, operation_episode_id, operation_stage, lease_owner_id, "
            "lease_fence_token, branch_owner_id, branch_owner_role, branch_fence_token, "
            "nonce, state, expires_at, created_at, updated_at) VALUES "
            "($1, 'batch', 0, 'root_main', 'repo', 'refs/heads/b', 'refs/heads/main', $3, $3, "
            "'op', 'batch', $2, 'lease', 1, 'op', 'collector', 1, 'nonce', 'reserved', "
            "1.0, 1.0, 1.0)",
            f"mutation-{suffix}", stage, oid,
        )
    finally:
        await conn.execute("SET session_replication_role = DEFAULT")


async def test_later_repair_stage_constraints_upgrade_from_two_stage_schema():
    """a00000000050 admits stage 2+ resolutions/mutations and still rejects negatives."""
    import asyncpg

    dsn = await create_scratch_database("stages50")
    before = _alembic_pg(dsn, "upgrade", "a00000000048")
    assert before.returncode == 0, before.stderr
    conn = await _pg_conn(dsn)
    try:
        # Simulate the deployed installation, not the live-metadata baseline.
        for name, (table, column) in _STAGE_CHECKS.items():
            await conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
            await conn.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN (0, 1))"
            )
        await _insert_stage_rows(conn, 1, "stage-1")
        with pytest.raises(asyncpg.CheckViolationError) as refused:
            await _insert_stage_rows(conn, 2, "stage-2-before")
        assert refused.value.constraint_name == "ck_integration_candidate_resolutions_stage"
    finally:
        await conn.close()

    upgraded = _alembic_pg(dsn, "upgrade", "a00000000050")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        definitions = await _stage_check_definitions(conn)
        assert definitions == {
            name: f"CHECK (({column} >= 0))" for name, (_table, column) in _STAGE_CHECKS.items()
        }
        await _insert_stage_rows(conn, 2, "stage-2")
        await _insert_stage_rows(conn, 7, "stage-7")
        for table, column in _STAGE_CHECKS.values():
            with pytest.raises(asyncpg.CheckViolationError) as negative:
                await conn.execute("SET session_replication_role = replica")
                try:
                    await conn.execute(f"UPDATE {table} SET {column} = -1 WHERE {column} = 1")
                finally:
                    await conn.execute("SET session_replication_role = DEFAULT")
            assert negative.value.constraint_name in _STAGE_CHECKS
        # Rows recorded before the upgrade are untouched.
        assert await conn.fetchval(
            "SELECT stage_ordinal FROM integration_candidate_resolutions WHERE id = 'resolution-stage-1'"
        ) == 1
    finally:
        await conn.close()

    # Retained stage 2+ evidence cannot fit the old schema: downgrade refuses.
    refused_downgrade = _alembic_pg(dsn, "downgrade", "a00000000048")
    assert refused_downgrade.returncode != 0
    assert "repair stage above 1" in refused_downgrade.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "a00000000050"
        assert set((await _stage_check_definitions(conn)).values()) == {
            "CHECK ((stage_ordinal >= 0))", "CHECK ((operation_stage >= 0))"
        }
        await conn.execute("SET session_replication_role = replica")
        await conn.execute("DELETE FROM integration_candidate_resolutions WHERE stage_ordinal > 1")
        await conn.execute("DELETE FROM integration_candidate_ref_mutations WHERE operation_stage > 1")
        await conn.execute("SET session_replication_role = DEFAULT")
    finally:
        await conn.close()

    downgraded = _alembic_pg(dsn, "downgrade", "a00000000048")
    assert downgraded.returncode == 0, downgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "a00000000048"
        definitions = await _stage_check_definitions(conn)
        assert "ARRAY[0, 1]" in definitions["ck_integration_candidate_resolutions_stage"]
        assert "ARRAY[0, 1]" in definitions["ck_integration_candidate_ref_mutations_stage"]
    finally:
        await conn.close()

    # Re-upgrade, then re-run the revision over an already-relaxed schema.
    for args in (("upgrade", "a00000000050"), ("stamp", "a00000000048"), ("upgrade", "a00000000050")):
        rerun = _alembic_pg(dsn, *args)
        assert rerun.returncode == 0, rerun.stderr
    conn = await _pg_conn(dsn)
    try:
        assert set((await _stage_check_definitions(conn)).values()) == {
            "CHECK ((stage_ordinal >= 0))", "CHECK ((operation_stage >= 0))"
        }
    finally:
        await conn.close()


_SIBLING_STANDIN = '''"""Stand-in for the live candidate's repair-ejection revision (test only).

Revision ID: a00000000049
Revises: a00000000048
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a00000000049"
down_revision = "a00000000048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = sa.inspect(op.get_bind()).get_columns("integration_candidate_revisions")
    if "source_manifest" not in {column["name"] for column in columns}:
        op.add_column(
            "integration_candidate_revisions", sa.Column("source_manifest", JSONB())
        )


def downgrade() -> None:
    op.drop_column("integration_candidate_revisions", "source_manifest")
'''

_MERGE_STANDIN = '''"""Join the repair-ejection and later-repair-stage revisions (test only).

Revision ID: a00000000051
Revises: a00000000049, a00000000050
"""

revision = "a00000000051"
down_revision = ("a00000000049", "a00000000050")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
'''


def _combined_script_directory(tmp_path) -> str:
    """The migration tree as it looks once a00000000049 and a00000000050 both land.

    Until the live candidate's ``a00000000049`` reaches this tree, a stand-in
    with its parent and its first schema effect, plus the merge revision the
    spec prescribes, are added to a private copy.  Afterwards the real tree is
    used unchanged, so the same assertions guard the real merge.
    """
    import shutil

    copy = tmp_path / "migrations"
    shutil.copytree(
        os.path.join(ROOT, "migrations"), copy, ignore=shutil.ignore_patterns("__pycache__")
    )
    versions = copy / "versions"
    if not list(versions.glob("a00000000049_*.py")):
        (versions / "a00000000049_sibling_standin.py").write_text(_SIBLING_STANDIN)
        (versions / "a00000000051_join_standin.py").write_text(_MERGE_STANDIN)
    ini = tmp_path / "alembic.ini"
    with open(os.path.join(ROOT, "alembic.ini")) as handle:
        original = handle.read()
    ini.write_text(
        original.replace("script_location = %(here)s/migrations", f"script_location = {copy}")
        .replace("prepend_sys_path = .", f"prepend_sys_path = {ROOT}")
    )
    return str(ini)


def _alembic_combined(ini: str, dsn: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ini, *args],
        cwd=ROOT,
        env=dict(os.environ, AGENT_QUEUE_DB_URL=_async_dsn(dsn)),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("deployed_first", ["a00000000050", "a00000000049"])
async def test_deployed_050_and_sibling_049_join_through_a_merge_revision(
    tmp_path, deployed_first
):
    """Either deploy order reaches one head with both revisions applied.

    The operator database is stamped ``a00000000050`` before the live
    candidate's ``a00000000049`` lands (the reverse order is covered too).
    A merge revision makes ``upgrade head`` apply the missing sibling; a
    re-chain of ``a00000000050`` onto ``a00000000049`` would instead make a
    database stamped ``a00000000050`` skip ``a00000000049`` forever.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = _combined_script_directory(tmp_path)
    script = ScriptDirectory.from_config(Config(ini))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    assert script.get_revision("a00000000050").down_revision == "a00000000048"
    assert script.get_revision("a00000000049").down_revision == "a00000000048"
    ancestors = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert {"a00000000049", "a00000000050"} <= ancestors

    dsn = await create_scratch_database("join4950")
    first = _alembic_combined(ini, dsn, "upgrade", deployed_first)
    assert first.returncode == 0, first.stderr
    conn = await _pg_conn(dsn)
    try:
        # The baseline is built from current metadata, so remove the effect of
        # the revision that has not run yet: only that revision can restore it.
        if deployed_first == "a00000000050":
            await conn.execute(
                "ALTER TABLE integration_candidate_revisions DROP COLUMN IF EXISTS source_manifest"
            )
        else:
            for name, (table, column) in _STAGE_CHECKS.items():
                await conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
                await conn.execute(
                    f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN (0, 1))"
                )
    finally:
        await conn.close()
    joined = _alembic_combined(ini, dsn, "upgrade", "head")
    assert joined.returncode == 0, joined.stderr
    conn = await _pg_conn(dsn)
    try:
        assert [row["version_num"] for row in await conn.fetch(
            "SELECT version_num FROM alembic_version"
        )] == [heads[0]]
        assert set((await _stage_check_definitions(conn)).values()) == {
            "CHECK ((stage_ordinal >= 0))", "CHECK ((operation_stage >= 0))"
        }
        assert await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'integration_candidate_revisions' "
            "AND column_name = 'source_manifest'"
        ) == "jsonb"
    finally:
        await conn.close()


_JOIN_REVISION = "a00000000051"
_EJECTION_TRIGGERS = {
    ("integration_result_current_member", "integration_candidate_member_results"),
    ("integration_revision_manifest_immutable", "integration_candidate_revisions"),
}


async def _alembic_versions(conn) -> list[str]:
    return [row["version_num"] for row in await conn.fetch("SELECT version_num FROM alembic_version")]


async def _ejection_triggers(conn) -> set[tuple[str, str]]:
    return {
        (row["tgname"], row["relname"])
        for row in await conn.fetch(
            "SELECT tgname, relname FROM pg_trigger JOIN pg_class c ON c.oid=tgrelid "
            "WHERE NOT tgisinternal AND tgname = ANY($1::text[])",
            [name for name, _table in _EJECTION_TRIGGERS],
        )
    }


async def _member_result_fk(conn):
    return await conn.fetchval(
        "SELECT conname FROM pg_constraint "
        "WHERE conname = 'fk_integration_candidate_member_results_member'"
    )


async def _seed_unmanifested_candidate(conn) -> None:
    """Two members, one candidate revision and a result, as the live DB holds them.

    ``session_replication_role = replica`` suspends FK and guard triggers so
    the rows need no project, batch or task graph; only the shape that
    a00000000049 backfills and guards is under test.
    """
    await conn.execute("SET session_replication_role = replica")
    try:
        for ordinal, task_id in ((1, "second"), (0, "first")):
            await conn.execute(
                "INSERT INTO integration_batch_members (batch_id, ordinal, task_id, "
                "repository_id, source_base_sha, reviewed_head_sha, reviewed_tree_sha, "
                "review_evidence_id, review_evidence) VALUES ('deployed-batch', $1, $2, "
                "'repo', $3, $3, $3, $4, '{}')",
                ordinal, task_id, str(ordinal) * 40, f"evidence-{task_id}",
            )
        await conn.execute(
            "INSERT INTO integration_candidate_revisions (batch_id, revision, "
            "construction_base_sha, next_member_ordinal, state, created_at, updated_at) "
            "VALUES ('deployed-batch', 0, $1, 1, 'red', 1.0, 1.0)",
            "b" * 40,
        )
        await conn.execute(
            "INSERT INTO integration_candidate_member_results (batch_id, revision, "
            "member_ordinal, input_head_sha, input_tree_sha, generated_squash_sha, result, "
            "created_at, updated_at) VALUES ('deployed-batch', 0, 0, $1, $1, $1, "
            "'applied', 1.0, 1.0)",
            "0" * 40,
        )
    finally:
        await conn.execute("SET session_replication_role = DEFAULT")


async def test_join_revision_applies_repair_ejection_to_a_database_stamped_050():
    """The live DB took a00000000050 first; the join must still run a00000000049.

    Re-chaining 050 onto 049 would mark 049 applied here and skip its backfill,
    FK drop and guards (2026-10-01 join design).
    """
    import json

    import asyncpg

    dsn = await create_scratch_database("join51from50")
    before = _alembic_pg(dsn, "upgrade", "a00000000050")
    assert before.returncode == 0, before.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await _alembic_versions(conn) == ["a00000000050"]
        # Simulate the deployed installation: the baseline is built from live
        # metadata, which already carries 049's column and lacks its FK.
        await conn.execute("ALTER TABLE integration_candidate_revisions DROP COLUMN source_manifest")
        await conn.execute(
            "ALTER TABLE integration_candidate_member_results ADD CONSTRAINT "
            "fk_integration_candidate_member_results_member FOREIGN KEY "
            "(batch_id, member_ordinal) REFERENCES integration_batch_members (batch_id, ordinal)"
        )
        assert await _ejection_triggers(conn) == set()
        await _seed_unmanifested_candidate(conn)
        # Deployed behaviour: stage 2 evidence is already being recorded.
        await _insert_stage_rows(conn, 2, "deployed-stage-2")
    finally:
        await conn.close()

    upgraded = _alembic_pg(dsn, "upgrade", _JOIN_REVISION)
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await _alembic_versions(conn) == [_JOIN_REVISION]
        manifest = json.loads(
            await conn.fetchval(
                "SELECT source_manifest FROM integration_candidate_revisions "
                "WHERE batch_id = 'deployed-batch' AND revision = 0"
            )
        )
        assert [(member["ordinal"], member["task_id"]) for member in manifest] == [
            (0, "first"),
            (1, "second"),
        ]
        assert await _member_result_fk(conn) is None
        assert await _ejection_triggers(conn) == _EJECTION_TRIGGERS
        assert "source_manifest" in await conn.fetchval(
            "SELECT prosrc FROM pg_proc WHERE proname = 'integration_eject_authorized'"
        )
        with pytest.raises(asyncpg.PostgresError, match="source manifest is immutable"):
            await conn.execute(
                "UPDATE integration_candidate_revisions SET source_manifest = '[]'::jsonb "
                "WHERE batch_id = 'deployed-batch'"
            )
        # 050's relaxed checks and the stage 2 rows recorded under them survive.
        assert set((await _stage_check_definitions(conn)).values()) == {
            "CHECK ((stage_ordinal >= 0))", "CHECK ((operation_stage >= 0))"
        }
        assert await conn.fetchval(
            "SELECT stage_ordinal FROM integration_candidate_resolutions "
            "WHERE id = 'resolution-deployed-stage-2'"
        ) == 2
        await _assert_integration_guards(conn)
    finally:
        await conn.close()

    # The join is a single head: a rerun is a no-op.
    rerun = _alembic_pg(dsn, "upgrade", _JOIN_REVISION)
    assert rerun.returncode == 0, rerun.stderr


async def test_join_revision_applies_later_stage_checks_to_a_database_stamped_049():
    """A database that took the main line first receives a00000000050 at the join."""
    import asyncpg

    dsn = await create_scratch_database("join51from49")
    before = _alembic_pg(dsn, "upgrade", "a00000000049")
    assert before.returncode == 0, before.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await _alembic_versions(conn) == ["a00000000049"]
        # Simulate an installation whose checks predate a00000000050.
        for name, (table, column) in _STAGE_CHECKS.items():
            await conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
            await conn.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN (0, 1))"
            )
        await _insert_stage_rows(conn, 1, "stage-1")
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_stage_rows(conn, 2, "stage-2-before")
        assert await _ejection_triggers(conn) == _EJECTION_TRIGGERS
    finally:
        await conn.close()

    upgraded = _alembic_pg(dsn, "upgrade", _JOIN_REVISION)
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        assert await _alembic_versions(conn) == [_JOIN_REVISION]
        assert set((await _stage_check_definitions(conn)).values()) == {
            "CHECK ((stage_ordinal >= 0))", "CHECK ((operation_stage >= 0))"
        }
        await _insert_stage_rows(conn, 2, "stage-2")
        assert await conn.fetchval(
            "SELECT stage_ordinal FROM integration_candidate_resolutions "
            "WHERE id = 'resolution-stage-1'"
        ) == 1
        # 049's ejection shape is retained across the join.
        assert await _member_result_fk(conn) is None
        assert await _ejection_triggers(conn) == _EJECTION_TRIGGERS
        await _assert_integration_guards(conn)
    finally:
        await conn.close()


async def test_benchmark_attribution_upgrade_from_revision_46():
    """Revision 47 adds attribution after inbox, object loops and attachments."""
    dsn = await create_scratch_database("benchmark47")
    before = _alembic_pg(dsn, "upgrade", "a00000000046")
    assert before.returncode == 0, before.stderr
    conn = await _pg_conn(dsn)
    try:
        await conn.execute("DROP TABLE benchmark_stage_spans")
        await conn.execute("ALTER TABLE archived_tasks DROP COLUMN route")
        for name in ("session_id", "attempt_id", "call_id", "model_source"):
            await conn.execute(f"ALTER TABLE token_ledger DROP COLUMN {name}")
    finally:
        await conn.close()
    upgraded = _alembic_pg(dsn, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        columns = {row["column_name"] for row in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='token_ledger'"
        )}
        assert {"session_id", "attempt_id", "call_id", "model_source"} <= columns
        assert await conn.fetchval("SELECT to_regclass('benchmark_stage_spans')")
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name='archived_tasks' AND column_name='route'"
        ) == 1
        head = _alembic_pg(dsn, "heads")
        assert head.returncode == 0, head.stderr
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == head.stdout.split()[0]
        for table in ("pull_request_inbox_snapshot", "object_loops", "doc_review_attachments"):
            assert await conn.fetchval("SELECT to_regclass($1)", table)
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


async def test_ref_lease_upgrade_preserves_legacy_shadow_owner_and_constraints():
    """Stage 2 adds fields on an old install without rewriting live ownership."""
    dsn = await create_scratch_database("reflease72")
    before = _alembic_pg(dsn, "upgrade", "a00000000071")
    assert before.returncode == 0, before.stderr
    conn = await _pg_conn(dsn)
    try:
        # The baseline uses live metadata; reproduce the actual prior shape.
        await conn.execute(
            "ALTER TABLE integration_branch_owners DROP CONSTRAINT ck_integration_branch_owners_lease_binding"
        )
        await conn.execute(
            "ALTER TABLE integration_branch_owners DROP CONSTRAINT ck_integration_branch_owners_lease_fence"
        )
        await conn.execute(
            "ALTER TABLE integration_branch_owners DROP COLUMN holder, DROP COLUMN fence"
        )
        await conn.execute(
            "INSERT INTO integration_branch_owners "
            "(id, repository_id, ref, owner_id, owner_role, fence_token, handoff_state, "
            "session_id, workspace_id, confirmed_workspace_id, expires_at, created_at, updated_at) "
            "VALUES ('legacy', 'r', 'aq/epic', 'worker', 'repair', 41, 'handoff_pending', "
            "'session', 'workspace', 'confirmed', NULL, 1, 2)"
        )
        original = dict(
            await conn.fetchrow("SELECT * FROM integration_branch_owners WHERE id='legacy'")
        )
        constraints = set(
            await conn.fetch(
                "SELECT conname FROM pg_constraint WHERE conrelid='integration_branch_owners'::regclass"
            )
        )
    finally:
        await conn.close()
    upgraded = _alembic_pg(dsn, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    conn = await _pg_conn(dsn)
    try:
        row = dict(await conn.fetchrow("SELECT * FROM integration_branch_owners WHERE id='legacy'"))
        assert row.pop("holder") is None
        assert row.pop("fence") is None
        assert row == original
        installed = set(
            await conn.fetch(
                "SELECT conname FROM pg_constraint WHERE conrelid='integration_branch_owners'::regclass"
            )
        )
        assert constraints <= installed
    finally:
        await conn.close()
    # Fresh baseline already contains the new columns/constraints: upgrade is
    # inspect-guarded and idempotent for that path too (the parity test above).
