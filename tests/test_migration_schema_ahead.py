"""Startup accepts only evidenced additive descendants within rollback policy."""

import pytest
from sqlalchemy import text

from src.config import AppConfig, DatabaseConfig, load_config
from src.database import create_database
from src.database.engine import create_postgres_engine, run_schema_setup, verify_schema_current
from src.database.migration_guard import SchemaAheadCode, SchemaAheadPolicy
from src.database.schema_key import alembic_head_revisions
from tests.pg_dsn import create_scratch_database
from tests.test_additive_migrations import BASE, revision


@pytest.fixture
def evidence(tmp_path):
    code = tmp_path / "code"
    newer = tmp_path / "newer"
    code.mkdir()
    newer.mkdir()
    (code / "a.py").write_text(BASE["migrations/versions/a.py"])
    (newer / "a.py").write_text(BASE["migrations/versions/a.py"])
    (newer / "b.py").write_text(revision())
    return code, newer


def test_default_has_no_distance_limit_but_does_not_trust_orphans(evidence):
    code, newer = evidence
    assert not SchemaAheadPolicy().accepts(("unknown",), code)
    (newer / "c.py").write_text(revision("c", "b"))
    assert SchemaAheadPolicy(str(newer)).accepts(("c",), code)
    assert not SchemaAheadPolicy(str(newer)).accepts(("a",), code)


def test_limit_accepts_boundary_and_refuses_beyond_it_with_remedy(evidence):
    code, newer = evidence
    assert SchemaAheadPolicy(str(newer), 1).accepts(("b",), code)
    (newer / "c.py").write_text(revision("c", "b"))
    with pytest.raises(SchemaAheadCode, match="2 revisions ahead; policy permits 1") as exc:
        SchemaAheadPolicy(str(newer), 1).accepts(("c",), code)
    assert "Deploy code matching" in str(exc.value)
    assert "Do not downgrade or stamp" in str(exc.value)


@pytest.mark.parametrize(
    "body",
    [
        "op.drop_table('tasks')",
        "op.rename_table('tasks', 'jobs')",
        "op.alter_column('tasks', 'title', type_=sa.String(1))",
    ],
)
def test_breaking_ahead_refused_even_without_limit(evidence, body):
    code, newer = evidence
    (newer / "b.py").write_text(revision(body=body))
    with pytest.raises(SchemaAheadCode, match="non-additive"):
        SchemaAheadPolicy(str(newer)).accepts(("b",), code)


def test_evidence_must_preserve_history_and_prove_ancestry(evidence):
    code, newer = evidence
    (newer / "a.py").write_text(revision("a", None, body="op.drop_table('tasks')"))
    with pytest.raises(SchemaAheadCode, match="changed code revision"):
        SchemaAheadPolicy(str(newer)).accepts(("b",), code)
    (newer / "a.py").write_text(BASE["migrations/versions/a.py"])
    with pytest.raises(SchemaAheadCode, match="unknown revision"):
        SchemaAheadPolicy(str(newer)).accepts(("orphan",), code)
    (newer / "b.py").write_text(revision(parent=None))
    with pytest.raises(SchemaAheadCode, match="heads"):
        SchemaAheadPolicy(str(newer)).accepts(("b",), code)


@pytest.mark.parametrize("value", [-1, True, "1", 1.5])
def test_invalid_limits_rejected(value):
    with pytest.raises(ValueError, match="nonnegative integer"):
        SchemaAheadPolicy(max_revisions=value)
    assert DatabaseConfig(schema_ahead_max_revisions=value).validate()


def test_config_reaches_daemon_database_factory(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "discord:\n  bot_token: fixture\n  guild_id: '1'\n"
        "database:\n  url: postgresql://u:p@localhost/scratch\n"
        "  schema_ahead_migrations: /retained/versions\n"
        "  schema_ahead_max_revisions: 1\n"
    )
    config = load_config(str(config_file))
    database = create_database(config)
    assert database._schema_ahead_policy == SchemaAheadPolicy("/retained/versions", 1)
    assert AppConfig().database.schema_ahead_max_revisions is None


