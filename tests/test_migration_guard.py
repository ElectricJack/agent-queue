"""Only the daemon may migrate the production database.

The regression these cover, in one sentence: on 2026-09-02 a worker session
in a worktree slot ran Alembic against the URL in ``~/.agent-queue/config.yaml``
and stamped it with an unmerged branch's revision, after which the daemon
refused to boot.  See ``docs/guides/migrations.md``.
"""

from __future__ import annotations

import pytest
import yaml

from src.database.engine import create_sqlite_engine, run_schema_setup
from src.database.migration_guard import (
    CLI,
    DAEMON,
    MIGRATE,
    OPERATOR,
    TEST,
    VERIFY,
    WORKER,
    MigrationRefused,
    SchemaBehindCode,
    assert_not_production_database,
    current_scope,
    is_production_database,
    migration_decision,
    normalize_database_url,
    process_scope,
    production_database_url,
    same_database,
    set_process_scope,
)


@pytest.fixture
def production_config(tmp_path, monkeypatch):
    """A config.yaml naming *its own* SQLite file as the production database."""
    db_path = tmp_path / "production.db"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"database": {"url": str(db_path)}}), encoding="utf-8")
    monkeypatch.setenv("AQ_CONFIG_PATH", str(config_path))
    return db_path


@pytest.fixture(autouse=True)
def _clean_scope(monkeypatch):
    """Every test starts from "nothing declared, nothing in the env"."""
    monkeypatch.delenv("AQ_DB_SCOPE", raising=False)
    monkeypatch.delenv("AQ_SESSION_ID", raising=False)
    previous = set_process_scope(None)
    yield
    set_process_scope(previous)


# ---------------------------------------------------------------------------
# Who is asking
# ---------------------------------------------------------------------------


class TestCurrentScope:
    def test_defaults_to_test_under_pytest(self):
        assert current_scope() == TEST

    def test_session_marker_means_worker(self, monkeypatch):
        monkeypatch.setenv("AQ_SESSION_ID", "abc")
        assert current_scope() == WORKER

    def test_declared_process_scope_is_used(self):
        with process_scope(DAEMON):
            assert current_scope() == DAEMON
        assert current_scope() == TEST

    def test_env_beats_a_declared_daemon_scope(self, monkeypatch):
        """An ``aq start`` inside a worktree slot is still a worker.

        This is incident vector 3: the daemon declares itself the daemon, but
        the session it was launched from says otherwise, and the session wins.
        """
        monkeypatch.setenv("AQ_DB_SCOPE", WORKER)
        with process_scope(DAEMON):
            assert current_scope() == WORKER

    def test_unknown_env_value_is_ignored(self, monkeypatch):
        monkeypatch.setenv("AQ_DB_SCOPE", "wizard")
        assert current_scope() == TEST

    def test_set_process_scope_rejects_nonsense(self):
        with pytest.raises(ValueError, match="unknown database scope"):
            set_process_scope("wizard")


# ---------------------------------------------------------------------------
# What they point at
# ---------------------------------------------------------------------------


class TestUrlIdentity:
    def test_postgres_ignores_driver_credentials_and_query(self):
        assert same_database(
            "postgresql+asyncpg://user:secret@127.0.0.1:5533/agent_queue?sslmode=require",
            "postgresql://other@localhost:5533/agent_queue",
        )

    def test_postgres_different_database_names_differ(self):
        assert not same_database(
            "postgresql://localhost:5533/agent_queue",
            "postgresql://localhost:5533/agent_queue_gw0",
        )

    def test_postgres_default_port_is_explicit(self):
        assert (
            normalize_database_url("postgresql://localhost/aq") == "postgresql://localhost:5432/aq"
        )

    def test_sqlite_paths_are_resolved(self, tmp_path):
        target = tmp_path / "aq.db"
        assert same_database(str(target), f"{tmp_path}/./aq.db")

    def test_sqlite_url_form_matches_bare_path(self, tmp_path):
        target = tmp_path / "aq.db"
        assert same_database(f"sqlite:///{target}", str(target))

    def test_sqlite_three_slash_form_is_relative(self):
        """``sqlite:///a/b`` is relative and ``sqlite:////a/b`` absolute."""
        assert normalize_database_url("sqlite:////tmp/aq.db") == "/tmp/aq.db"
        assert normalize_database_url("sqlite:///tmp/aq.db").endswith("/tmp/aq.db")
        assert normalize_database_url("sqlite:///tmp/aq.db") != "/tmp/aq.db"

    def test_async_driver_url_matches_the_configured_path(self, tmp_path):
        """``str(engine.url)`` is what the guard actually gets handed."""
        target = tmp_path / "aq.db"
        assert same_database(f"sqlite+aiosqlite:///{target}", str(target))

    def test_memory_and_empty_never_match(self):
        assert not same_database(":memory:", ":memory:")
        assert not same_database("", "")


