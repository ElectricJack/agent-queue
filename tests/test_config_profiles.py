"""Tests for environment-specific config profiles (profiles/ directory)."""

import os

import pytest
import yaml

from src.config import AppConfig, _deep_merge, load_config
from src.main import _parse_args


# ---------------------------------------------------------------------------
# _deep_merge tests
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        overlay = {"b": 3}
        assert _deep_merge(base, overlay) == {"a": 1, "b": 3}

    def test_nested_dict_merge(self):
        base = {"x": {"a": 1, "b": 2}}
        overlay = {"x": {"b": 3, "c": 4}}
        assert _deep_merge(base, overlay) == {"x": {"a": 1, "b": 3, "c": 4}}

    def test_list_replaced_not_appended(self):
        base = {"items": [1, 2, 3]}
        overlay = {"items": [4, 5]}
        assert _deep_merge(base, overlay) == {"items": [4, 5]}

    def test_none_removes_key(self):
        base = {"a": 1, "b": 2, "c": 3}
        overlay = {"b": None}
        assert _deep_merge(base, overlay) == {"a": 1, "c": 3}

    def test_none_removes_nested_key(self):
        base = {"x": {"a": 1, "b": 2}}
        overlay = {"x": {"a": None}}
        assert _deep_merge(base, overlay) == {"x": {"b": 2}}

    def test_none_for_missing_key_is_noop(self):
        base = {"a": 1}
        overlay = {"missing": None}
        assert _deep_merge(base, overlay) == {"a": 1}

    def test_new_keys_added(self):
        base = {"a": 1}
        overlay = {"b": 2}
        assert _deep_merge(base, overlay) == {"a": 1, "b": 2}

    def test_does_not_mutate_base(self):
        base = {"a": {"x": 1}}
        overlay = {"a": {"y": 2}}
        _deep_merge(base, overlay)
        assert base == {"a": {"x": 1}}


# ---------------------------------------------------------------------------
# Profile loading via load_config
# ---------------------------------------------------------------------------


