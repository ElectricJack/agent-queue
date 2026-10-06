"""Unit tests for system command handlers."""

import sys
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.commands.handler import CommandHandler
from src.config import DatabaseConfig, AppConfig, DiscordConfig
from src.database import Database
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url=lease_dsn("test.db")),
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
    async def test_uses_shared_preflight_and_launches_the_operator_updater(
        self, handler, monkeypatch, tmp_path
    ):
        from src.commands import system_commands
        from src.install import update
        from src.install.update import UpdatePlan

        handler.config.data_dir = str(tmp_path)
        handler.config.deploy.tag_glob = "v*"
        handler.config.deploy.target = "production"
        repo_root = Path(system_commands.__file__).resolve().parents[2]
        planning = []

        def fake_plan(checkout, **kwargs):
            planning.append((checkout, kwargs))
            return UpdatePlan(checkout, "", "v0.2.0", "a" * 40, "b" * 40, False)

        launch = AsyncMock(return_value=MagicMock(pid=42))
        monkeypatch.setattr(update, "plan_update", fake_plan)
        monkeypatch.setattr(system_commands.asyncio, "create_subprocess_exec", launch)
        handler.orchestrator._emit_text_notify = AsyncMock()

        result = await handler._cmd_update_and_restart({"reason": "test update"})

        assert planning == [(repo_root, dict(
            state_dir=tmp_path, tag_glob="v*", promotion_target="production"
        ))]
        assert launch.call_args.args == (
            sys.executable, "-m", "src.cli.app", "update", "--yes"
        )
        assert launch.call_args.kwargs["start_new_session"] is True
        assert launch.call_args.kwargs["cwd"] == str(repo_root)
        assert launch.call_args.kwargs["env"]["AQ_INSTALL_STATE_DIR"] == str(tmp_path)
        assert result["status"] == "updating" and result["pid"] == 42
        assert result["selector"] == "v0.2.0"
        assert result["commit"] == "b" * 40
        assert (tmp_path / "update.log").exists()

    async def test_does_not_launch_or_restart_when_release_preflight_refuses(
        self, handler, monkeypatch
    ):
        from src.commands import system_commands
        from src.install import update
        from src.install.update import UpdateRefused

        def refuse(*args, **kwargs):
            raise UpdateRefused("deploy_tag_invalid: lightweight tag", "Select an annotated tag.")

        launch = AsyncMock()
        monkeypatch.setattr(update, "plan_update", refuse)
        monkeypatch.setattr(system_commands.asyncio, "create_subprocess_exec", launch)
        handler.orchestrator._emit_text_notify = AsyncMock()
        result = await handler._cmd_update_and_restart({"reason": "test update"})
        assert result["success"] is False
        assert "lightweight tag" in result["error"]
        launch.assert_not_awaited()
        handler.orchestrator._emit_text_notify.assert_not_awaited()

    async def test_up_to_date_deploy_does_not_start_another_update(self, handler, monkeypatch):
        from src.commands import system_commands
        from src.install import update
        from src.install.update import UpdatePlan

        monkeypatch.setattr(update, "plan_update", lambda checkout, **kwargs:
                            UpdatePlan(checkout, "", "v0.2.0", "a" * 40, "a" * 40, False))
        launch = AsyncMock()
        monkeypatch.setattr(system_commands.asyncio, "create_subprocess_exec", launch)
        result = await handler._cmd_update_and_restart({})
        assert result["status"] == "up_to_date"
        launch.assert_not_awaited()


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