class TestProductionUrl:
    def test_read_from_config(self, production_config):
        assert same_database(production_database_url(), str(production_config))
        assert is_production_database(str(production_config))

    def test_env_overrides_cannot_redefine_production(self, production_config, monkeypatch):
        """The scratch URL a worker is handed must not become "production"."""
        monkeypatch.setenv("AGENT_QUEUE_DB", "/tmp/scratch.db")
        monkeypatch.setenv("AQ_DATABASE_URL", "/tmp/scratch.db")
        assert same_database(production_database_url(), str(production_config))
        assert not is_production_database("/tmp/scratch.db")

    def test_missing_config_protects_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AQ_CONFIG_PATH", str(tmp_path / "absent.yaml"))
        assert production_database_url() == ""
        assert not is_production_database(str(tmp_path / "anything.db"))

    def test_unparseable_config_protects_nothing(self, monkeypatch, tmp_path):
        broken = tmp_path / "config.yaml"
        broken.write_text("database: [unclosed\n", encoding="utf-8")
        monkeypatch.setenv("AQ_CONFIG_PATH", str(broken))
        assert production_database_url() == ""

    def test_legacy_database_path_key(self, monkeypatch, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text(yaml.safe_dump({"database_path": str(tmp_path / "legacy.db")}))
        monkeypatch.setenv("AQ_CONFIG_PATH", str(config))
        assert is_production_database(str(tmp_path / "legacy.db"))


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


class TestMigrationDecision:
    @pytest.mark.parametrize("scope", [DAEMON, OPERATOR, WORKER, CLI, TEST])
    def test_scratch_databases_always_migrate(self, production_config, tmp_path, scope):
        assert migration_decision(str(tmp_path / "scratch.db"), scope=scope) == MIGRATE

    @pytest.mark.parametrize("scope", [DAEMON, OPERATOR])
    def test_daemon_and_operator_may_migrate_production(self, production_config, scope):
        assert migration_decision(str(production_config), scope=scope) == MIGRATE

    @pytest.mark.parametrize("scope", [WORKER, CLI, TEST])
    def test_everyone_else_only_verifies_production(self, production_config, scope):
        assert migration_decision(str(production_config), scope=scope) == VERIFY

    def test_scope_is_resolved_when_not_passed(self, production_config, monkeypatch):
        monkeypatch.setenv("AQ_DB_SCOPE", WORKER)
        assert migration_decision(str(production_config)) == VERIFY
        monkeypatch.setenv("AQ_DB_SCOPE", DAEMON)
        assert migration_decision(str(production_config)) == MIGRATE


# ---------------------------------------------------------------------------
# The engine honours it
# ---------------------------------------------------------------------------


async def _stamped(db_path) -> list[str]:
    import sqlite3

    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path)) as conn:
        try:
            return sorted(row[0] for row in conn.execute("SELECT version_num FROM alembic_version"))
        except sqlite3.Error:
            return []


