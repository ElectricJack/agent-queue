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
        assert config.messages.enabled is True

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
        "dashboard": {"graph_layout": {"enabled": False, "incremental_debounce_ms": 250}},
    }))
    cfg = load_config(str(p))
    # The block wins over the on-by-default field.
    assert cfg.graph_layout.enabled is False
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
    # On by default since design §10 step 3 removed the dashboard's grid fallback.
    assert cfg.graph_layout.enabled is True
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
        "graph_layout": {"enabled": False, "tidy_job_budget_seconds": 5},
    }))
    cfg = load_config(str(p))
    assert cfg.graph_layout.enabled is False
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


# ---------------------------------------------------------------------------
# PlaybooksConfig.cancellation_grace_seconds — Playbook V2 Package 4 §9
# ---------------------------------------------------------------------------


def test_playbooks_config_rejects_a_negative_cancellation_grace():
    from src.config import PlaybooksConfig

    errors = PlaybooksConfig(cancellation_grace_seconds=-1).validate()
    assert [e.field for e in errors] == ["cancellation_grace_seconds"]


def test_playbooks_config_accepts_a_zero_cancellation_grace():
    """Zero is "do not wait", not a misconfiguration.

    An operator who wants a cancellation to land immediately — accepting that
    the receipt will say ``grace_expired`` — has to be able to say so, so the
    bound is ``>= 0`` rather than ``> 0`` like the size limits above it.
    """
    from src.config import PlaybooksConfig

    assert PlaybooksConfig(cancellation_grace_seconds=0).validate() == []
    assert PlaybooksConfig().cancellation_grace_seconds == 30


def test_chat_analyzer_section_is_gone_from_appconfig_and_every_registry():
    """prime-torrent-81: the section had three fields and no consumer.

    ``ChatAnalyzerConfig`` declared ``min_confidence``,
    ``in_flight_min_confidence`` and ``dismiss_cooldown_seconds`` for the
    post-``observe()`` suggester gate stack.  The messaging-rework M0 strip
    removed the suggester (see ``docs/specs/design/feature-pauses.md``), so
    nothing read them; grand-glacier-97 made the keys *reachable* without
    deciding their fate, and this task deleted them.

    Deleting a dataclass is easy to do halfway: a stale name left in
    ``HOT_RELOADABLE_SECTIONS``, ``_SECTION_FIELDS`` or ``reload_non_critical``
    would surface only later — as an ``AttributeError`` on the next hot
    reload, or as a section the config editor still offers.  Assert every
    registry at once.
    """
    import dataclasses

    from src import config as config_module
    from src.config import (
        HOT_RELOADABLE_SECTIONS,
        RESTART_REQUIRED_SECTIONS,
        AppConfig,
    )

    assert not hasattr(config_module, "ChatAnalyzerConfig")
    assert "chat_analyzer" not in {f.name for f in dataclasses.fields(AppConfig)}
    assert "chat_analyzer" not in HOT_RELOADABLE_SECTIONS
    assert "chat_analyzer" not in RESTART_REQUIRED_SECTIONS
    assert "chat_analyzer" not in config_module._SECTION_FIELDS


def test_a_leftover_chat_analyzer_block_still_loads(config_dir):
    """Operator impact of the deletion is nil.

    ``load_config`` ignores sections it does not recognise, so a config file
    that still carries the retired block loads exactly like one that does
    not — no error, no warning, and no attribute to read it back from.  This
    is what makes the removal safe to ship without an operator migration.
    """
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "discord": {"bot_token": "t", "guild_id": "1"},
                "database_path": str(config_dir / "test.db"),
                "chat_analyzer": {
                    "min_confidence": 0.25,
                    "in_flight_min_confidence": 0.95,
                    "dismiss_cooldown_seconds": 0,
                },
            }
        )
    )
    config = load_config(str(config_file))

    assert not hasattr(config, "chat_analyzer")
    assert config.discord.bot_token == "t"
    assert config.validate() == []


def test_hot_reload_round_trip_does_not_touch_the_retired_section(config_dir):
    """``reload_non_critical`` is where a half-finished deletion would bite.

    ``diff_configs`` reads ``_SECTION_FIELDS`` with ``dict.get``, so a stale
    entry there is merely inert.  ``reload_non_critical`` is not: it assigns
    section by section (``updated.<name> = fresh.<name>``), so a leftover
    ``updated.chat_analyzer = fresh.chat_analyzer`` raises ``AttributeError``
    the first time an operator edits their config — at runtime, in the daemon,
    not at import.  Drive a real reload over a file that still carries the
    retired block.
    """
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "discord": {"bot_token": "t", "guild_id": "1"},
                "database_path": str(config_dir / "test.db"),
                "chat_analyzer": {"min_confidence": 0.25},
                "scheduling": {"min_task_guarantee": 3},
            }
        )
    )
    config = load_config(str(config_file))
    assert config.scheduling.min_task_guarantee == 3

    config_file.write_text(
        yaml.dump(
            {
                "discord": {"bot_token": "t", "guild_id": "1"},
                "database_path": str(config_dir / "test.db"),
                "chat_analyzer": {"min_confidence": 0.25},
                "scheduling": {"min_task_guarantee": 9},
            }
        )
    )
    reloaded = config.reload_non_critical()

    assert reloaded.scheduling.min_task_guarantee == 9
    assert not hasattr(reloaded, "chat_analyzer")
