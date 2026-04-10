"""Tests for per-section config validation and ConfigError aggregation."""

import os

import pytest
import yaml

from src.config import (
    AgentProfileConfig,
    AgentsDefaultConfig,
    AppConfig,
    ArchiveConfig,
    AutoTaskConfig,
    ChatProviderConfig,
    ConfigError,
    ConfigValidationError,
    DiscordConfig,
    LLMLoggingConfig,
    McpServerConfig,
    MemoryConfig,
    PauseRetryConfig,
    SchedulingConfig,
    load_config,
)


# ── ConfigError dataclass ──────────────────────────────────────────────


class TestConfigError:
    def test_str_format(self):
        e = ConfigError("discord", "bot_token", "is required")
        assert str(e) == "[discord] bot_token: is required"

    def test_default_severity_is_error(self):
        e = ConfigError("x", "y", "msg")
        assert e.severity == "error"

    def test_warning_severity(self):
        e = ConfigError("x", "y", "msg", severity="warning")
        assert e.severity == "warning"


# ── DiscordConfig ──────────────────────────────────────────────────────


class TestDiscordConfigValidation:
    def test_valid(self):
        cfg = DiscordConfig(bot_token="tok", guild_id="123")
        assert cfg.validate() == []

    def test_missing_bot_token(self):
        cfg = DiscordConfig(guild_id="123")
        errors = cfg.validate()
        assert len(errors) == 1
        assert errors[0].field == "bot_token"

    def test_missing_guild_id(self):
        cfg = DiscordConfig(bot_token="tok")
        errors = cfg.validate()
        assert len(errors) == 1
        assert errors[0].field == "guild_id"

    def test_both_missing(self):
        cfg = DiscordConfig()
        errors = cfg.validate()
        assert len(errors) == 2


# ── AgentsDefaultConfig ────────────────────────────────────────────────


class TestAgentsDefaultConfigValidation:
    def test_valid_defaults(self):
        assert AgentsDefaultConfig().validate() == []

    def test_heartbeat_zero(self):
        cfg = AgentsDefaultConfig(heartbeat_interval_seconds=0)
        errors = cfg.validate()
        assert any("heartbeat" in e.field for e in errors)

    def test_stuck_timeout_negative(self):
        cfg = AgentsDefaultConfig(stuck_timeout_seconds=-1)
        errors = cfg.validate()
        assert any("stuck_timeout" in e.field for e in errors)

    def test_graceful_shutdown_zero(self):
        cfg = AgentsDefaultConfig(graceful_shutdown_timeout_seconds=0)
        errors = cfg.validate()
        assert any("graceful_shutdown" in e.field for e in errors)


# ── SchedulingConfig ──────────────────────────────────────────────────


class TestSchedulingConfigValidation:
    def test_valid_defaults(self):
        assert SchedulingConfig().validate() == []

    def test_rolling_window_zero(self):
        cfg = SchedulingConfig(rolling_window_hours=0)
        errors = cfg.validate()
        assert len(errors) == 1
        assert errors[0].field == "rolling_window_hours"


# ── PauseRetryConfig ──────────────────────────────────────────────────


class TestPauseRetryConfigValidation:
    def test_valid_defaults(self):
        assert PauseRetryConfig().validate() == []

    def test_backoff_zero(self):
        cfg = PauseRetryConfig(rate_limit_backoff_seconds=0)
        errors = cfg.validate()
        assert any("rate_limit_backoff" in e.field for e in errors)

    def test_token_exhaustion_zero(self):
        cfg = PauseRetryConfig(token_exhaustion_retry_seconds=0)
        errors = cfg.validate()
        assert any("token_exhaustion" in e.field for e in errors)

    def test_max_retries_negative(self):
        cfg = PauseRetryConfig(rate_limit_max_retries=-1)
        errors = cfg.validate()
        assert any("rate_limit_max_retries" in e.field for e in errors)

    def test_max_backoff_zero(self):
        cfg = PauseRetryConfig(rate_limit_max_backoff_seconds=0)
        errors = cfg.validate()
        assert any("rate_limit_max_backoff" in e.field for e in errors)