@pytest.mark.parametrize("verify_only", [False, True])
async def test_real_startup_preserves_ahead_stamp_and_never_upgrades(tmp_path, verify_only):
    import shutil

    from src.database.schema_key import PROJECT_ROOT

    newer = tmp_path / "versions"
    shutil.copytree(PROJECT_ROOT / "migrations" / "versions", newer)
    head = alembic_head_revisions()[0]
    (newer / "fixture_ahead.py").write_text(revision("fixture_ahead", head))
    engine = create_postgres_engine(await create_scratch_database("schema_ahead"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            await connection.execute(text("INSERT INTO alembic_version VALUES ('fixture_ahead')"))
            await connection.execute(text("CREATE TABLE preserved (id text PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO preserved VALUES ('keep')"))
        startup = verify_schema_current if verify_only else run_schema_setup
        await startup(engine, schema_ahead_policy=SchemaAheadPolicy(str(newer), 1))
        with pytest.raises(SchemaAheadCode, match="policy permits 0"):
            await startup(engine, schema_ahead_policy=SchemaAheadPolicy(str(newer), 0))
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT * FROM preserved"))).all() == [("keep",)]
            assert (await connection.execute(text("SELECT * FROM alembic_version"))).all() == [
                ("fixture_ahead",)
            ]
    finally:
        await engine.dispose()


@pytest.mark.parametrize("current", [("b", "c"), ("a", "b")])
def test_known_multiple_heads_take_ordinary_behind_path(evidence, current):
    code, newer = evidence
    (code / "b.py").write_text(revision("b", "a"))
    (code / "c.py").write_text(revision("c", "a"))
    (code / "merge.py").write_text(revision("merge", ("b", "c")))
    assert not SchemaAheadPolicy(str(newer), 0).accepts(current, code)


def test_unknown_multiple_heads_are_still_refused(evidence):
    code, newer = evidence
    with pytest.raises(SchemaAheadCode, match="multiple or missing heads"):
        SchemaAheadPolicy(str(newer)).accepts(("a", "unknown"), code)


@pytest.mark.parametrize("limit,accepted", [(1, True), (0, False)])
async def test_plugin_client_uses_retained_schema_policy(tmp_path, monkeypatch, limit, accepted):
    import shutil
    from src.cli import client as cli_client
    from src.database.schema_key import PROJECT_ROOT

    newer = tmp_path / "versions"
    shutil.copytree(PROJECT_ROOT / "migrations" / "versions", newer)
    (newer / "fixture.py").write_text(revision("plugin_ahead", alembic_head_revisions()[0]))
    url = await create_scratch_database("plugin_ahead")
    engine = create_postgres_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            await conn.execute(text("INSERT INTO alembic_version VALUES ('plugin_ahead')"))
        monkeypatch.setattr(
            cli_client,
            "_resolve_db_config",
            lambda: dict(schema_ahead_migrations=str(newer), schema_ahead_max_revisions=limit),
        )
        client = cli_client.PluginClient(db_path=url)
        # This exercises the real startup guard, without the subsequent data
        # migrations which require a fully seeded application database.
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        async def initialize(self):
            await run_schema_setup(engine, schema_ahead_policy=self._schema_ahead_policy)

        monkeypatch.setattr(PostgreSQLDatabaseAdapter, "initialize", initialize)
        try:
            if accepted:
                await client.connect()
            else:
                with pytest.raises(SchemaAheadCode, match="policy permits 0"):
                    await client.connect()
            async with engine.connect() as conn:
                assert (
                    await conn.scalar(text("SELECT version_num FROM alembic_version"))
                    == "plugin_ahead"
                )
        finally:
            await client.close()
    finally:
        await engine.dispose()
