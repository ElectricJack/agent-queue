"""Unit tests for system command handlers."""

import os
import signal
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.orchestrator import Orchestrator


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
def mock_git():
    from src.git.manager import GitManager

    return MagicMock(spec=GitManager)


@pytest.fixture
async def handler(db, config, mock_git):
    from src.event_bus import EventBus
    from src.plugins.registry import PluginRegistry
    from src.plugins.services import build_internal_services

    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = mock_git

    services = build_internal_services(db=db, git=mock_git, config=config)
    registry = PluginRegistry(db=db, bus=EventBus(), config=config)
    registry._internal_services = services
    await registry.load_internal_plugins()
    orchestrator.plugin_registry = registry

    h = CommandHandler(orchestrator, config)
    registry.set_active_project_id_getter(lambda: h._active_project_id)
    return h


class TestGetStatusGraphLayoutFlag:
    async def test_get_status_reports_graph_layout_flag(self, command_handler_factory):
        h = await command_handler_factory()
        r = await h.execute("get_status", {})
        assert r["graph_layout_enabled"] is True
        h.config.graph_layout.enabled = False
        r2 = await h.execute("get_status", {})
        assert r2["graph_layout_enabled"] is False


class TestUpdateAndRestart:
    async def test_regenerates_dashboard_client_from_committed_spec_before_restart(
        self, handler, monkeypatch
    ):
        from src.commands import system_commands

        calls = []

        async def fake_run(*command, cwd, timeout):
            calls.append((command, Path(cwd), timeout))
            return 0, "updated", ""

        kills = []
        monkeypatch.setattr(system_commands, "_run_subprocess", fake_run)
        monkeypatch.setattr(system_commands.os, "kill", lambda pid, sig: kills.append((pid, sig)))
        handler.orchestrator._emit_text_notify = AsyncMock()

        result = await handler._cmd_update_and_restart({"reason": "test update"})

        repo_root = Path(system_commands.__file__).resolve().parents[2]
        assert calls == [
            (("git", "pull", "--ff-only"), repo_root, 30),
            (("pip", "install", "-e", "."), repo_root, 120),
            (
                ("npm", "run", "generate:ts-client", "--", "--from-file"),
                repo_root,
                120,
            ),
        ]
        assert result["status"] == "updating"
        assert kills == [(os.getpid(), signal.SIGTERM)]

    async def test_does_not_restart_when_dashboard_client_generation_fails(
        self, handler, monkeypatch
    ):
        from src.commands import system_commands

        async def fake_run(*command, cwd, timeout):
            if command[0] == "npm":
                return 1, "", "generator failed"
            return 0, "updated", ""

        kills = []
        monkeypatch.setattr(system_commands, "_run_subprocess", fake_run)
        monkeypatch.setattr(system_commands.os, "kill", lambda pid, sig: kills.append((pid, sig)))
        handler.orchestrator._emit_text_notify = AsyncMock()

        result = await handler._cmd_update_and_restart({"reason": "test update"})

        assert result == {"error": "TypeScript client generation failed: generator failed"}
        assert kills == []
        handler.orchestrator._emit_text_notify.assert_not_awaited()


class TestRunCommand:
    async def test_missing_working_dir_returns_error(self, handler):
        result = await handler.execute("run_command", {"command": "echo hi"})
        assert result == {"error": "working_dir is required"}

    async def test_missing_command_returns_error(self, handler):
        result = await handler.execute("run_command", {"working_dir": "/tmp"})
        assert result == {"error": "command is required"}

    async def test_missing_both_returns_error(self, handler):
        result = await handler.execute("run_command", {})
        assert result == {"error": "command is required"}