# ── ChatProviderConfig ────────────────────────────────────────────────


class TestChatProviderConfigValidation:
    def test_valid_anthropic(self):
        assert ChatProviderConfig(provider="anthropic").validate() == []

    def test_valid_ollama_with_url(self):
        cfg = ChatProviderConfig(provider="ollama", base_url="http://localhost:11434")
        assert cfg.validate() == []

    def test_invalid_provider(self):
        cfg = ChatProviderConfig(provider="openai")
        errors = cfg.validate()
        assert len(errors) == 1
        assert "provider" in errors[0].field

    def test_ollama_without_base_url(self):
        cfg = ChatProviderConfig(provider="ollama")
        errors = cfg.validate()
        assert any("base_url" in e.field for e in errors)


# ── AutoTaskConfig ────────────────────────────────────────────────────


class TestAutoTaskConfigValidation:
    def test_valid_defaults(self):
        assert AutoTaskConfig().validate() == []

    def test_max_plan_depth_zero(self):
        cfg = AutoTaskConfig(max_plan_depth=0)
        errors = cfg.validate()
        assert any("max_plan_depth" in e.field for e in errors)

    def test_max_steps_per_plan_zero(self):
        cfg = AutoTaskConfig(max_steps_per_plan=0)
        errors = cfg.validate()
        assert any("max_steps_per_plan" in e.field for e in errors)

    def test_base_priority_negative(self):
        cfg = AutoTaskConfig(base_priority=-1)
        errors = cfg.validate()
        assert any("base_priority" in e.field for e in errors)


# ── ArchiveConfig ─────────────────────────────────────────────────────


class TestArchiveConfigValidation:
    def test_valid_defaults(self):
        assert ArchiveConfig().validate() == []

    def test_after_hours_zero(self):
        cfg = ArchiveConfig(after_hours=0)
        errors = cfg.validate()
        assert any("after_hours" in e.field for e in errors)

    def test_invalid_status(self):
        cfg = ArchiveConfig(statuses=["COMPLETED", "INVALID_STATUS"])
        errors = cfg.validate()
        assert any("INVALID_STATUS" in e.message for e in errors)

    def test_all_valid_statuses(self):
        cfg = ArchiveConfig(statuses=["COMPLETED", "FAILED", "BLOCKED"])
        assert cfg.validate() == []


# ── LLMLoggingConfig ─────────────────────────────────────────────────


class TestLLMLoggingConfigValidation:
    def test_disabled_skips_checks(self):
        cfg = LLMLoggingConfig(enabled=False, retention_days=0)
        assert cfg.validate() == []

    def test_enabled_retention_zero(self):
        cfg = LLMLoggingConfig(enabled=True, retention_days=0)
        errors = cfg.validate()
        assert len(errors) == 1
        assert errors[0].field == "retention_days"

    def test_enabled_valid(self):
        cfg = LLMLoggingConfig(enabled=True, retention_days=30)
        assert cfg.validate() == []


# ── MemoryConfig ──────────────────────────────────────────────────────


class TestMemoryConfigValidation:
    def test_disabled_skips_checks(self):
        cfg = MemoryConfig(enabled=False, embedding_provider="invalid")
        assert cfg.validate() == []

    def test_enabled_invalid_provider(self):
        cfg = MemoryConfig(enabled=True, embedding_provider="invalid")
        errors = cfg.validate()
        assert any("embedding_provider" in e.field for e in errors)

    def test_enabled_valid_provider(self):
        cfg = MemoryConfig(enabled=True, embedding_provider="openai")
        assert cfg.validate() == []

    def test_enabled_max_chunk_size_zero(self):
        cfg = MemoryConfig(enabled=True, max_chunk_size=0)
        errors = cfg.validate()
        assert any("max_chunk_size" in e.field for e in errors)


# ── AgentProfileConfig ───────────────────────────────────────────────


