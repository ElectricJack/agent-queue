"""Tests for config hot-reloading: ConfigWatcher, diff_configs, and reload_config command."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import os

import pytest
import yaml

from src.config import (
    AppConfig,
    ConfigWatcher,
    HOT_RELOADABLE_SECTIONS,
    RESTART_REQUIRED_SECTIONS,
    SchedulingConfig,
    ArchiveConfig,
    DiscordConfig,
    GraphLayoutConfig,
    config_section_names,
    diff_configs,
    load_config,
)
from src.config_editor import classify_sections
from src.event_bus import EventBus


# ---------------------------------------------------------------------------
# diff_configs tests
# ---------------------------------------------------------------------------


class TestDiffConfigs:
    """Tests for diff_configs() helper."""

    def test_identical_configs_no_diff(self, tmp_path):
        a = AppConfig(data_dir=str(tmp_path / "data"))
        b = AppConfig(data_dir=str(tmp_path / "data"))
        assert diff_configs(a, b) == set()

    def test_scheduling_change_detected(self, tmp_path):
        a = AppConfig(data_dir=str(tmp_path / "data"))
        b = AppConfig(data_dir=str(tmp_path / "data"))
        b.scheduling = SchedulingConfig(rolling_window_hours=48)
        result = diff_configs(a, b)
        assert "scheduling" in result

    def test_multiple_changes_detected(self, tmp_path):
        a = AppConfig(data_dir=str(tmp_path / "data"))
        b = AppConfig(data_dir=str(tmp_path / "data"))
        b.scheduling = SchedulingConfig(rolling_window_hours=48)
        b.archive = ArchiveConfig(after_hours=72)
        result = diff_configs(a, b)
        assert result == {"scheduling", "archive"}

    def test_restart_required_section_detected(self, tmp_path):
        a = AppConfig(data_dir=str(tmp_path / "data"))
        b = AppConfig(data_dir=str(tmp_path / "data"))
        b.discord = DiscordConfig(bot_token="new-token")
        result = diff_configs(a, b)
        assert "discord" in result

    def test_mixed_hot_and_restart_changes(self, tmp_path):
        a = AppConfig(data_dir=str(tmp_path / "data"))
        b = AppConfig(data_dir=str(tmp_path / "data"))
        b.scheduling = SchedulingConfig(rolling_window_hours=48)
        b.discord = DiscordConfig(bot_token="new-token")
        result = diff_configs(a, b)
        assert result == {"scheduling", "discord"}

    def test_graph_layout_change_detected(self, tmp_path):
        a = AppConfig(data_dir=str(tmp_path / "data"))
        b = AppConfig(data_dir=str(tmp_path / "data"))
        b.graph_layout = GraphLayoutConfig(enabled=False)
        result = diff_configs(a, b)
        assert "graph_layout" in result
        assert "graph_layout" in HOT_RELOADABLE_SECTIONS

    def test_scalar_field_change(self, tmp_path):
        a = AppConfig(data_dir=str(tmp_path / "data"))
        b = AppConfig(data_dir=str(tmp_path / "data"))
        b.global_token_budget_daily = 100000
        result = diff_configs(a, b)
        assert "global_token_budget_daily" in result

    def test_no_private_fields_in_diff(self, tmp_path):
        """Internal fields like _config_path should not appear in diff."""
        a = AppConfig(data_dir=str(tmp_path / "data"))
        b = AppConfig(data_dir=str(tmp_path / "data"))
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
# Exhaustive coverage of every AppConfig section
# ---------------------------------------------------------------------------


class _Unmutatable(Exception):
    """Raised when the helper cannot build a differing value for a field."""


def _mutated(value):
    """Return a value that compares unequal to ``value`` under ``asdict``.

    Walks nested dataclasses so the probe changes a real leaf rather than
    replacing a whole section with a sentinel of a different shape.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            if f.name.startswith("_"):
                continue
            try:
                replacement = _mutated(getattr(value, f.name))
            except _Unmutatable:
                continue
            clone = copy.deepcopy(value)
            setattr(clone, f.name, replacement)
            return clone
        raise _Unmutatable(f"no mutatable leaf in {type(value).__name__}")
    if value is None:
        return "__diff_probe__"
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    if isinstance(value, str):
        return value + "__diff_probe__"
    if isinstance(value, dict):
        return {**value, "__diff_probe__": True}
    if isinstance(value, (list, tuple)):
        return [*value, "__diff_probe__"]
    raise _Unmutatable(f"cannot mutate {type(value).__name__}")


