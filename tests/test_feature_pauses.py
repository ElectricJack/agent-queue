"""Tests for the framework-overhaul feature pauses.

Covers ``docs/specs/implementation/feature-pauses.md`` §10 (Test Plan):

* config defaults and hot-reload classification (§2)
* the memory plugin skip set (M1/M2)
* reflection forced to ``level="off"`` while memory is paused (M5)
* the playbook wiring block not being constructed (P1-P8, P11)
* housekeeping early returns (P9, P10)
* the ``CommandHandler.execute()`` gate and its exact error strings (§5)
* data preservation — no rows or compiled JSON are touched while paused (§7)
"""

from __future__ import annotations

import json
import logging
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import (
    MEMORY_PAUSED_ERROR,
    PAUSED_PLAYBOOK_COMMANDS,
    PLAYBOOKS_PAUSED_ERROR,
    CommandHandler,
    _is_memory_command,
)
from src.config import (
    RESTART_REQUIRED_SECTIONS,
    AppConfig,
    MemoryConfig,
    ObservationConfig,
    PlaybooksConfig,
    diff_configs,
    load_config,
)
from src.models import (
    Agent,
    AgentOutput,
    AgentProfile,
    AgentResult,
    PlaybookRun,
    Project,
    RepoSourceType,
    Task,
    TaskContext,
    TaskStatus,
    Workspace,
)
from src.orchestrator import Orchestrator
from src.plugins.registry import PluginRegistry
from src.runtimes.base import Runtime


# ---------------------------------------------------------------------------
# §2 / §10 — Config defaults
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    def test_dataclass_defaults_are_paused(self):
        assert MemoryConfig().enabled is False
        assert PlaybooksConfig().enabled is False
        assert ObservationConfig().enabled is False

    def test_appconfig_defaults_are_paused(self, tmp_path):
        cfg = AppConfig(data_dir=str(tmp_path / "data"))
        assert cfg.memory.enabled is False
        assert cfg.playbooks.enabled is False
        assert cfg.supervisor.observation.enabled is False

    def test_playbooks_config_validate_is_clean(self):
        assert PlaybooksConfig().validate() == []
        assert PlaybooksConfig(enabled=True).validate() == []

    def _write(self, tmp_path, body: str) -> str:
        """Write a config file with the minimum required keys plus *body*."""
        preamble = (
            "discord:\n"
            "  bot_token: test-token\n"
            "  guild_id: '123'\n"
            f"database_path: {tmp_path / 'test.db'}\n"
        )
        path = tmp_path / "config.yaml"
        path.write_text(preamble + body, encoding="utf-8")
        return str(path)

    def test_partial_sections_keep_paused_defaults(self, tmp_path):
        """A ``memory:``/``playbooks:`` section without ``enabled`` stays off.

        The *parse* default is authoritative for partial sections, which is
        why §2.1/§2.3 require flipping both the dataclass and the
        ``.get(..., default)`` call.
        """
        path = self._write(
            tmp_path,
            "memory:\n"
            "  recall_top_k: 9\n"
            "playbooks: {}\n"
            "supervisor:\n"
            "  observation:\n"
            "    max_buffer_size: 7\n",
        )
        cfg = load_config(path)
        assert cfg.memory.enabled is False
        assert cfg.memory.recall_top_k == 9
        assert cfg.playbooks.enabled is False
        assert cfg.supervisor.observation.enabled is False
        assert cfg.supervisor.observation.max_buffer_size == 7

    def test_explicit_true_round_trips(self, tmp_path):
        path = self._write(
            tmp_path,
            "memory:\n"
            "  enabled: true\n"
            "playbooks:\n"
            "  enabled: true\n"
            "supervisor:\n"
            "  observation:\n"
            "    enabled: true\n",
        )
        cfg = load_config(path)
        assert cfg.memory.enabled is True
        assert cfg.playbooks.enabled is True
        assert cfg.supervisor.observation.enabled is True

    def test_absent_sections_keep_paused_defaults(self, tmp_path):
        cfg = load_config(self._write(tmp_path, "agents:\n  max_concurrent: 2\n"))
        assert cfg.memory.enabled is False
        assert cfg.playbooks.enabled is False
        assert cfg.supervisor.observation.enabled is False

    def test_both_flags_are_restart_required(self):
        assert "playbooks" in RESTART_REQUIRED_SECTIONS
        assert "memory" in RESTART_REQUIRED_SECTIONS

    def test_playbooks_shows_up_in_diff_configs(self, tmp_path):
        old = AppConfig(data_dir=str(tmp_path / "d"))
        new = AppConfig(data_dir=str(tmp_path / "d"))
        new.playbooks = PlaybooksConfig(enabled=True)
        assert "playbooks" in diff_configs(old, new)

    def test_config_editor_schema_marks_both_flags(self):
        from src.config_editor import build_config_schema

        schema = build_config_schema()
        props = schema["properties"]
        for section in ("memory", "playbooks"):
            flag = props[section]["properties"]["enabled"]
            assert flag["default"] is False
            assert flag["restart_required"] is True
            assert "feature-pauses" in flag["description"]
        assert props["playbooks"]["x-reload"] == "restart"


