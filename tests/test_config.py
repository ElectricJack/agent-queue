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
    assert cfg.default_runtime == "claude_sdk"


def test_default_runtime_validation_accepts_known():
    for name in ("claude_sdk", "claude_cli", "codex_cli"):
        cfg = AppConfig()
        cfg.default_runtime = name
        errors = [e for e in cfg.validate() if e.field == "default_runtime"]
        assert errors == [], f"{name} flagged as invalid"


def test_default_runtime_validation_rejects_unknown():
    cfg = AppConfig()
    cfg.default_runtime = "made-up"
    errors = [e for e in cfg.validate() if e.field == "default_runtime"]
    assert len(errors) == 1
    assert "unknown platform" in errors[0].message.lower()
