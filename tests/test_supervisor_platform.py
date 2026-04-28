"""Tests for Supervisor in its Platform role.

The Supervisor class is registered as a daemon-wide singleton in
``PlatformRegistry``.  When a profile sets ``platform: supervisor``,
the orchestrator dispatches via ``Supervisor.start(task) → wait() →
stop()`` on that singleton.  Per-task state (TaskContext, cancel
event) lives in module-level ContextVars so concurrent task dispatches
on the same instance don't race.

These tests verify:
- Supervisor satisfies the Platform contract (start/wait/stop/is_alive)
- ``requires_workspace`` is False so the orchestrator skips workspace prep
- ``profile.allowed_tools`` flows through to ``chat()`` as ``tool_overrides``
- Two concurrent ``wait()`` calls don't race on per-task state
- ``stop()`` cancels only this dispatch, not siblings
- Token estimation populates AgentOutput
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models import (
    AgentOutput,
    AgentProfile,
    AgentResult,
    TaskContext,
)
from src.platforms.base import Platform
from src.platforms.supervisor import (
    Supervisor,
    _cancel_var,
    _task_var,
)


def _make_supervisor() -> Supervisor:
    """Build a Supervisor with a chat() mock and minimal config."""
    from types import SimpleNamespace

    sup = MagicMock(spec=Supervisor)
    # MagicMock(spec=...) doesn't expose Platform's actual methods well —
    # real instance is simpler.  Instead, use a real Supervisor with stub
    # provider/handler/config.
    return sup


def _real_supervisor(chat_response: str = "ok") -> Supervisor:
    """Build a real Supervisor with chat() patched for the platform tests."""
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        chat_provider=SimpleNamespace(playbook_max_tokens=2048, max_tokens=1024),
        supervisor=SimpleNamespace(reflection=SimpleNamespace()),
        data_dir="/tmp",
    )
    orch = MagicMock()
    orch.bus = MagicMock()
    sup = Supervisor.__new__(Supervisor)
    sup.orchestrator = orch
    sup.config = cfg
    sup._provider = None  # not used, chat() is replaced
    sup._llm_logger = None
    sup.handler = MagicMock()
    sup.reflection = MagicMock()
    sup._registry = MagicMock()
    sup._cancel_events = []
    sup.chat_calls = []  # type: ignore[attr-defined]

    async def _chat(**kwargs):
        sup.chat_calls.append(kwargs)
        return chat_response

    sup.chat = _chat  # type: ignore[method-assign]
    return sup


class TestSupervisorPlatformContract:
    """Supervisor must satisfy the Platform ABC and skip workspace prep."""

    def test_is_platform_subclass(self):
        assert issubclass(Supervisor, Platform)

    def test_requires_workspace_is_false(self):
        # The orchestrator reads this ClassVar to decide whether to call
        # _prepare_workspace; tool-call-only platforms skip it.
        assert Supervisor.requires_workspace is False

    def test_name_and_capabilities(self):
        # Registered in default_registry() under this exact name.
        assert Supervisor.name == "supervisor"
        # Capabilities include MCP so profiles can attach MCP servers.
        from src.platforms.base import Capability

        assert Capability.MCP in Supervisor.capabilities


class TestSupervisorPlatformLifecycle:
    @pytest.mark.asyncio
    async def test_start_records_task_in_contextvar(self):
        sup = _real_supervisor()
        task = TaskContext(task_id="t-1", description="x")
        await sup.start(task)
        assert _task_var.get() is task
        assert _cancel_var.get() is not None
        assert await sup.is_alive() is True

    @pytest.mark.asyncio
    async def test_wait_calls_chat_with_description(self):
        sup = _real_supervisor(chat_response="completed")
        profile = AgentProfile(
            id="email-triager",
            name="Email Triager",
            platform="supervisor",
            allowed_tools=["list_tasks", "create_task"],
        )
        task = TaskContext(
            task_id="t-1",
            description="triage inbox",
            profile=profile,
        )
        await sup.start(task)
        out = await sup.wait()

        assert out.result == AgentResult.COMPLETED
        assert "completed" in out.summary
        assert len(sup.chat_calls) == 1
        call = sup.chat_calls[0]
        assert call["text"] == "triage inbox"
        # tool_overrides bounds the LLM to the profile's allowed_tools
        assert call["tool_overrides"] == ["list_tasks", "create_task"]
        # Cancel event was passed in (per-call cancellation)
        assert call["cancel_event"] is _cancel_var.get()
        # Token estimate is populated (non-zero for non-empty text)
        assert out.tokens_used > 0

    @pytest.mark.asyncio
    async def test_wait_passes_no_tool_overrides_when_profile_empty(self):
        sup = _real_supervisor()
        await sup.start(TaskContext(task_id="t-1", description="x", profile=None))
        await sup.wait()
        assert sup.chat_calls[0]["tool_overrides"] is None

    @pytest.mark.asyncio
    async def test_wait_returns_failed_when_chat_raises(self):
        sup = _real_supervisor()

        async def _raise(**_kwargs):
            raise RuntimeError("provider down")

        sup.chat = _raise  # type: ignore[method-assign]
        await sup.start(TaskContext(task_id="t-1", description="x"))
        out = await sup.wait()
        assert out.result == AgentResult.FAILED
        assert "provider down" in (out.error_message or "")

    @pytest.mark.asyncio
    async def test_wait_without_start_returns_failed(self):
        sup = _real_supervisor()
        out = await sup.wait()
        assert out.result == AgentResult.FAILED


class TestSupervisorPlatformStop:
    @pytest.mark.asyncio
    async def test_stop_marks_not_alive(self):
        sup = _real_supervisor()
        await sup.start(TaskContext(task_id="t-1", description="x"))
        await sup.stop()
        assert await sup.is_alive() is False

    @pytest.mark.asyncio
    async def test_stop_only_affects_current_task(self):
        # stop() must set only this dispatch's cancel_event; sibling
        # dispatches running on the same singleton stay alive.  We verify
        # by running two start/stop sequences in separate asyncio tasks
        # (each gets its own ContextVar copy).
        sup = _real_supervisor()
        ev_a_started = asyncio.Event()
        ev_b_check = asyncio.Event()
        recorded: dict[str, bool] = {}

        async def task_a() -> None:
            await sup.start(TaskContext(task_id="A", description="A"))
            ev_a_started.set()
            await ev_b_check.wait()
            recorded["a_alive"] = await sup.is_alive()

        async def task_b() -> None:
            await ev_a_started.wait()
            # B runs in its own asyncio task → its own _cancel_var copy.
            await sup.start(TaskContext(task_id="B", description="B"))
            await sup.stop()  # B's own cancel only
            recorded["b_alive"] = await sup.is_alive()
            ev_b_check.set()

        await asyncio.gather(task_a(), task_b())
        # B stopped itself; A is still running.
        assert recorded["b_alive"] is False
        assert recorded["a_alive"] is True


class TestRegistrySingleton:
    """default_registry(supervisor=...) registers the singleton properly."""

    def test_create_returns_same_instance(self):
        from src.platforms import default_registry

        sup = _real_supervisor()
        registry = default_registry(supervisor=sup)
        # create() returns the singleton verbatim, ignoring profile-based
        # construction since Supervisor's __init__ takes orchestrator/config.
        out = registry.create("supervisor", profile=None)
        assert out is sup

    def test_supervisor_listed_in_names(self):
        from src.platforms import default_registry

        sup = _real_supervisor()
        registry = default_registry(supervisor=sup)
        assert "supervisor" in registry.names()

    def test_no_supervisor_means_unknown_platform(self):
        from src.platforms import default_registry

        registry = default_registry()  # no supervisor registered
        with pytest.raises(ValueError, match="Unknown platform"):
            registry.create("supervisor", profile=None)
