"""Process-lifecycle tests for the daemon entry point (platform plan 4-7, 23).

Drives the real ``src.main.run`` coroutine with monkeypatched config,
logging, Orchestrator, messaging adapter, and runtime registry so startup
ordering, degraded messaging, SQLite-only directory creation, argument
parsing, health checks, and the readiness-race teardown are exercised
without a real daemon, database, or Discord connection.
"""

from __future__ import annotations

import asyncio
import os
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.main as main_mod
from src.config import AppConfig, DatabaseConfig, DiscordConfig


class LoginFailure(Exception):
    """Type name is what src.main.run_bot matches for degraded messaging."""


class _FakeAdapter:
    platform_name = "fake"

    def __init__(self, events: list):
        self.events = events
        self.ready_cancel_delivered = False
        self.ready_cleanup_finished = False

    async def start(self):
        self.events.append("adapter.start")
        raise LoginFailure("token revoked")

    async def wait_until_ready(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.ready_cancel_delivered = True
            # Cancellation cleanup that needs the loop: only visible to the
            # scheduler if the losing race task is *awaited*, not just
            # cancelled (PLA-2).
            for _ in range(3):
                await asyncio.sleep(0)
            self.ready_cleanup_finished = True
            raise

    def get_command_handler(self):
        return None

    def is_connected(self):
        return False

    async def close(self):
        self.events.append("adapter.close")


def _sqlite_config(tmp_path) -> AppConfig:
    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "db" / "agent-queue.db"),
        data_dir=str(tmp_path / "data"),
    )
    config.mcp_server.enabled = False
    return config


