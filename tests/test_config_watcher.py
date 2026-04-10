"""Tests for config hot-reloading: ConfigWatcher, diff_configs, and reload_config command."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest
import yaml

from src.config import (
    AppConfig,
    ConfigWatcher,
    HOT_RELOADABLE_SECTIONS,
    RESTART_REQUIRED_SECTIONS,
    SchedulingConfig,
    PauseRetryConfig,
    ArchiveConfig,
    MonitoringConfig,
    LLMLoggingConfig,
    DiscordConfig,
    ChatProviderConfig,
    MemoryConfig,
    diff_configs,
    load_config,
)
from src.event_bus import EventBus


# ---------------------------------------------------------------------------
# diff_configs tests
# ---------------------------------------------------------------------------


class TestDiffConfigs:
    """Tests for diff_configs() helper."""

    def test_identical_configs_no_diff(self):
        a = AppConfig()
        b = AppConfig()
        assert diff_configs(a, b) == set()

    def test_scheduling_change_detected(self):
        a = AppConfig()
        b = AppConfig()
        b.scheduling = SchedulingConfig(rolling_window_hours=48)
        result = diff_configs(a, b)
        assert "scheduling" in result

    def test_multiple_changes_detected(self):
        a = AppConfig()
        b = AppConfig()
        b.scheduling = SchedulingConfig(rolling_window_hours=48)
        b.archive = ArchiveConfig(after_hours=72)
        result = diff_configs(a, b)
        assert result == {"scheduling", "archive"}

    def test_restart_required_section_detected(self):
        a = AppConfig()
        b = AppConfig()
        b.discord = DiscordConfig(bot_token="new-token")
        result = diff_configs(a, b)
        assert "discord" in result

    def test_mixed_hot_and_restart_changes(self):
        a = AppConfig()
        b = AppConfig()
        b.scheduling = SchedulingConfig(rolling_window_hours=48)
        b.discord = DiscordConfig(bot_token="new-token")
        result = diff_configs(a, b)
        assert result == {"scheduling", "discord"}

    def test_scalar_field_change(self):
        a = AppConfig()
        b = AppConfig()
        b.global_token_budget_daily = 100000
        result = diff_configs(a, b)
        assert "global_token_budget_daily" in result

    def test_no_private_fields_in_diff(self):
        """Internal fields like _config_path should not appear in diff."""
        a = AppConfig()
        b = AppConfig()
        b._config_path = "/some/path"
        result = diff_configs(a, b)
        assert "_config_path" not in result


# ---------------------------------------------------------------------------
# Classification constants tests
# ---------------------------------------------------------------------------


class TestClassificationConstants:
    """Verify that the hot-reload / restart classification sets are sane."""

    def test_no_overlap(self):
        """Hot-reloadable and restart-required should not overlap."""
        overlap = HOT_RELOADABLE_SECTIONS & RESTART_REQUIRED_SECTIONS
        assert overlap == set(), f"Overlapping sections: {overlap}"

    def test_scheduling_is_hot_reloadable(self):
        assert "scheduling" in HOT_RELOADABLE_SECTIONS

    def test_discord_requires_restart(self):
        assert "discord" in RESTART_REQUIRED_SECTIONS

    def test_hook_engine_removed_from_hot_reloadable(self):
        """hook_engine was removed (playbooks spec §13 Phase 3)."""
        assert "hook_engine" not in HOT_RELOADABLE_SECTIONS


# ---------------------------------------------------------------------------
# ConfigWatcher tests
# ---------------------------------------------------------------------------


class TestConfigWatcher:
    """Tests for the ConfigWatcher class."""

    @pytest.fixture
    def config_dir(self, tmp_path):
        """Create a temp config file."""
        config_data = {
            "workspace_dir": str(tmp_path / "workspaces"),
            "database_path": str(tmp_path / "test.db"),
            "discord": {
                "bot_token": "test-token-for-validation",
                "guild_id": "123456789",
            },
            "scheduling": {"rolling_window_hours": 24},
            "archive": {"after_hours": 24.0},
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(config_data))
        os.makedirs(tmp_path / "workspaces", exist_ok=True)
        return tmp_path, config_path

    @pytest.fixture
    def bus(self):
        return EventBus()

    @pytest.mark.asyncio
    async def test_reload_no_changes(self, config_dir, bus):
        tmp_path, config_path = config_dir
        config = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), bus, config)

        result = await watcher.reload()
        assert result["changed_sections"] == []

    @pytest.mark.asyncio
    async def test_reload_detects_hot_reloadable_change(self, config_dir, bus):
        tmp_path, config_path = config_dir
        config = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), bus, config)

        # Modify a hot-reloadable section
        config_data = yaml.safe_load(config_path.read_text())
        config_data["scheduling"]["rolling_window_hours"] = 48
        config_path.write_text(yaml.dump(config_data))

        events_received = []
        bus.subscribe("config.reloaded", lambda data: events_received.append(data))

        result = await watcher.reload()
        assert "scheduling" in result["changed_sections"]
        assert "scheduling" in result["applied"]
        assert result["restart_required"] == []

        # Verify event was emitted
        assert len(events_received) == 1
        assert "scheduling" in events_received[0]["changed_sections"]

        # Verify config was updated in-place
        assert watcher.config.scheduling.rolling_window_hours == 48

    @pytest.mark.asyncio
    async def test_reload_warns_restart_required(self, config_dir, bus):
        tmp_path, config_path = config_dir
        config = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), bus, config)

        # Modify a restart-required section
        config_data = yaml.safe_load(config_path.read_text())
        config_data["workspace_dir"] = str(tmp_path / "new-workspaces")
        os.makedirs(tmp_path / "new-workspaces", exist_ok=True)
        config_path.write_text(yaml.dump(config_data))

        restart_events = []
        bus.subscribe("config.restart_needed", lambda d: restart_events.append(d))

        result = await watcher.reload()
        assert "workspace_dir" in result["restart_required"]

        # Verify restart_needed event was emitted
        assert len(restart_events) == 1
        assert "workspace_dir" in restart_events[0]["changed_sections"]

    @pytest.mark.asyncio
    async def test_reload_invalid_config_keeps_current(self, config_dir, bus):
        tmp_path, config_path = config_dir
        config = load_config(str(config_path))
        original_window = config.scheduling.rolling_window_hours
        watcher = ConfigWatcher(str(config_path), bus, config)

        # Write invalid config (validation should fail)
        config_data = yaml.safe_load(config_path.read_text())
        config_data["scheduling"]["rolling_window_hours"] = -1
        config_path.write_text(yaml.dump(config_data))

        result = await watcher.reload()
        assert "error" in result
        # Config should remain unchanged
        assert watcher.config.scheduling.rolling_window_hours == original_window

    @pytest.mark.asyncio
    async def test_reload_mixed_changes(self, config_dir, bus):
        tmp_path, config_path = config_dir
        config = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), bus, config)

        # Modify both hot-reloadable and restart-required sections
        config_data = yaml.safe_load(config_path.read_text())
        config_data["scheduling"]["rolling_window_hours"] = 48
        config_data["workspace_dir"] = str(tmp_path / "new-ws")
        os.makedirs(tmp_path / "new-ws", exist_ok=True)
        config_path.write_text(yaml.dump(config_data))

        reloaded_events = []
        restart_events = []
        bus.subscribe("config.reloaded", lambda d: reloaded_events.append(d))
        bus.subscribe("config.restart_needed", lambda d: restart_events.append(d))

        result = await watcher.reload()
        assert "scheduling" in result["applied"]
        assert "workspace_dir" in result["restart_required"]
        assert len(reloaded_events) == 1
        assert len(restart_events) == 1

    @pytest.mark.asyncio
    async def test_start_and_stop(self, config_dir, bus):
        tmp_path, config_path = config_dir
        config = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), bus, config, poll_interval=0.1)

        watcher.start()
        assert watcher._task is not None
        assert not watcher._task.done()

        await watcher.stop()
        assert watcher._task is None

    @pytest.mark.asyncio
    async def test_poll_detects_mtime_change(self, config_dir, bus):
        """Verify the poll loop detects file changes via mtime."""
        tmp_path, config_path = config_dir
        config = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), bus, config, poll_interval=0.05)

        events = []
        bus.subscribe("config.reloaded", lambda d: events.append(d))

        watcher.start()

        # Wait a moment, then modify the config
        await asyncio.sleep(0.02)
        config_data = yaml.safe_load(config_path.read_text())
        config_data["archive"]["after_hours"] = 72.0
        config_path.write_text(yaml.dump(config_data))

        # Wait for poll to detect change
        await asyncio.sleep(0.2)
        await watcher.stop()

        assert len(events) >= 1
        assert "archive" in events[0]["changed_sections"]

    @pytest.mark.asyncio
    async def test_config_property(self, config_dir, bus):
        tmp_path, config_path = config_dir
        config = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), bus, config)
        assert watcher.config is config


# ---------------------------------------------------------------------------
# Integration: reload_config command
# ---------------------------------------------------------------------------


class TestReloadConfigCommand:
    """Test the reload_config command handler integration."""

    @pytest.mark.asyncio
    async def test_no_watcher_returns_error(self):
        """When config watcher is not active, command returns error."""
        from unittest.mock import AsyncMock, MagicMock
        from src.command_handler import CommandHandler

        orch = MagicMock()
        orch._config_watcher = None
        config = AppConfig()

        handler = CommandHandler(orch, config)
        result = await handler.execute("reload_config", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_reload_returns_summary(self, tmp_path):
        """When config changes, command returns a summary."""
        from unittest.mock import AsyncMock, MagicMock
        from src.command_handler import CommandHandler

        # Create a mock watcher that returns a result
        mock_watcher = MagicMock()
        mock_watcher.reload = AsyncMock(
            return_value={
                "changed_sections": ["scheduling"],
                "applied": ["scheduling"],
                "restart_required": [],
            }
        )

        orch = MagicMock()
        orch._config_watcher = mock_watcher
        config = AppConfig()

        handler = CommandHandler(orch, config)
        result = await handler.execute("reload_config", {})
        assert "message" in result
        assert "scheduling" in result["applied"]

    @pytest.mark.asyncio
    async def test_reload_no_changes(self):
        """When no changes detected, returns appropriate message."""
        from unittest.mock import AsyncMock, MagicMock
        from src.command_handler import CommandHandler

        mock_watcher = MagicMock()
        mock_watcher.reload = AsyncMock(
            return_value={
                "changed_sections": [],
                "applied": [],
                "restart_required": [],
            }
        )

        orch = MagicMock()
        orch._config_watcher = mock_watcher
        config = AppConfig()

        handler = CommandHandler(orch, config)
        result = await handler.execute("reload_config", {})
        assert "No configuration changes" in result.get("message", "")