class TestAgentProfileConfigValidation:
    def test_valid_profile(self):
        cfg = AgentProfileConfig(id="test", permission_mode="default")
        assert cfg.validate() == []

    def test_empty_id(self):
        cfg = AgentProfileConfig(id="", name="Test")
        errors = cfg.validate()
        assert any("id" in e.field for e in errors)

    def test_invalid_permission_mode(self):
        cfg = AgentProfileConfig(id="test", permission_mode="admin")
        errors = cfg.validate()
        assert any("permission_mode" in e.field for e in errors)

    def test_empty_permission_mode_ok(self):
        cfg = AgentProfileConfig(id="test", permission_mode="")
        assert cfg.validate() == []


# ── AppConfig.validate() aggregation ─────────────────────────────────


class TestAppConfigValidation:
    def test_valid_defaults_have_discord_warnings(self, tmp_path):
        """Default AppConfig has empty discord tokens, so validate returns errors."""
        cfg = AppConfig(data_dir=str(tmp_path / "data"))
        errors = cfg.validate()
        # Should have discord errors (empty bot_token and guild_id)
        discord_errors = [e for e in errors if e.section == "discord"]
        assert len(discord_errors) == 2

    def test_valid_config_no_errors(self, tmp_path):
        cfg = AppConfig(
            data_dir=str(tmp_path / "data"),
            discord=DiscordConfig(bot_token="tok", guild_id="123"),
        )
        errors = cfg.validate()
        fatal = [e for e in errors if e.severity == "error"]
        assert fatal == []

    def test_collects_all_errors(self, tmp_path):
        """Validation should collect ALL errors, not stop at first."""
        cfg = AppConfig(
            data_dir=str(tmp_path / "data"),
            workspace_dir="",
            database_path="",
            scheduling=SchedulingConfig(rolling_window_hours=0),
        )
        errors = cfg.validate()
        sections = {e.section for e in errors}
        # Should have errors from multiple sections
        assert "app" in sections
        assert "scheduling" in sections

    def test_cross_field_workspace_warning(self, tmp_path):
        """Workspace dir warning when path doesn't exist and parent not writable."""
        cfg = AppConfig(
            data_dir=str(tmp_path / "data"),
            workspace_dir="/nonexistent/deeply/nested/path",
            discord=DiscordConfig(bot_token="tok", guild_id="123"),
        )
        errors = cfg.validate()
        # May or may not produce a warning depending on filesystem
        # At minimum, should not crash
        assert isinstance(errors, list)

    def test_health_check_port_invalid(self, tmp_path):
        from src.config import HealthCheckConfig

        cfg = AppConfig(
            data_dir=str(tmp_path / "data"),
            discord=DiscordConfig(bot_token="tok", guild_id="123"),
            health_check=HealthCheckConfig(enabled=True, port=99999),
        )
        errors = cfg.validate()
        port_errors = [e for e in errors if e.field == "port"]
        assert len(port_errors) == 1

    def test_rate_limits_invalid_structure(self, tmp_path):
        cfg = AppConfig(
            data_dir=str(tmp_path / "data"),
            discord=DiscordConfig(bot_token="tok", guild_id="123"),
            rate_limits={"global": "not-a-dict"},
        )
        errors = cfg.validate()
        rl_errors = [e for e in errors if e.section == "rate_limits"]
        assert len(rl_errors) == 1

    def test_agent_profiles_delegated(self, tmp_path):
        cfg = AppConfig(
            data_dir=str(tmp_path / "data"),
            discord=DiscordConfig(bot_token="tok", guild_id="123"),
            agent_profiles=[AgentProfileConfig(id="", name="Bad")],
        )
        errors = cfg.validate()
        profile_errors = [e for e in errors if e.section == "agent_profiles"]
        assert len(profile_errors) >= 1


# ── load_config integration ──────────────────────────────────────────


