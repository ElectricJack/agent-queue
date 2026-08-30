import os
import pytest
import yaml
from src.config import load_config, AppConfig


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


def test_default_runtime_default_value():
    cfg = AppConfig()
    assert cfg.default_runtime == ""  # session-routed; harness picks the CLI


def test_default_runtime_validation_accepts_empty():
    """Empty is the normal value: run as a session, harness picks the CLI."""
    cfg = AppConfig()
    cfg.default_runtime = ""
    errors = [e for e in cfg.validate() if e.field == "default_runtime"]
    assert errors == []


def test_default_runtime_validation_rejects_deleted_runtimes():
    """`claude_sdk` / `acpx` were removed; naming one must not silently pass."""
    for name in ("claude_sdk", "acpx"):
        cfg = AppConfig()
        cfg.default_runtime = name
        errors = [e for e in cfg.validate() if e.field == "default_runtime"]
        assert len(errors) == 1, f"{name} should be rejected"


def test_default_runtime_validation_rejects_unknown():
    cfg = AppConfig()
    cfg.default_runtime = "made-up"
    errors = [e for e in cfg.validate() if e.field == "default_runtime"]
    assert len(errors) == 1
    assert "unknown runtime" in errors[0].message.lower()


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