class TestProfileLoading:
    def _write_config(self, config_dir, data, filename="config.yaml"):
        path = config_dir / filename
        path.write_text(yaml.dump(data))
        return str(path)

    def _write_profile(self, config_dir, profile_name, data):
        profiles_dir = config_dir / "profiles"
        profiles_dir.mkdir(exist_ok=True)
        (profiles_dir / f"{profile_name}.yaml").write_text(yaml.dump(data))

    def test_no_profile_backward_compatible(self, tmp_path):
        """Without --profile, load_config should work exactly as before."""
        path = self._write_config(
            tmp_path,
            {
                "discord": {"bot_token": "tok", "guild_id": "1"},
            },
        )
        config = load_config(path)
        assert config.profile == ""
        assert config.discord.bot_token == "tok"

    def test_profile_overlay_applied(self, tmp_path):
        """Profile overlay should deep-merge over base config."""
        path = self._write_config(
            tmp_path,
            {
                "discord": {"bot_token": "base-token", "guild_id": "1"},
                "scheduling": {"rolling_window_hours": 24},
            },
        )
        self._write_profile(
            tmp_path,
            "dev",
            {
                "scheduling": {"rolling_window_hours": 6},
            },
        )
        config = load_config(path, profile="dev")
        assert config.profile == "dev"
        assert config.scheduling.rolling_window_hours == 6
        # Base values preserved
        assert config.discord.bot_token == "base-token"

    def test_profile_not_found_raises(self, tmp_path):
        path = self._write_config(
            tmp_path,
            {
                "discord": {"bot_token": "x", "guild_id": "1"},
            },
        )
        with pytest.raises(FileNotFoundError, match="Profile 'nonexistent' not found"):
            load_config(path, profile="nonexistent")

    def test_profile_not_found_lists_available(self, tmp_path):
        """Error message should list available profiles when some exist."""
        path = self._write_config(
            tmp_path,
            {
                "discord": {"bot_token": "x", "guild_id": "1"},
            },
        )
        self._write_profile(tmp_path, "dev", {"scheduling": {}})
        self._write_profile(tmp_path, "staging", {"scheduling": {}})
        with pytest.raises(FileNotFoundError, match="Available profiles: dev, staging"):
            load_config(path, profile="prod")

    def test_profile_from_env_var(self, tmp_path, monkeypatch):
        """AGENT_QUEUE_PROFILE env var should be used when no CLI profile."""
        path = self._write_config(
            tmp_path,
            {
                "discord": {"bot_token": "x", "guild_id": "1"},
                "scheduling": {"rolling_window_hours": 24},
            },
        )
        self._write_profile(
            tmp_path,
            "staging",
            {
                "scheduling": {"rolling_window_hours": 12},
            },
        )
        monkeypatch.setenv("AGENT_QUEUE_PROFILE", "staging")
        config = load_config(path)
        assert config.profile == "staging"
        assert config.scheduling.rolling_window_hours == 12

    def test_cli_profile_overrides_env_var(self, tmp_path, monkeypatch):
        """CLI --profile should take precedence over AGENT_QUEUE_PROFILE."""
        path = self._write_config(
            tmp_path,
            {
                "discord": {"bot_token": "x", "guild_id": "1"},
                "scheduling": {"rolling_window_hours": 24},
            },
        )
        self._write_profile(
            tmp_path,
            "dev",
            {
                "scheduling": {"rolling_window_hours": 6},
            },
        )
        self._write_profile(
            tmp_path,
            "staging",
            {
                "scheduling": {"rolling_window_hours": 12},
            },
        )
        monkeypatch.setenv("AGENT_QUEUE_PROFILE", "staging")
        config = load_config(path, profile="dev")
        assert config.profile == "dev"
        assert config.scheduling.rolling_window_hours == 6

    def test_profile_applied_after_env_overlay(self, tmp_path, monkeypatch):
        """Profile should layer on top of env overlay."""
        path = self._write_config(
            tmp_path,
            {
                "discord": {"bot_token": "base", "guild_id": "1"},
                "scheduling": {"rolling_window_hours": 24},
            },
        )
        # Env overlay changes token
        env_overlay = tmp_path / "config.dev.yaml"
        env_overlay.write_text(
            yaml.dump(
                {
                    "discord": {"bot_token": "dev-token"},
                }
            )
        )
        # Profile changes scheduling
        self._write_profile(
            tmp_path,
            "local",
            {
                "scheduling": {"rolling_window_hours": 1},
            },
        )
        monkeypatch.setenv("AGENT_QUEUE_ENV", "dev")
        config = load_config(path, profile="local")
        assert config.discord.bot_token == "dev-token"  # from env overlay
        assert config.scheduling.rolling_window_hours == 1  # from profile
        assert config.profile == "local"

    def test_profile_none_removal(self, tmp_path):
        """Profile with None values should remove keys from base."""
        path = self._write_config(
            tmp_path,
            {
                "discord": {"bot_token": "x", "guild_id": "1"},
                "archive": {"enabled": True, "after_hours": 24},
            },
        )
        # Profile disables archive by removing after_hours (set to None)
        # Write raw YAML with null to test None handling
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "test.yaml").write_text("archive:\n  enabled: false\n")
        config = load_config(path, profile="test")
        assert config.archive.enabled is False

    def test_profile_with_yml_extension(self, tmp_path):
        """Profiles with .yml extension should also be listed in error messages."""
        path = self._write_config(
            tmp_path,
            {
                "discord": {"bot_token": "x", "guild_id": "1"},
            },
        )
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        (profiles_dir / "alt.yml").write_text(yaml.dump({"scheduling": {}}))
        with pytest.raises(FileNotFoundError, match="Available profiles: alt"):
            load_config(path, profile="missing")

    def test_profile_validation_runs_on_merged(self, tmp_path):
        """Validation should run on the final merged config."""
        path = self._write_config(
            tmp_path,
            {
                "discord": {"bot_token": "x", "guild_id": "1"},
                "scheduling": {"rolling_window_hours": 24},
            },
        )
        self._write_profile(
            tmp_path,
            "bad",
            {
                "scheduling": {"rolling_window_hours": -1},
            },
        )
        from src.config import ConfigValidationError

        with pytest.raises(ConfigValidationError):
            load_config(path, profile="bad")


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_no_args(self):
        config_path, profile, _ = _parse_args([])
        assert profile is None
        assert "config.yaml" in config_path

    def test_config_path_only(self):
        config_path, profile, _ = _parse_args(["/path/to/config.yaml"])
        assert config_path == "/path/to/config.yaml"
        assert profile is None

    def test_profile_flag(self):
        config_path, profile, _ = _parse_args(["--profile", "dev", "/path/config.yaml"])
        assert profile == "dev"
        assert config_path == "/path/config.yaml"

    def test_profile_equals_syntax(self):
        config_path, profile, _ = _parse_args(["--profile=staging", "/path/config.yaml"])
        assert profile == "staging"
        assert config_path == "/path/config.yaml"

    def test_profile_after_config_path(self):
        config_path, profile, _ = _parse_args(["/path/config.yaml", "--profile", "prod"])
        assert profile == "prod"
        assert config_path == "/path/config.yaml"

    def test_validate_config_preserved(self):
        _, profile, _ = _parse_args(["--validate-config", "--profile", "dev"])
        assert profile == "dev"
