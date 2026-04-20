"""Unit tests for system command handlers."""

import pytest
from unittest.mock import MagicMock

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