class TestRunSchemaSetup:
    async def test_worker_refuses_to_create_the_production_schema(
        self, production_config, monkeypatch
    ):
        monkeypatch.setenv("AQ_DB_SCOPE", WORKER)
        engine = create_sqlite_engine(str(production_config))
        try:
            with pytest.raises(SchemaBehindCode, match="[Ss]chema behind code"):
                await run_schema_setup(engine)
        finally:
            await engine.dispose()
        assert await _stamped(production_config) == []

    async def test_refusal_names_the_operator_action(self, production_config, monkeypatch):
        monkeypatch.setenv("AQ_DB_SCOPE", WORKER)
        engine = create_sqlite_engine(str(production_config))
        try:
            with pytest.raises(SchemaBehindCode) as excinfo:
                await run_schema_setup(engine)
        finally:
            await engine.dispose()
        message = str(excinfo.value)
        assert "aq db upgrade" in message
        assert "AQ_DB_SCOPE=worker" in message

    async def test_daemon_migrates_then_worker_passes_verification(
        self, production_config, monkeypatch
    ):
        monkeypatch.setenv("AQ_DB_SCOPE", DAEMON)
        engine = create_sqlite_engine(str(production_config))
        try:
            await run_schema_setup(engine)
        finally:
            await engine.dispose()
        assert await _stamped(production_config) != []

        # The same worker call that refused above is now a silent no-op:
        # a worker may read a production database that is at head.
        monkeypatch.setenv("AQ_DB_SCOPE", WORKER)
        engine = create_sqlite_engine(str(production_config))
        try:
            await run_schema_setup(engine)
        finally:
            await engine.dispose()

    async def test_worker_still_migrates_its_own_scratch_database(
        self, production_config, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AQ_DB_SCOPE", WORKER)
        scratch = tmp_path / "scratch.db"
        engine = create_sqlite_engine(str(scratch))
        try:
            await run_schema_setup(engine)
        finally:
            await engine.dispose()
        assert await _stamped(scratch) != []

    async def test_worker_sees_the_unknown_revision_diagnostic(
        self, production_config, monkeypatch
    ):
        """An orphaned row is reported as such, not as "behind"."""
        import sqlite3

        monkeypatch.setenv("AQ_DB_SCOPE", DAEMON)
        engine = create_sqlite_engine(str(production_config))
        try:
            await run_schema_setup(engine)
        finally:
            await engine.dispose()
        with sqlite3.connect(str(production_config)) as conn:
            conn.execute("UPDATE alembic_version SET version_num = 'f2a4c6e8b0d2'")

        monkeypatch.setenv("AQ_DB_SCOPE", WORKER)
        engine = create_sqlite_engine(str(production_config))
        try:
            with pytest.raises(RuntimeError, match="unknown revision"):
                await run_schema_setup(engine)
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# The outer fence the test suite itself sits behind
# ---------------------------------------------------------------------------


class TestConftestRefusal:
    def test_refuses_the_production_url(self, production_config):
        with pytest.raises(MigrationRefused, match="production database"):
            assert_not_production_database(str(production_config), actor="pytest (X)")

    def test_allows_anything_else(self, production_config, tmp_path):
        assert_not_production_database(str(tmp_path / "scratch.db"), actor="pytest (X)")
        assert_not_production_database(None, actor="pytest (X)")

    def test_the_real_conftest_guard_runs_clean(self):
        """The guard this suite is collected behind is itself exercised."""
        from tests.conftest import _refuse_production_database

        _refuse_production_database()


# ---------------------------------------------------------------------------
# What a worker session's own db tooling resolves to
# ---------------------------------------------------------------------------


class TestWorkerSessionEnvironment:
    def test_isolation_block_points_at_a_per_slot_scratch_file(self):
        from src.sessions.env import session_db_isolation

        env = session_db_isolation("/slots/slot-3")
        assert env["AQ_DB_SCOPE"] == WORKER
        assert env["AQ_DATABASE_URL"] == "/slots/slot-3/.aq/scratch.db"
        assert env["AGENT_QUEUE_DB"] == env["AQ_DATABASE_URL"]

    def test_a_session_without_a_work_dir_still_gets_the_scope(self):
        from src.sessions.env import session_db_isolation

        assert session_db_isolation("") == {"AQ_DB_SCOPE": WORKER}

    def test_cli_resolves_the_scratch_url(self, monkeypatch, tmp_path):
        from src.cli.client import _resolve_db_url

        monkeypatch.delenv("AGENT_QUEUE_DB", raising=False)
        monkeypatch.setenv("AQ_DATABASE_URL", str(tmp_path / "scratch.db"))
        assert _resolve_db_url() == str(tmp_path / "scratch.db")

    def test_legacy_name_still_wins(self, monkeypatch, tmp_path):
        from src.cli.client import _resolve_db_url

        monkeypatch.setenv("AGENT_QUEUE_DB", "/legacy.db")
        monkeypatch.setenv("AQ_DATABASE_URL", str(tmp_path / "scratch.db"))
        assert _resolve_db_url() == "/legacy.db"

    async def test_plugin_client_explains_the_worker_scratch_database(self, monkeypatch, tmp_path):
        from src.cli.client import PluginClient

        monkeypatch.setenv("AQ_DB_SCOPE", WORKER)
        client = PluginClient(db_path=str(tmp_path / ".aq" / "scratch.db"))
        with pytest.raises(FileNotFoundError, match="AQ_DB_SCOPE=worker"):
            await client.connect()