# ---------------------------------------------------------------------------
# §5 / §10 — Command-surface gate
# ---------------------------------------------------------------------------


def _handler(*, memory: bool = False, playbooks: bool = False) -> CommandHandler:
    config = AppConfig()
    config.memory.enabled = memory
    config.playbooks.enabled = playbooks
    orch = MagicMock()
    orch.plugin_registry = None
    return CommandHandler(orch, config)


class TestCommandGate:
    def test_paused_playbook_command_set_matches_the_mixins(self):
        """The frozen set must cover exactly the two frozen command modules."""
        import src.commands.playbook_commands as pbc
        import src.commands.workflow_commands as wfc

        discovered = {
            name[len("_cmd_") :]
            for module in (pbc, wfc)
            for cls in vars(module).values()
            if isinstance(cls, type) and cls.__module__ == module.__name__
            for name in vars(cls)
            if name.startswith("_cmd_")
        }
        assert discovered == set(PAUSED_PLAYBOOK_COMMANDS)

    @pytest.mark.parametrize("name", sorted(PAUSED_PLAYBOOK_COMMANDS))
    async def test_every_playbook_command_is_gated(self, name):
        result = await _handler(playbooks=False).execute(name, {})
        assert result == {"success": False, "error": PLAYBOOKS_PAUSED_ERROR}

    @pytest.mark.parametrize(
        "name",
        ["memory_search", "memory_save", "memory_stats", "compact_memory", "memory"],
    )
    async def test_memory_commands_are_gated(self, name):
        result = await _handler(memory=False).execute(name, {})
        assert result == {"success": False, "error": MEMORY_PAUSED_ERROR}

    def test_memory_name_matcher(self):
        for name in ("memory", "memory_search", "memory.search", "aq-memory.memory_save"):
            assert _is_memory_command(name) is True
        for name in ("list_tasks", "memorable_thing", "create_task", "list_playbooks"):
            assert _is_memory_command(name) is False

    async def test_error_strings_are_exact(self):
        assert MEMORY_PAUSED_ERROR == "memory is paused (memory.enabled=false)"
        assert PLAYBOOKS_PAUSED_ERROR == "playbooks are paused (playbooks.enabled=false)"

    async def test_not_gated_when_enabled(self):
        """With the flags on, the gate is transparent and dispatch proceeds."""
        handler = _handler(memory=True, playbooks=True)
        assert handler._paused_command_error("list_playbooks") is None
        assert handler._paused_command_error("memory_search") is None
        # Dispatch falls through to the normal unknown-command path because
        # the memory plugin isn't loaded in this bare handler.
        result = await handler.execute("memory_search", {})
        assert result == {"error": "Unknown command: memory_search"}

    async def test_unknown_command_still_reports_unknown(self):
        result = await _handler().execute("definitely_not_a_command", {})
        assert result == {"error": "Unknown command: definitely_not_a_command"}

    async def test_playbook_gate_ignores_memory_flag(self):
        handler = _handler(memory=True, playbooks=False)
        assert handler._paused_command_error("list_playbooks") == PLAYBOOKS_PAUSED_ERROR

    async def test_memory_gate_ignores_playbook_flag(self):
        handler = _handler(memory=False, playbooks=True)
        assert handler._paused_command_error("memory_search") == MEMORY_PAUSED_ERROR


# ---------------------------------------------------------------------------
# M2 / §10 — Plugin skip set
# ---------------------------------------------------------------------------