class TestEverySectionIsCovered:
    """Nothing in AppConfig may be invisible to the reload path.

    ``diff_configs`` and the hot/restart classification used to be
    hand-maintained lists; nine sections were missing from the diff list and
    eleven from the classification, so editing them produced no event at all
    (sound-bridge-70).  Both lists are now checked against ``AppConfig``
    itself, so a new section fails here instead of failing silently.
    """

    def test_section_names_match_appconfig_fields(self):
        expected = [f.name for f in dataclasses.fields(AppConfig) if not f.name.startswith("_")]
        assert list(config_section_names()) == expected

    def test_section_names_exclude_private_fields(self):
        assert not [name for name in config_section_names() if name.startswith("_")]

    @pytest.mark.parametrize("section", config_section_names())
    def test_diff_configs_reports_every_section(self, section, tmp_path):
        a = AppConfig(data_dir=str(tmp_path / "data"))
        b = AppConfig(data_dir=str(tmp_path / "data"))
        try:
            setattr(b, section, _mutated(getattr(b, section)))
        except _Unmutatable as exc:  # pragma: no cover - helper limitation
            pytest.fail(f"could not build a differing value for {section!r}: {exc}")
        assert diff_configs(a, b) == {section}

    @pytest.mark.parametrize("section", config_section_names())
    def test_every_section_is_classified(self, section):
        assert section in HOT_RELOADABLE_SECTIONS or section in RESTART_REQUIRED_SECTIONS, (
            f"{section!r} is in neither HOT_RELOADABLE_SECTIONS nor "
            "RESTART_REQUIRED_SECTIONS, so a change to it would be reported "
            "to nobody"
        )

    def test_classification_sets_name_only_real_sections(self):
        known = set(config_section_names())
        assert (HOT_RELOADABLE_SECTIONS | RESTART_REQUIRED_SECTIONS) - known == set()

    def test_config_editor_has_no_unclassified_bucket(self):
        assert classify_sections()["other"] == []


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
    async def test_integration_credentials_require_restart_and_cached_state_is_not_swapped(
        self, config_dir, bus
    ):
        _tmp_path, config_path = config_dir
        raw = yaml.safe_load(config_path.read_text())
        raw["integration"] = {
            "github_app": {
                "client_id": "Iv1.positive",
                "app_id": 101,
                "installation_id": 202,
                "private_key_path": "/run/secrets/positive.pem",
            },
            "scratch_probe": {
                "repository_id": 303,
                "repository_full_name": "acme/probe",
                "negative_client_id": "Iv1.negative",
                "negative_app_id": 404,
                "negative_installation_id": 505,
                "negative_private_key_path": "/run/secrets/negative.pem",
            },
        }
        config_path.write_text(yaml.safe_dump(raw))
        config = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), bus, config)
        cached_integration = watcher.config.integration

        raw["integration"]["scratch_probe"]["repository_id"] = 304
        config_path.write_text(yaml.safe_dump(raw))
        result = await watcher.reload()

        assert result["restart_required"] == ["integration"]
        assert result["applied"] == []
        assert watcher.config.integration is cached_integration
        assert watcher.config.integration.scratch_probe.repository_id == 303

    @pytest.mark.asyncio
    async def test_reload_reports_previously_blind_section(self, config_dir, bus):
        """``database`` was missing from the hand-written diff list.

        Editing it used to produce no ``config.reloaded`` and no
        ``config.restart_needed`` — the operator changed the file and nothing
        at all was said (sound-bridge-70).
        """
        tmp_path, config_path = config_dir
        config = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), bus, config)

        config_data = yaml.safe_load(config_path.read_text())
        config_data["database"] = {
            "url": str(tmp_path / "test.db"),
            "pool_max_size": 20,
        }
        config_path.write_text(yaml.dump(config_data))

        restart_events = []
        bus.subscribe("config.restart_needed", lambda d: restart_events.append(d))

        result = await watcher.reload()
        assert "database" in result["changed_sections"]
        assert "database" in result["restart_required"]
        assert len(restart_events) == 1
        assert "database" in restart_events[0]["changed_sections"]

    @pytest.mark.asyncio
    async def test_reload_reports_unclassified_section_as_restart(
        self, config_dir, bus, monkeypatch
    ):
        """A section in neither classification set is still reported.

        The classification registry stays hand-maintained (a test asserts it
        covers every section), so the reload path treats "not hot-reloadable"
        as restart-required rather than dropping the change on the floor.
        """
        _tmp_path, config_path = config_dir
        config = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), bus, config)

        monkeypatch.setattr(
            "src.config.HOT_RELOADABLE_SECTIONS",
            HOT_RELOADABLE_SECTIONS - {"archive"},
        )
        monkeypatch.setattr(
            "src.config.RESTART_REQUIRED_SECTIONS",
            RESTART_REQUIRED_SECTIONS - {"archive"},
        )

        config_data = yaml.safe_load(config_path.read_text())
        config_data["archive"]["after_hours"] = 72.0
        config_path.write_text(yaml.dump(config_data))

        restart_events = []
        bus.subscribe("config.restart_needed", lambda d: restart_events.append(d))

        result = await watcher.reload()
        assert result["applied"] == []
        assert "archive" in result["restart_required"]
        assert len(restart_events) == 1

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
    async def test_no_watcher_returns_error(self, tmp_path):
        """When config watcher is not active, command returns error."""
        from unittest.mock import MagicMock
        from src.commands.handler import CommandHandler

        orch = MagicMock()
        orch._config_watcher = None
        config = AppConfig(data_dir=str(tmp_path / "data"))

        handler = CommandHandler(orch, config)
        result = await handler.execute("reload_config", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_reload_returns_summary(self, tmp_path):
        """When config changes, command returns a summary."""
        from unittest.mock import AsyncMock, MagicMock
        from src.commands.handler import CommandHandler

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
        config = AppConfig(data_dir=str(tmp_path / "data"))

        handler = CommandHandler(orch, config)
        result = await handler.execute("reload_config", {})
        assert "message" in result
        assert "scheduling" in result["applied"]

    @pytest.mark.asyncio
    async def test_reload_no_changes(self, tmp_path):
        """When no changes detected, returns appropriate message."""
        from unittest.mock import AsyncMock, MagicMock
        from src.commands.handler import CommandHandler

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
        config = AppConfig(data_dir=str(tmp_path / "data"))

        handler = CommandHandler(orch, config)
        result = await handler.execute("reload_config", {})
        assert "No configuration changes" in result.get("message", "")


# ---------------------------------------------------------------------------
# Platform plan 15: hot/restart split with event shapes
# ---------------------------------------------------------------------------


async def test_reload_applies_only_hot_sections_and_emits_restart_notice(tmp_path):
    """One reload touching a hot and a restart-required section applies only
    the hot field in memory and emits each correctly shaped event."""
    from unittest.mock import AsyncMock

    (tmp_path / "workspaces").mkdir()
    (tmp_path / "new-workspaces").mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "workspace_dir": str(tmp_path / "workspaces"),
                "database_path": str(tmp_path / "test.db"),
                "discord": {"bot_token": "test-token", "guild_id": "123"},
                "scheduling": {"rolling_window_hours": 24},
            }
        )
    )

    config = load_config(str(config_path))
    original_workspace = config.workspace_dir
    bus = AsyncMock()
    watcher = ConfigWatcher(str(config_path), bus, config)

    data = yaml.safe_load(config_path.read_text())
    data["scheduling"]["rolling_window_hours"] = 48  # hot-reloadable
    data["workspace_dir"] = str(tmp_path / "new-workspaces")  # restart-required
    config_path.write_text(yaml.dump(data))

    result = await watcher.reload()

    assert result == {
        "changed_sections": ["scheduling", "workspace_dir"],
        "restart_required": ["workspace_dir"],
        "applied": ["scheduling"],
    }
    # Hot field mutated in place; restart-required field kept its old value.
    assert watcher.config.scheduling.rolling_window_hours == 48
    assert watcher.config.workspace_dir == original_workspace

    emitted = {call.args[0]: call.args[1] for call in bus.emit.await_args_list}
    assert set(emitted) == {"config.reloaded", "config.restart_needed"}
    assert emitted["config.reloaded"]["changed_sections"] == ["scheduling"]
    assert emitted["config.reloaded"]["config"] is watcher.config
    assert emitted["config.restart_needed"] == {"changed_sections": ["workspace_dir"]}
