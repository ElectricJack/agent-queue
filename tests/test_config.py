import os
import pytest
import yaml
from src.config import load_config


@pytest.fixture
def config_dir(tmp_path):
    return tmp_path


class TestConfigLoading:
    def test_load_minimal_config(self, config_dir):
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "discord": {
                        "bot_token": "test-token",
                        "guild_id": "123",
                    },
                    "database_path": str(config_dir / "test.db"),
                }
            )
        )
        config = load_config(str(config_file))
        assert config.discord.bot_token == "test-token"
        assert config.workspace_dir == os.path.expanduser("~/agent-queue-workspaces")

    def test_env_var_substitution(self, config_dir, monkeypatch):
        monkeypatch.setenv("TEST_BOT_TOKEN", "secret-token-123")
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "discord": {
                        "bot_token": "${TEST_BOT_TOKEN}",
                        "guild_id": "123",
                    },
                    "database_path": str(config_dir / "test.db"),
                }
            )
        )
        config = load_config(str(config_file))
        assert config.discord.bot_token == "secret-token-123"

    def test_defaults_applied(self, config_dir):
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump({
            "discord": {"bot_token": "x", "guild_id": "1"},
            "database_path": str(config_dir / "test.db"),
        }))
        config = load_config(str(config_file))
        assert config.scheduling.rolling_window_hours == 24
        assert config.scheduling.min_task_guarantee is True
        assert config.agents_config.heartbeat_interval_seconds == 30

    def test_custom_workspace_dir(self, config_dir):
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "workspace_dir": "/custom/path",
                    "discord": {"bot_token": "x", "guild_id": "1"},
                    "database_path": str(config_dir / "test.db"),
                }
            )
        )
        config = load_config(str(config_file))
        assert config.workspace_dir == "/custom/path"

    def test_missing_config_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")


def test_streams_config_defaults():
    from src.config import AppConfig

    cfg = AppConfig()
    assert cfg.streams.buffer_max_lines == 5000
    assert cfg.streams.buffer_max_bytes == 2 * 1024 * 1024
    assert cfg.streams.retention_seconds == 300
    assert cfg.streams.kill_grace_seconds == 5.0
    assert cfg.streams.max_concurrent_per_session == 3
    assert cfg.streams.client_reconnect_attempts == 5


def test_streams_config_validate_rejects_non_positive():
    from src.config import StreamsConfig

    cfg = StreamsConfig(buffer_max_lines=0, retention_seconds=-1, max_concurrent_per_session=0)
    errors = cfg.validate()
    fields = {e.field for e in errors}
    assert "buffer_max_lines" in fields
    assert "retention_seconds" in fields
    assert "max_concurrent_per_session" in fields


def test_streams_config_validate_accepts_defaults():
    from src.config import StreamsConfig

    assert StreamsConfig().validate() == []


# ---------------------------------------------------------------------------
# DatabaseConfig.backend — DSN scheme detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@localhost:5432/db",
        "postgres://u:p@localhost:5432/db",
        # SQLAlchemy's driver-qualified form.  This is what
        # ``create_postgres_engine`` normalizes *to*, what ``alembic.ini``
        # and ``POSTGRES_TEST_DSN`` carry, and what the swarm e2e kit writes
        # into its generated config.  Missing it sent the DSN down the
        # SQLite branch, where it was treated as a file path: the daemon
        # came up healthy on an empty SQLite database and ``src.main.run``
        # created a directory named after the DSN.
        "postgresql+asyncpg://u:p@localhost:5432/db",
        "postgresql+psycopg://u:p@localhost:5432/db",
        "postgresql+psycopg2://u:p@localhost:5432/db",
    ],
)
def test_database_backend_detects_every_postgres_scheme(url):
    from src.config import DatabaseConfig, is_postgres_url

    assert is_postgres_url(url) is True
    assert DatabaseConfig(url=url).backend == "postgresql"


@pytest.mark.parametrize(
    "url",
    ["", "~/.agent-queue/agent-queue.db", "/var/lib/aq/aq.db", "sqlite+aiosqlite:///x.db"],
)
def test_database_backend_treats_everything_else_as_sqlite(url):
    from src.config import DatabaseConfig, is_postgres_url

    assert is_postgres_url(url) is False
    assert DatabaseConfig(url=url).backend == "sqlite"