class TestPluginSkip:
    def _registry(self, rows):
        db = MagicMock()
        db.list_plugins = AsyncMock(return_value=rows)
        db.update_plugin = AsyncMock()
        registry = PluginRegistry(db=db, bus=MagicMock(), config=AppConfig())
        registry.load_internal_plugins = AsyncMock(return_value=0)
        registry.load_plugin = AsyncMock()
        return registry, db

    async def test_skip_leaves_plugin_unloaded_and_db_untouched(self):
        rows = [
            {"id": "aq-memory", "status": "installed"},
            {"id": "aq-other", "status": "installed"},
        ]
        registry, db = self._registry(rows)

        loaded = await registry.load_all(skip=frozenset({"aq-memory", "memory"}))

        assert loaded == 1
        registry.load_plugin.assert_awaited_once_with("aq-other")
        # Crucially: the plugin's DB row keeps its status — re-enabling is a
        # config flip plus a restart, with no database change (M2).
        db.update_plugin.assert_not_awaited()

    async def test_no_skip_loads_everything(self):
        rows = [
            {"id": "aq-memory", "status": "installed"},
            {"id": "aq-other", "status": "installed"},
        ]
        registry, _ = self._registry(rows)
        assert await registry.load_all() == 2

    async def test_default_skip_is_empty(self):
        rows = [{"id": "aq-memory", "status": "installed"}]
        registry, _ = self._registry(rows)
        assert await registry.load_all() == 1


# ---------------------------------------------------------------------------
# M5 / §10 — Reflection forced off
# ---------------------------------------------------------------------------


class TestReflectionForcedOff:
    def _supervisor(self, *, memory_enabled: bool):
        from src.runtimes.supervisor import Supervisor

        config = AppConfig()
        config.memory.enabled = memory_enabled
        config.supervisor.reflection.level = "full"
        orch = MagicMock()
        return Supervisor(orch, config)

    def test_reflection_is_off_when_memory_paused(self):
        sup = self._supervisor(memory_enabled=False)
        assert sup.reflection._config.level == "off"
        assert sup.reflection.should_reflect("task.completed") is False
        assert sup.reflection.should_reflect("user.request") is False
        assert sup.reflection.determine_depth("task.completed", {}) is None

    def test_reflection_untouched_when_memory_enabled(self):
        sup = self._supervisor(memory_enabled=True)
        assert sup.reflection._config.level == "full"
        assert sup.reflection.should_reflect("task.completed") is True

    def test_app_config_reflection_object_is_not_mutated(self):
        """The override is a copy — the caller's config object is intact."""
        from src.runtimes.supervisor import Supervisor

        config = AppConfig()
        config.memory.enabled = False
        config.supervisor.reflection.level = "full"
        Supervisor(MagicMock(), config)
        assert config.supervisor.reflection.level == "full"


# ---------------------------------------------------------------------------
# P1-P11 / §10 — Paused orchestrator startup
# ---------------------------------------------------------------------------


