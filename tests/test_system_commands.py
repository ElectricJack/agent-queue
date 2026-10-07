"""Unit tests for system command handlers."""

import asyncio
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

        launch = AsyncMock(return_value=MagicMock(pid=42, wait=AsyncMock(return_value=10)))
        monkeypatch.setattr(update, "plan_update", fake_plan)
        monkeypatch.setattr(system_commands.asyncio, "create_subprocess_exec", launch)
        handler.orchestrator._emit_text_notify = AsyncMock()

        result = await handler._cmd_update_and_restart({"reason": "test update"})

        assert planning == [
            (repo_root, dict(state_dir=tmp_path, tag_glob="v*", promotion_target="production"))
        ]
        assert launch.call_args.args == (sys.executable, "-m", "src.cli.app", "update", "--yes")
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

        handler.config.deploy.tag_glob = "v*"

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

        handler.config.deploy.tag_glob = "v*"
        monkeypatch.setattr(
            update,
            "plan_update",
            lambda checkout, **kwargs: UpdatePlan(
                checkout, "", "v0.2.0", "a" * 40, "a" * 40, False
            ),
        )
        launch = AsyncMock()
        monkeypatch.setattr(system_commands.asyncio, "create_subprocess_exec", launch)
        result = await handler._cmd_update_and_restart({})
        assert result["status"] == "up_to_date"
        launch.assert_not_awaited()

    @pytest.mark.parametrize("pull_output", ["Already up to date.", "Local commits retained."])
    async def test_without_selector_preserves_pull_and_restart(
        self, handler, monkeypatch, pull_output
    ):
        from src.commands import system_commands
        from src.install import update

        monkeypatch.setattr(
            update, "plan_update", lambda *a, **k: pytest.fail("selector is opt-in")
        )
        run = AsyncMock(side_effect=[(0, pull_output, ""), (0, "", ""), (0, "", "")])
        monkeypatch.setattr(system_commands, "_run_subprocess", run)
        kill = MagicMock()
        monkeypatch.setattr(system_commands.os, "kill", kill)
        handler.orchestrator._emit_text_notify = AsyncMock()
        result = await handler._cmd_update_and_restart({})
        assert result["status"] == "updating" and result["pull_output"] == pull_output
        assert run.await_args_list[0].args == ("git", "pull", "--ff-only")
        assert run.await_count == 3
        assert handler.orchestrator._restart_requested
        kill.assert_called_once()

    @pytest.mark.parametrize(
        "stage,error", [(0, "git pull"), (1, "pip install"), (2, "TypeScript client")]
    )
    async def test_legacy_update_failures_do_not_restart_or_pause(
        self, handler, monkeypatch, stage, error
    ):
        from src.commands import system_commands

        run = AsyncMock(side_effect=[(0, "", "")] * stage + [(1, "", "fixture failure")])
        monkeypatch.setattr(system_commands, "_run_subprocess", run)
        kill = MagicMock()
        monkeypatch.setattr(system_commands.os, "kill", kill)
        result = await handler._cmd_update_and_restart({"wait_for_tasks": True})
        assert error in result["error"]
        assert not handler.orchestrator._paused
        kill.assert_not_called()

    @pytest.mark.parametrize("failure", ["wait", "cancel", "signal"])
    @pytest.mark.parametrize("was_paused", [False, True])
    async def test_interrupted_legacy_update_restores_prior_scheduling(
        self, handler, monkeypatch, failure, was_paused
    ):
        from src.commands import system_commands

        monkeypatch.setattr(system_commands, "_run_subprocess", AsyncMock(return_value=(0, "", "")))
        orch = handler.orchestrator
        orch._paused = was_paused
        orch._running_tasks = {"fixture": object()}
        orch._emit_text_notify = AsyncMock()
        orch.wait_for_running_tasks = AsyncMock()
        exception = asyncio.CancelledError if failure == "cancel" else RuntimeError
        kill = MagicMock()
        if failure == "signal":
            kill.side_effect = exception("fixture signal failure")
        else:
            orch.wait_for_running_tasks.side_effect = exception("fixture wait failure")
        monkeypatch.setattr(system_commands.os, "kill", kill)
        with pytest.raises(exception):
            await handler._cmd_update_and_restart({"wait_for_tasks": True})
        assert orch._paused is was_paused
        assert orch._restart_requested is False

    @pytest.mark.parametrize("exit_code", [0, 10, 20, -15])
    @pytest.mark.parametrize("was_paused", [False, True])
    async def test_exited_release_updater_restores_prior_scheduling(
        self, handler, monkeypatch, exit_code, was_paused
    ):
        from src.commands import system_commands
        from src.install import update
        from src.install.update import UpdatePlan

        handler.config.deploy.tag_glob = "v*"
        monkeypatch.setattr(
            update,
            "plan_update",
            lambda checkout, **kwargs: UpdatePlan(
                checkout, "", "v0.2.0", "a" * 40, "b" * 40, False
            ),
        )
        finished = asyncio.get_running_loop().create_future()
        process = MagicMock(pid=42, wait=AsyncMock(side_effect=lambda: None))

        async def wait():
            return await finished

        process.wait = wait
        monkeypatch.setattr(
            system_commands.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
        )
        orch = handler.orchestrator
        orch._paused = was_paused
        orch._running_tasks = {"fixture": object()}
        orch.wait_for_running_tasks = AsyncMock()
        orch._emit_text_notify = AsyncMock()
        result = await handler._cmd_update_and_restart({"wait_for_tasks": True})
        assert result["status"] == "updating" and orch._paused
        await asyncio.sleep(0)
        finished.set_result(exit_code)
        await asyncio.sleep(0)
        assert orch._paused is was_paused

    @pytest.mark.parametrize("failure", ["launch", "wait", "cancel"])
    async def test_release_update_prelaunch_failure_restores_scheduling(
        self, handler, monkeypatch, failure
    ):
        from src.commands import system_commands
        from src.install import update
        from src.install.update import UpdatePlan

        handler.config.deploy.tag_glob = "v*"
        monkeypatch.setattr(
            update,
            "plan_update",
            lambda checkout, **kwargs: UpdatePlan(
                checkout, "", "v0.2.0", "a" * 40, "b" * 40, False
            ),
        )
        orch = handler.orchestrator
        orch._running_tasks = {"fixture": object()}
        orch._emit_text_notify = AsyncMock()
        orch.wait_for_running_tasks = AsyncMock()
        launch = AsyncMock(side_effect=OSError("fixture launch failure"))
        monkeypatch.setattr(system_commands.asyncio, "create_subprocess_exec", launch)
        if failure == "launch":
            result = await handler._cmd_update_and_restart({"wait_for_tasks": True})
            assert not result["success"]
        else:
            exception = RuntimeError if failure == "wait" else asyncio.CancelledError
            orch.wait_for_running_tasks.side_effect = exception("fixture wait failure")
            with pytest.raises(exception):
                await handler._cmd_update_and_restart({"wait_for_tasks": True})
            launch.assert_not_awaited()
        assert not orch._paused

    def test_typed_response_preserves_release_receipt(self):
        from src.api.models.system import UpdateAndRestartResponse

        receipt = dict(
            pid=42,
            log="/fixture/update.log",
            selector="v0.2.0",
            commit="b" * 40,
            waited_for_tasks=True,
        )
        response = UpdateAndRestartResponse(**receipt).model_dump()
        assert all(response[key] == value for key, value in receipt.items())


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
