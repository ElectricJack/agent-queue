"""Behavioral tests for the embedded MCP server lifecycle (platform plan 1-3).

Drives ``src.embedded_mcp.run_mcp_server`` — the real supervised loop — with
fake FastMCP / uvicorn / API modules injected via ``sys.modules`` so lifespan
sharing, the shutdown race, and crash-restart supervision are exercised
without real sockets or the ~3s MCP SDK import.
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from types import SimpleNamespace

from starlette.routing import Mount

from src.embedded_mcp import run_mcp_server


class _FakeOrchestrator:
    """Just enough orchestrator surface for run_mcp_server."""

    def __init__(self):
        self.db = object()
        self.bus = object()
        self._command_handler = None
        self.plugin_registry = None
        self.handler_sets: list = []

    def set_command_handler(self, handler):
        self.handler_sets.append(handler)
        self._command_handler = handler


def _fake_config():
    return SimpleNamespace(mcp_server=SimpleNamespace(host="127.0.0.1", port=0))


def _install_fakes(monkeypatch, serve_behaviors):
    """Install fake uvicorn / mcp.server / src.api.app / registration modules.

    ``serve_behaviors`` supplies one async callable per uvicorn.Server
    instantiation; ``serve()`` awaits the behavior for its own index.
    Returns a dict recording every object the fakes create or receive.
    """
    created: dict = {
        "mcps": [],
        "servers": [],
        "apps": [],
        "handlers": [],
        "register_calls": {},
        "exclusions": frozenset({"excluded-tool"}),
    }

    class FakeFastMCP:
        def __init__(self, *, name=None, instructions=None, host=None, port=None, lifespan=None):
            self.captured_lifespan = lifespan
            self._session_manager = None
            created["mcps"].append(self)

        def streamable_http_app(self):
            return SimpleNamespace(kind="mcp-sub-app")

        @property
        def session_manager(self):
            @asynccontextmanager
            async def _run():
                yield

            return SimpleNamespace(run=_run)

    mcp_pkg = types.ModuleType("mcp")
    mcp_server_mod = types.ModuleType("mcp.server")
    mcp_server_mod.FastMCP = FakeFastMCP
    mcp_pkg.server = mcp_server_mod

    uvicorn_mod = types.ModuleType("uvicorn")

    class UvConfig:
        def __init__(self, app, host=None, port=None, log_level=None):
            self.app = app

    class UvServer:
        def __init__(self, config):
            self.config = config
            self.index = len(created["servers"])
            created["servers"].append(self)

        async def serve(self):
            await serve_behaviors[self.index](self)

    uvicorn_mod.Config = UvConfig
    uvicorn_mod.Server = UvServer

    class _Router:
        def __init__(self):
            self.routes: list = []
            self.lifespan_context = None

    class _App:
        def __init__(self):
            self.router = _Router()

    api_mod = types.ModuleType("src.api.app")

    def create_app(*, orchestrator, config, health_provider=None, plan_content_provider=None):
        app = _App()
        created["apps"].append(app)
        return app

    api_mod.create_app = create_app

    reg_mod = types.ModuleType("src.mcp_registration")

    def get_effective_exclusions(config=None):
        created["exclusion_config"] = config
        return created["exclusions"]

    def register_command_tools(mcp, excluded=None, plugin_tools=None):
        created["register_calls"]["tools"] = {
            "mcp": mcp,
            "excluded": excluded,
            "plugin_tools": plugin_tools,
        }

    reg_mod.get_effective_exclusions = get_effective_exclusions
    reg_mod.register_command_tools = register_command_tools
    reg_mod.register_resources = lambda mcp: created["register_calls"].__setitem__(
        "resources", mcp
    )
    reg_mod.register_prompts = lambda mcp: created["register_calls"].__setitem__("prompts", mcp)

    handler_mod = types.ModuleType("src.commands.handler")

    class FakeCommandHandler:
        def __init__(self, orchestrator, config):
            created["handlers"].append(self)

    handler_mod.CommandHandler = FakeCommandHandler

    monkeypatch.setitem(sys.modules, "mcp", mcp_pkg)
    monkeypatch.setitem(sys.modules, "mcp.server", mcp_server_mod)
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn_mod)
    monkeypatch.setitem(sys.modules, "src.api.app", api_mod)
    monkeypatch.setitem(sys.modules, "src.mcp_registration", reg_mod)
    monkeypatch.setitem(sys.modules, "src.commands.handler", handler_mod)
    return created


async def test_embedded_lifespan_reuses_daemon_objects(monkeypatch):
    """Lifespan yields the daemon's own DB/bus/orchestrator and installs
    exactly one shared CommandHandler; registrations get effective exclusions."""
    shutdown_event = asyncio.Event()

    async def serve_then_block(server):
        shutdown_event.set()
        await asyncio.Event().wait()

    created = _install_fakes(monkeypatch, [serve_then_block])
    orch = _FakeOrchestrator()
    config = _fake_config()

    await run_mcp_server(orch, config, shutdown_event)

    assert len(created["mcps"]) == 1
    mcp = created["mcps"][0]

    async with mcp.captured_lifespan(mcp) as ctx:
        assert ctx["db"] is orch.db
        assert ctx["event_bus"] is orch.bus
        assert ctx["orchestrator"] is orch
        handler = ctx["command_handler"]

    # The handler was installed on the orchestrator, once.
    assert orch.handler_sets == [handler]
    assert orch._command_handler is handler

    # A second lifespan entry reuses the daemon handler — no duplicate.
    async with mcp.captured_lifespan(mcp) as ctx2:
        assert ctx2["command_handler"] is handler
    assert len(created["handlers"]) == 1

    # Registrations received the same FastMCP and the effective exclusions.
    assert created["exclusion_config"] is config
    tools_call = created["register_calls"]["tools"]
    assert tools_call["mcp"] is mcp
    assert tools_call["excluded"] == created["exclusions"]
    assert tools_call["plugin_tools"] == []
    assert created["register_calls"]["resources"] is mcp
    assert created["register_calls"]["prompts"] is mcp


async def test_embedded_server_shutdown_cancels_serve_without_backoff(monkeypatch):
    """Shutdown winning the race cancels *and awaits* the serve task and
    returns immediately — no restart, no backoff sleep."""
    shutdown_event = asyncio.Event()
    serve_cancelled = asyncio.Event()

    async def blocking_serve(server):
        shutdown_event.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            serve_cancelled.set()
            raise

    created = _install_fakes(monkeypatch, [blocking_serve])

    backoff_timeouts: list = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(awaitable, timeout=None):
        backoff_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)

    await run_mcp_server(_FakeOrchestrator(), _fake_config(), shutdown_event)

    # The serve task observed its cancellation before run_mcp_server returned.
    assert serve_cancelled.is_set()
    assert len(created["servers"]) == 1
    assert backoff_timeouts == []


async def test_embedded_server_crash_restarts_with_capped_backoff(monkeypatch):
    """A crashed server is restarted with progressing backoff (1.0 → 2.0)
    instead of terminating the daemon, and — PLA-1 — restarts must not
    accumulate root MCP mounts on the shared FastAPI router."""
    shutdown_event = asyncio.Event()

    async def crashing_serve(server):
        raise RuntimeError("boom")

    async def healthy_serve(server):
        shutdown_event.set()
        await asyncio.Event().wait()

    created = _install_fakes(monkeypatch, [crashing_serve, crashing_serve, healthy_serve])

    backoff_delays: list = []

    async def instant_wait_for(awaitable, timeout=None):
        # The supervised loop only reaches wait_for for its restart backoff:
        # record the delay and elapse it instantly.
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        backoff_delays.append(timeout)
        if shutdown_event.is_set():
            return None
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", instant_wait_for)

    orch = _FakeOrchestrator()
    await run_mcp_server(orch, _fake_config(), shutdown_event)

    # Two crashes → two backoff waits with progressing delay, then recovery.
    assert len(created["servers"]) == 3
    assert backoff_delays == [1.0, 2.0]

    # PLA-1: exactly one root MCP mount remains after restarts.
    assert len(created["apps"]) == 1
    mounts = [r for r in created["apps"][0].router.routes if isinstance(r, Mount)]
    assert len(mounts) == 1