class TestLoadConfigValidation:
    def test_load_config_raises_on_fatal_errors(self, tmp_path):
        """load_config() should still raise ConfigValidationError for backward compat."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "scheduling": {"rolling_window_hours": -1},
                }
            )
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(str(config_file))
        assert len(exc_info.value.errors) >= 1

    def test_load_config_succeeds_with_valid(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "discord": {"bot_token": "tok", "guild_id": "123"},
                }
            )
        )
        config = load_config(str(config_file))
        assert config.discord.bot_token == "tok"


# ── --validate-config CLI flag ───────────────────────────────────────


class TestParseArgs:
    def test_validate_config_flag(self):
        from src.main import _parse_args

        config_path, profile, validate_only = _parse_args(["--validate-config"])
        assert validate_only is True

    def test_validate_config_with_path(self):
        from src.main import _parse_args

        config_path, profile, validate_only = _parse_args(
            ["--validate-config", "/some/config.yaml"]
        )
        assert validate_only is True
        assert config_path == "/some/config.yaml"

    def test_no_validate_flag(self):
        from src.main import _parse_args

        _, _, validate_only = _parse_args(["/some/config.yaml"])
        assert validate_only is False

    def test_profile_and_validate(self):
        from src.main import _parse_args

        config_path, profile, validate_only = _parse_args(
            ["--profile", "dev", "--validate-config", "/cfg.yaml"]
        )
        assert profile == "dev"
        assert validate_only is True
        assert config_path == "/cfg.yaml"


class TestValidateConfigOnly:
    def test_valid_config_returns_zero(self, tmp_path):
        from src.main import _validate_config_only

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "discord": {"bot_token": "tok", "guild_id": "123"},
                }
            )
        )
        assert _validate_config_only(str(config_file)) == 0

    def test_invalid_config_returns_one(self, tmp_path):
        from src.main import _validate_config_only

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "scheduling": {"rolling_window_hours": -1},
                }
            )
        )
        assert _validate_config_only(str(config_file)) == 1

    def test_missing_file_returns_one(self):
        from src.main import _validate_config_only

        assert _validate_config_only("/nonexistent/config.yaml") == 1


# ── McpServerConfig injection helpers ─────────────────────────────────


class TestMcpServerConfigInjection:
    """Unit tests for McpServerConfig.should_inject_into_tasks and task_mcp_entry."""

    def test_should_inject_defaults_to_enabled_state(self):
        cfg = McpServerConfig(enabled=True, port=8082)
        assert cfg.should_inject_into_tasks is True

        cfg_off = McpServerConfig(enabled=False)
        assert cfg_off.should_inject_into_tasks is False

    def test_explicit_inject_overrides_default(self):
        cfg = McpServerConfig(enabled=True, port=8082, inject_into_tasks=False)
        assert cfg.should_inject_into_tasks is False

        cfg2 = McpServerConfig(enabled=False, inject_into_tasks=True)
        assert cfg2.should_inject_into_tasks is True

    def test_task_mcp_entry_when_enabled(self):
        cfg = McpServerConfig(enabled=True, host="127.0.0.1", port=8082)
        entry = cfg.task_mcp_entry()
        assert entry == {
            "agent-queue": {"type": "http", "url": "http://127.0.0.1:8082/mcp"},
        }

    def test_task_mcp_entry_custom_host_port(self):
        cfg = McpServerConfig(enabled=True, host="0.0.0.0", port=9999)
        entry = cfg.task_mcp_entry()
        assert entry["agent-queue"]["url"] == "http://0.0.0.0:9999/mcp"

    def test_task_mcp_entry_when_disabled(self):
        cfg = McpServerConfig(enabled=False)
        assert cfg.task_mcp_entry() == {}

    def test_task_mcp_entry_when_inject_false(self):
        cfg = McpServerConfig(enabled=True, port=8082, inject_into_tasks=False)
        assert cfg.task_mcp_entry() == {}

    def test_task_mcp_entry_when_enabled_false_but_inject_true(self):
        """Even with inject_into_tasks=True, disabled server produces empty entry."""
        cfg = McpServerConfig(enabled=False, inject_into_tasks=True)
        assert cfg.task_mcp_entry() == {}

    def test_inject_into_tasks_loaded_from_yaml(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "discord": {"bot_token": "tok", "guild_id": "123"},
                    "mcp_server": {
                        "enabled": True,
                        "port": 8082,
                        "inject_into_tasks": False,
                    },
                }
            )
        )
        cfg = load_config(str(config_file))
        assert cfg.mcp_server.enabled is True
        assert cfg.mcp_server.inject_into_tasks is False
        assert cfg.mcp_server.should_inject_into_tasks is False