def _base_config(tmp_path) -> AppConfig:
    return AppConfig(
        database_path=str(tmp_path / "test.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def paused_orch(tmp_path):
    orch = Orchestrator(_base_config(tmp_path))
    await orch.initialize()
    yield orch
    await orch.shutdown()


@pytest.fixture
async def enabled_orch(tmp_path):
    config = _base_config(tmp_path)
    config.playbooks.enabled = True
    orch = Orchestrator(config)
    await orch.initialize()
    yield orch
    await orch.shutdown()


class TestPausedStartup:
    PLAYBOOK_ATTRS = (
        "playbook_manager",
        "timer_service",
        "playbook_resume_handler",
        "workflow_stage_resume_handler",
        "orphan_workflow_recovery",
    )

    async def test_playbook_attributes_are_none(self, paused_orch):
        for attr in self.PLAYBOOK_ATTRS:
            assert getattr(paused_orch, attr) is None, attr

    async def test_memory_plumbing_is_down(self, paused_orch):
        assert paused_orch.workspace_spec_watcher is None
        assert paused_orch.reference_stub_enricher is None

    async def test_memory_service_absent(self, paused_orch):
        assert paused_orch.plugin_registry.get_service("memory") is None

    async def test_vault_watcher_keeps_non_playbook_handlers(self, paused_orch):
        handler_ids = _vault_handler_ids(paused_orch.vault_watcher)
        assert not any("playbook" in hid for hid in handler_ids), handler_ids
        # ...while the rest of the vault surface keeps watching.
        assert any("facts" in hid for hid in handler_ids), handler_ids

    async def test_cycles_and_shutdown_are_clean(self, paused_orch):
        seen: list[str] = []
        for event in ("timer.tick", "timer.1h", "timer.24h", "cron.tick"):
            paused_orch.bus.subscribe(event, lambda d, e=event: seen.append(e))
        for _ in range(10):
            await paused_orch.run_one_cycle()
        assert seen == []

    async def test_both_paused_lines_are_logged(self, tmp_path, caplog):
        orch = Orchestrator(_base_config(tmp_path))
        with caplog.at_level(logging.INFO, logger="src.orchestrator.core"):
            await orch.initialize()
        try:
            text = caplog.text
            assert "Memory subsystem PAUSED (memory.enabled=false)" in text
            assert "Playbooks subsystem PAUSED (playbooks.enabled=false)" in text
            assert "docs/specs/design/feature-pauses.md" in text
        finally:
            await orch.shutdown()

    async def test_health_checks_report_paused_state(self, paused_orch):
        from src.main import _health_checks

        adapter = MagicMock()
        adapter.is_connected.return_value = True
        adapter.platform_name = "test"
        checks = await _health_checks(paused_orch, adapter)
        for name, flag in (("memory", "memory.enabled"), ("playbooks", "playbooks.enabled")):
            assert checks[name] == {
                "check": name,
                "ok": True,
                "state": "paused",
                "severity": "info",
                "flag": flag,
            }


class TestEnabledStartup:
    async def test_playbook_wiring_is_constructed_when_enabled(self, enabled_orch):
        for attr in TestPausedStartup.PLAYBOOK_ATTRS:
            assert getattr(enabled_orch, attr) is not None, attr

    async def test_vault_watcher_has_playbook_handlers_when_enabled(self, enabled_orch):
        handler_ids = _vault_handler_ids(enabled_orch.vault_watcher)
        assert any("playbook" in hid for hid in handler_ids), handler_ids


def _vault_handler_ids(watcher) -> list[str]:
    """Best-effort extraction of registered vault-watcher handler ids."""
    for attr in ("_handlers", "handlers", "_registered"):
        registry = getattr(watcher, attr, None)
        if registry is None:
            continue
        if isinstance(registry, dict):
            return [str(k) for k in registry]
        try:
            return [str(getattr(h, "handler_id", h)) for h in registry]
        except TypeError:
            continue
    return []


# ---------------------------------------------------------------------------
# P9 / P10 — Housekeeping early returns
# ---------------------------------------------------------------------------


class TestHousekeepingEarlyReturns:
    async def test_paused_playbook_timeout_sweep_is_a_noop(self, paused_orch):
        paused_orch.command_handler = MagicMock()
        paused_orch.command_handler.check_paused_playbook_timeouts = AsyncMock(return_value=[])
        await paused_orch._check_paused_playbook_timeouts()
        paused_orch.command_handler.check_paused_playbook_timeouts.assert_not_awaited()

    async def test_paused_playbook_timeout_sweep_runs_when_enabled(self, enabled_orch):
        enabled_orch.command_handler = MagicMock()
        enabled_orch.command_handler.check_paused_playbook_timeouts = AsyncMock(return_value=[])
        await enabled_orch._check_paused_playbook_timeouts()
        enabled_orch.command_handler.check_paused_playbook_timeouts.assert_awaited_once()

    async def test_workflow_stage_check_is_a_noop(self, paused_orch):
        from src.models import Task, TaskStatus

        task = Task(
            id="t-1",
            project_id="p-1",
            title="t",
            description="d",
            status=TaskStatus.COMPLETED,
            workflow_id="wf-1",
        )
        paused_orch.db.get_workflow = AsyncMock(
            side_effect=AssertionError("must not be reached while paused")
        )
        await paused_orch._check_workflow_stage_completion(task)
        paused_orch.db.get_workflow.assert_not_awaited()

    async def test_workflow_stage_check_runs_when_enabled(self, enabled_orch):
        from src.models import Task, TaskStatus

        task = Task(
            id="t-1",
            project_id="p-1",
            title="t",
            description="d",
            status=TaskStatus.COMPLETED,
            workflow_id="wf-1",
        )
        enabled_orch.db.get_workflow = AsyncMock(return_value=None)
        await enabled_orch._check_workflow_stage_completion(task)
        enabled_orch.db.get_workflow.assert_awaited_once_with("wf-1")


# ---------------------------------------------------------------------------
# §7 / §10 — Data preservation
# ---------------------------------------------------------------------------


class TestDataPreservation:
    async def test_runs_and_compiled_json_survive_a_paused_boot(self, tmp_path):
        config = _base_config(tmp_path)
        compiled_dir = os.path.join(config.data_dir, "compiled")
        os.makedirs(compiled_dir, exist_ok=True)
        compiled_path = os.path.join(compiled_dir, "some-playbook.json")
        payload = json.dumps({"id": "some-playbook", "version": 3, "nodes": []})
        with open(compiled_path, "w", encoding="utf-8") as fh:
            fh.write(payload)

        run = PlaybookRun(
            run_id="pbr-preserved",
            playbook_id="some-playbook",
            playbook_version=3,
            status="paused",
            started_at=time.time(),
            paused_at=time.time(),
        )

        # Boot #1 — paused.  Seed the row, run cycles, shut down.
        orch = Orchestrator(config)
        await orch.initialize()
        try:
            await orch.db.create_playbook_run(run)
            for _ in range(5):
                await orch.run_one_cycle()
        finally:
            await orch.shutdown()

        with open(compiled_path, encoding="utf-8") as fh:
            assert fh.read() == payload, "compiled JSON must not be pruned while paused"

        # Boot #2 — enabled.  The run is still there and still paused.
        config2 = _base_config(tmp_path)
        config2.playbooks.enabled = True
        orch2 = Orchestrator(config2)
        await orch2.initialize()
        try:
            runs = await orch2.db.list_playbook_runs()
            assert [r.run_id for r in runs] == ["pbr-preserved"]
            assert runs[0].status == "paused"
            assert runs[0].playbook_version == 3
        finally:
            await orch2.shutdown()


# ---------------------------------------------------------------------------
# M3 / M4 / §10 — L1 and L2 prompt tiers stay empty end to end
# ---------------------------------------------------------------------------


class _CapturingRuntime(Runtime):
    """Records the TaskContext handed to ``start()`` and completes instantly."""

    def __init__(self):
        self.captured_ctx: TaskContext | None = None

    async def start(self, task: TaskContext) -> None:
        self.captured_ctx = task

    async def wait(self, on_message=None) -> AgentOutput:
        return AgentOutput(result=AgentResult.COMPLETED, summary="Done", tokens_used=1)

    async def stop(self) -> None:  # pragma: no cover - nothing to tear down
        pass

    async def is_alive(self) -> bool:
        return True


class _CapturingRegistry:
    def __init__(self):
        self.runtimes: list[_CapturingRuntime] = []

    def create(self, agent_type: str, profile=None, llm_logger=None) -> Runtime:
        runtime = _CapturingRuntime()
        self.runtimes.append(runtime)
        return runtime

    @property
    def last_ctx(self) -> TaskContext | None:
        return self.runtimes[-1].captured_ctx if self.runtimes else None


class TestPromptTiersEmptyWhilePaused:
    async def test_l1_l2_empty_but_l0_and_task_context_intact(self, tmp_path):
        """M3/M4: no memory service ⇒ empty L1/L2, everything else unaffected."""
        registry = _CapturingRegistry()
        orch = Orchestrator(_base_config(tmp_path), runtimes=registry)
        await orch.initialize()
        try:
            assert orch.plugin_registry.get_service("memory") is None

            profile = AgentProfile(
                id="coding",
                name="Coding Agent",
                system_prompt_suffix="You are a senior backend developer.",
            )
            await orch.db.create_profile(profile)
            await orch.db.create_project(
                Project(id="p-1", name="test-project", default_profile_id="coding")
            )
            await orch.db.create_workspace(
                Workspace(
                    id="ws-p-1",
                    project_id="p-1",
                    workspace_path=str(tmp_path / "ws"),
                    source_type=RepoSourceType.LINK,
                )
            )
            await orch.db.create_agent(Agent(id="a-1", name="claude-1", profile_id="claude"))
            await orch.db.create_task(
                Task(
                    id="t-1",
                    project_id="p-1",
                    title="Paused memory task",
                    description="Do something useful",
                    status=TaskStatus.READY,
                )
            )

            await orch.run_one_cycle()
            await orch.wait_for_running_tasks(timeout=10)

            ctx = registry.last_ctx
            assert ctx is not None, "runtime was never started — no TaskContext captured"
            assert ctx.l1_facts == ""
            assert ctx.l1_guidance == ""
            assert ctx.l2_context == ""
            # L0 role and the ordinary task context are untouched.
            assert ctx.l0_role == "You are a senior backend developer."
            assert "Do something useful" in ctx.description
            assert "## System Context" in ctx.description
        finally:
            await orch.wait_for_running_tasks(timeout=10)
            await orch.shutdown()