def test_asyncpg_dsn_is_pooled_like_any_other_postgres_url():
    """The pool bounds are only validated on the PostgreSQL branch."""
    from src.config import DatabaseConfig

    cfg = DatabaseConfig(url="postgresql+asyncpg://u:p@h/db", pool_min_size=0)
    assert [e.field for e in cfg.validate()] == ["pool_min_size"]


def test_psycopg2_dsn_is_rejected_at_load():
    """Recognized as PostgreSQL, but it cannot run this daemon.

    psycopg2 has no asyncio support, so ``create_async_engine`` rejects it —
    but only at first connect, deep in SQLAlchemy, with a message about the
    dialect not being async.  Config validation says it at load, with the
    fix in the text.
    """
    from src.config import DatabaseConfig

    cfg = DatabaseConfig(url="postgresql+psycopg2://u:p@h/db")
    errors = cfg.validate()
    assert [e.field for e in errors] == ["url"]
    assert "asyncpg" in errors[0].message


def test_psycopg3_dsn_is_accepted():
    """psycopg *3* is async-capable, unlike psycopg2 — it stays legal."""
    from src.config import DatabaseConfig

    assert DatabaseConfig(url="postgresql+psycopg://u:p@h/db").validate() == []


def test_graph_layout_config_defaults_and_parse(tmp_path):
    from src.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text(yaml.dump({
        "discord": {"bot_token": "t", "guild_id": "1"},
        "database_path": str(tmp_path / "test.db"),
        "dashboard": {"graph_layout": {"enabled": True, "incremental_debounce_ms": 250}},
    }))
    cfg = load_config(str(p))
    assert cfg.graph_layout.enabled is True
    assert cfg.graph_layout.incremental_debounce_ms == 250
    assert cfg.graph_layout.reconcile_interval_seconds == 900


def test_graph_layout_config_defaults_when_absent(tmp_path):
    from src.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text(yaml.dump({
        "discord": {"bot_token": "t", "guild_id": "1"},
        "database_path": str(tmp_path / "test.db"),
    }))
    cfg = load_config(str(p))
    assert cfg.graph_layout.enabled is False
    assert cfg.graph_layout.reconcile_interval_seconds == 900
    assert cfg.graph_layout.incremental_debounce_ms == 500
    assert cfg.graph_layout.tidy_job_budget_seconds == 60


def test_graph_layout_config_validate_rejects_negative():
    from src.config import GraphLayoutConfig

    errors = GraphLayoutConfig(reconcile_interval_seconds=-1).validate()
    assert [e.field for e in errors] == ["reconcile_interval_seconds"]

def test_graph_layout_config_reads_top_level_block(tmp_path):
    """``update_config``/``config_editor`` address sections by AppConfig
    field name and write them as TOP-LEVEL yaml keys, so the loader has to
    honour a top-level ``graph_layout:`` block or every runtime edit to this
    section is a silent no-op."""
    from src.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text(yaml.dump({
        "discord": {"bot_token": "t", "guild_id": "1"},
        "database_path": str(tmp_path / "test.db"),
        "graph_layout": {"enabled": True, "tidy_job_budget_seconds": 5},
    }))
    cfg = load_config(str(p))
    assert cfg.graph_layout.enabled is True
    assert cfg.graph_layout.tidy_job_budget_seconds == 5
    assert cfg.graph_layout.incremental_debounce_ms == 500


def test_graph_layout_nested_block_wins_over_top_level(tmp_path):
    from src.config import load_config

    p = tmp_path / "c.yaml"
    p.write_text(yaml.dump({
        "discord": {"bot_token": "t", "guild_id": "1"},
        "database_path": str(tmp_path / "test.db"),
        "dashboard": {"graph_layout": {"incremental_debounce_ms": 250}},
        "graph_layout": {"incremental_debounce_ms": 999},
    }))
    assert load_config(str(p)).graph_layout.incremental_debounce_ms == 250
