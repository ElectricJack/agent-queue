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

    async def test_tracked_branch_launch_is_detached_and_scrubbed(self, handler, monkeypatch, tmp_path):
        from src.commands import system_commands
        from src.install import update
        from src.install.update import UpdatePlan

        handler.config.data_dir = str(tmp_path)
        handler.config.deploy.track_branch = "dev"
        monkeypatch.setenv("UNRELATED_SECRET", "must-not-reach-updater")
        monkeypatch.setattr(update, "plan_update", lambda checkout, **kwargs: UpdatePlan(
            checkout, "dev", "origin/dev", "a" * 40, "b" * 40, False,
        ))
        launch = AsyncMock(return_value=MagicMock(pid=42, wait=AsyncMock(return_value=0)))
        monkeypatch.setattr(system_commands.asyncio, "create_subprocess_exec", launch)
        handler.orchestrator._emit_text_notify = AsyncMock()
        result = await handler._cmd_update_and_restart({"automatic": True})
        assert result["success"] and result["selector"] == "origin/dev"
        assert launch.call_args.args[-1] == "--automatic"
        assert launch.call_args.kwargs["start_new_session"] is True
        assert "UNRELATED_SECRET" not in launch.call_args.kwargs["env"]
        assert (tmp_path / "update.log").stat().st_mode & 0o777 == 0o600

    async def test_tracking_holds_failed_target_and_respects_update_lock(
        self, handler, monkeypatch, tmp_path,
    ):
        from src.commands import system_commands
        from src.install import update
        from src.install.deploy_history import record_deploy
        from src.install.update import UpdatePlan

        handler.config.data_dir = str(tmp_path)
        handler.config.deploy.track_branch = "dev"
        monkeypatch.setattr(update, "plan_update", lambda checkout, **kwargs: UpdatePlan(
            checkout, "dev", "origin/dev", "a" * 40, "b" * 40, False,
        ))
        record_deploy(tmp_path, "rolled_back", target="b" * 40, automatic=True)
        launch = AsyncMock()
        monkeypatch.setattr(system_commands.asyncio, "create_subprocess_exec", launch)
        result = await handler._cmd_update_and_restart({"automatic": True})
        assert result["error"] == "deployment_failed"
        launch.assert_not_awaited()
        (tmp_path / "update.lock").touch()
        result = await handler._cmd_update_and_restart({"automatic": True})
        assert result["status"] == "updating"
        launch.assert_not_awaited()

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

    @pytest.mark.parametrize("was_paused", [False, True])
    @pytest.mark.parametrize("args", [{}, {"wait_for_tasks": True}, {"automatic": True}])
    async def test_unconfigured_update_refuses_without_mutation(
        self, handler, monkeypatch, args, was_paused
    ):
        from src.commands import system_commands
        from src.install import update

        planning = MagicMock(side_effect=AssertionError("unconfigured update planned"))
        monkeypatch.setattr(update, "plan_update", planning)
        launch = AsyncMock()
        monkeypatch.setattr(system_commands.asyncio, "create_subprocess_exec", launch)
        kill = MagicMock()
        monkeypatch.setattr(system_commands.os, "kill", kill)
        orch = handler.orchestrator
        orch._paused = was_paused
        orch._running_tasks = {"fixture": object()}
        orch.wait_for_running_tasks = AsyncMock()
        orch._emit_text_notify = AsyncMock()

        result = await handler._cmd_update_and_restart(args)

        assert result["success"] is False
        if args.get("automatic"):
            assert result["error"] == "automatic branch deployment is disabled"
        else:
            assert result["error"] == "deployment_not_configured"
            assert "deploy.tag_glob" in result["remediation"]
            assert "deploy.track_branch" in result["remediation"]
            assert "aq update" in result["remediation"]
        assert orch._paused is was_paused
        assert not orch._restart_requested
        planning.assert_not_called()
        launch.assert_not_awaited()
        kill.assert_not_called()
        orch.wait_for_running_tasks.assert_not_awaited()
        orch._emit_text_notify.assert_not_awaited()

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


async def test_tracked_deployment_recovery_hold_refuses_without_fetch_or_launch(handler, monkeypatch):
    from src.install import update
    from src.install.deploy_history import record_deploy

    handler.config.deploy.track_branch = "dev"
    record_deploy(Path(handler.config.data_dir), "recovery_required", target="old-failed-target")
    monkeypatch.setattr(update, "plan_update", lambda *a, **kw: pytest.fail("held check fetched"))
    result = await handler._cmd_update_and_restart({"automatic": True})
    assert not result["success"] and result["error"] == "migration_recovery_required"


class TestRunCommand:
    async def test_missing_working_dir_returns_error(self, handler):
        result = await handler.execute("run_command", {"command": "echo hi"})
        assert result == {"success": False, "error": "working_dir is required"}

    async def test_missing_command_returns_error(self, handler):
        result = await handler.execute("run_command", {"working_dir": "/tmp"})
        assert result == {"success": False, "error": "command is required"}

    async def test_missing_both_returns_error(self, handler):
        result = await handler.execute("run_command", {})
        assert result == {"success": False, "error": "command is required"}