def _install_run_env(monkeypatch, config, adapter):
    """Patch src.main collaborators; return the shared event/cycle recorder."""
    events: list[str] = []
    state = {"cycles": 0, "on_first_cycle": lambda: None}

    class FakeOrchestrator:
        def __init__(self, cfg, runtimes=None):
            self.config = cfg
            self.db = SimpleNamespace()
            self.bus = object()
            self._restart_requested = False
            self._paused = False
            self._running_tasks: dict = {}
            self._command_handler = None
            self._runtimes = None
            self.doctor_registry = None

        async def initialize(self):
            events.append("orch.initialize")

        def set_command_handler(self, handler):
            self._command_handler = handler

        def set_tool_registry(self, registry):
            pass

        async def run_one_cycle(self):
            state["cycles"] += 1
            if state["cycles"] == 1:
                events.append("first_cycle")
                state["on_first_cycle"]()

        async def shutdown(self):
            events.append("orch.shutdown")

    class FakeCommandHandler:
        def __init__(self, orchestrator, cfg):
            pass

    monkeypatch.setattr(main_mod, "load_config", lambda path, profile=None: config)
    monkeypatch.setattr(main_mod, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(main_mod, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(main_mod, "create_messaging_adapter", lambda cfg, orch: adapter)
    monkeypatch.setattr(main_mod, "default_registry", lambda config=None: object())
    monkeypatch.setattr("src.commands.handler.CommandHandler", FakeCommandHandler)
    return events, state


async def test_run_initializes_before_adapter_and_runs_degraded_after_login_failure(
    monkeypatch, tmp_path
):
    """Orchestrator initialization precedes adapter startup; a login failure
    degrades messaging instead of tearing the daemon down; shutdown closes
    the adapter and the orchestrator."""
    config = _sqlite_config(tmp_path)
    adapter = _FakeAdapter([])
    events_ref, state = _install_run_env(monkeypatch, config, adapter)
    adapter.events = events_ref  # share the recorder installed by the env
    state["on_first_cycle"] = lambda: os.kill(os.getpid(), signal.SIGTERM)

    async with asyncio.timeout(30):
        restart = await main_mod.run(str(tmp_path / "config.yaml"))

    assert restart is False
    assert "orch.initialize" in events_ref and "adapter.start" in events_ref
    assert events_ref.index("orch.initialize") < events_ref.index("adapter.start")
    # Login failure did not stop the scheduler: exactly one cycle ran before
    # the test's own SIGTERM, then teardown closed adapter and orchestrator.
    assert state["cycles"] == 1
    assert events_ref.index("first_cycle") < events_ref.index("adapter.close")
    assert "orch.shutdown" in events_ref


async def test_run_uses_database_url_only_for_sqlite_directory_creation(monkeypatch, tmp_path):
    """SQLite: the database parent directory is created. PostgreSQL: no
    URL-derived path is ever passed to makedirs."""

    class _StopStartup(Exception):
        pass

    makedirs_calls: list[str] = []
    real_makedirs = os.makedirs

    def recording_makedirs(path, *args, **kwargs):
        makedirs_calls.append(str(path))
        return real_makedirs(path, *args, **kwargs)

    def exploding_orchestrator(cfg, runtimes=None):
        raise _StopStartup

    monkeypatch.setattr(main_mod, "setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(main_mod, "Orchestrator", exploding_orchestrator)
    monkeypatch.setattr(os, "makedirs", recording_makedirs)

    sqlite_config = _sqlite_config(tmp_path)
    monkeypatch.setattr(main_mod, "load_config", lambda path, profile=None: sqlite_config)
    with pytest.raises(_StopStartup):
        await main_mod.run(str(tmp_path / "config.yaml"))
    expected_parent = os.path.dirname(
        sqlite_config.database.url or sqlite_config.database_path
    )
    assert makedirs_calls == [expected_parent]

    makedirs_calls.clear()
    pg_config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url="postgresql+asyncpg://user:pw@localhost:5432/aq"),
        data_dir=str(tmp_path / "data"),
    )
    assert pg_config.database.backend == "postgresql"
    monkeypatch.setattr(main_mod, "load_config", lambda path, profile=None: pg_config)
    with pytest.raises(_StopStartup):
        await main_mod.run(str(tmp_path / "config.yaml"))
    assert makedirs_calls == []


def test_parse_args_preserves_path_and_profile_precedence(monkeypatch):
    """CLI --profile (both forms) wins over AGENT_QUEUE_PROFILE, the
    --validate-config flag is recognized, and the first remaining positional
    argument is the config path."""
    monkeypatch.setenv("AGENT_QUEUE_PROFILE", "env-profile")

    assert main_mod._parse_args(["--profile", "cli-prof", "/etc/aq.yaml"]) == (
        "/etc/aq.yaml",
        "cli-prof",
        False,
    )
    assert main_mod._parse_args(["--profile=cli-eq", "--validate-config", "a.yaml", "b.yaml"]) == (
        "a.yaml",
        "cli-eq",
        True,
    )
    # No CLI profile: parse leaves profile None (env fallback belongs to
    # load_config, so CLI always wins when present) and defaults the path.
    assert main_mod._parse_args([]) == (main_mod.DEFAULT_CONFIG_PATH, None, False)


async def test_health_checks_reports_each_failed_dependency_independently(tmp_path):
    """One failing DB call marks only its own check unhealthy; healthy
    subsystems and the messaging status stay present alongside it."""
    config = _sqlite_config(tmp_path)
    orch = SimpleNamespace(
        config=config,
        _paused=False,
        _running_tasks={},
        db=SimpleNamespace(
            # First call (database check) fails; second (agents check) is
            # healthy — proving the checks are independent.
            list_agents=AsyncMock(side_effect=[RuntimeError("db down"), []]),
            list_tasks=AsyncMock(side_effect=RuntimeError("query timeout")),
        ),
    )
    adapter = _FakeAdapter([])

    checks = await main_mod._health_checks(orch, adapter)

    assert checks["database"] == {"ok": False, "error": "db down"}
    assert checks["agents"]["ok"] is True
    assert checks["agents"]["total"] == 0
    assert checks["tasks"]["ok"] is False
    assert "query timeout" in checks["tasks"]["error"]
    assert checks["orchestrator"]["ok"] is True
    assert checks["messaging"]["ok"] is False
    assert checks["messaging"]["platform"] == "fake"
    assert checks["messaging"]["connected"] is False


async def test_readiness_race_tasks_are_awaited_after_cancellation(monkeypatch, tmp_path):
    """PLA-2: when the readiness race resolves, the losing task must be
    awaited — its cancellation cleanup observed — before the scheduler
    proceeds, so no pending readiness coroutine survives into shutdown."""
    config = _sqlite_config(tmp_path)
    adapter = _FakeAdapter([])
    events_ref, state = _install_run_env(monkeypatch, config, adapter)
    adapter.events = events_ref

    cleanup_seen_at_first_cycle: list[bool] = []

    def on_first_cycle():
        cleanup_seen_at_first_cycle.append(adapter.ready_cleanup_finished)
        os.kill(os.getpid(), signal.SIGTERM)

    state["on_first_cycle"] = on_first_cycle

    async with asyncio.timeout(30):
        await main_mod.run(str(tmp_path / "config.yaml"))

    # The losing wait_until_ready task was cancelled…
    assert adapter.ready_cancel_delivered is True
    # …and had been *awaited to completion* before the first scheduler cycle.
    assert cleanup_seen_at_first_cycle == [True]
    assert adapter.ready_cleanup_finished is True


@pytest.mark.asyncio
async def test_scheduler_cycle_failure_is_logged_and_next_cycle_runs(monkeypatch, caplog):
    from unittest.mock import AsyncMock
    from src.main import _run_scheduler_cycles

    shutdown = asyncio.Event()
    orch = SimpleNamespace(run_one_cycle=AsyncMock())
    calls = 0

    async def cycle():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("one broken cycle")
        shutdown.set()

    async def no_wait(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    orch.run_one_cycle.side_effect = cycle
    monkeypatch.setattr(asyncio, "wait_for", no_wait)
    await _run_scheduler_cycles(orch, shutdown)
    assert calls == 2
    assert "Orchestrator cycle failed" in caplog.text
