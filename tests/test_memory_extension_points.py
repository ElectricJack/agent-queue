"""Contract tests proving the memory plugin is reachable via the
extension points and absent-cleanly when no plugin registers itself.

Spec §8 acceptance test (docs/superpowers/plans/2026-04-25-aq-memory-extraction.md
§Task 4): the plugin <-> core boundary is the
``plugin_registry.get_service("memory")`` extension point and the
``ctx.register_service("memory", ...)`` registration on the plugin side.
With these in place the internal memory plugin can become external in
later tasks without touching the call sites again.

Tests:

* ``test_get_service_returns_none_when_no_plugin`` — a bare plugin
  registry with no plugins loaded returns ``None`` for ``"memory"``.
* ``test_get_service_returns_stub_when_plugin_loaded`` — a stub plugin
  that calls ``ctx.register_service("memory", stub)`` is reachable via
  ``registry.get_service("memory")``.
* ``test_l1_facts_skipped_when_memory_absent`` — the supervisor's L1
  facts injection code path silently no-ops when no memory service is
  registered.
* ``test_l1_facts_injected_when_memory_present`` — the same code path
  injects the L1 facts string when a stub memory plugin is loaded.

The L1 facts code path is exercised by driving the
:class:`PromptBuilder` the same way :mod:`src.supervisor` does, with a
small stub orchestrator that exposes only the ``plugin_registry``
attribute.  This keeps the tests fast and avoids spinning up a full
:class:`Orchestrator`.
"""

from __future__ import annotations

import pytest

from src.plugins.base import Plugin, PluginContext
from src.prompt_builder import PromptBuilder


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubMemoryService:
    """Minimal stub matching the parts of MemoryServiceProtocol the
    supervisor's prompt-builder slice uses (load_l1_facts /
    load_l1_guidance / load_l2_context).
    """

    @property
    def available(self) -> bool:
        return True

    async def load_l1_facts(self, *, project_id, agent_type):
        return f"l1-facts:{project_id}:{agent_type}"

    async def load_l1_guidance(self, *, project_id, agent_type):
        return ""

    async def load_l2_context(self, query, *, project_id, **_):
        return ""


class _StubMemoryPlugin(Plugin):
    """In-memory test plugin that registers a stub memory service."""

    plugin_name = "memory"

    async def initialize(self, ctx: PluginContext) -> None:
        ctx.register_service("memory", _StubMemoryService())

    async def shutdown(self, ctx: PluginContext) -> None:
        pass


class _StubOrchestrator:
    """Bare object exposing only ``plugin_registry`` — what the supervisor's
    L1 facts code path actually touches.
    """

    def __init__(self, registry):
        self.plugin_registry = registry


# ---------------------------------------------------------------------------
# (a) registry.get_service("memory") returns None when no plugin loaded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_service_returns_none_when_no_plugin(plugin_registry):
    """With no memory plugin loaded, registry.get_service('memory') is None."""
    assert plugin_registry.get_service("memory") is None


# ---------------------------------------------------------------------------
# (b) registry.get_service("memory") returns the stub when loaded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_service_returns_stub_when_plugin_loaded(
    plugin_registry_with_plugin,
):
    registry = await plugin_registry_with_plugin(_StubMemoryPlugin)
    svc = registry.get_service("memory")
    assert svc is not None
    assert await svc.load_l1_facts(project_id="p1", agent_type="supervisor") == (
        "l1-facts:p1:supervisor"
    )


# ---------------------------------------------------------------------------
# (c) L1 facts code path silently no-ops when memory is absent
# ---------------------------------------------------------------------------


async def _drive_supervisor_l1_injection(orch, *, project_id: str) -> str:
    """Replicate the supervisor's L1-facts injection slice (src/supervisor.py
    around lines 438-461) and return the assembled prompt.

    Mirrors the exact lookup pattern this task installs:
    ``orch.plugin_registry.get_service("memory")`` instead of the old
    ``getattr(orch, "_memory_service", None)``.
    """
    builder = PromptBuilder()
    builder.set_l0_role_from_markdown("## Role\nYou are a test supervisor.")

    mem_svc = (
        orch.plugin_registry.get_service("memory")
        if getattr(orch, "plugin_registry", None) is not None
        else None
    )
    if mem_svc:
        try:
            l1_text = await mem_svc.load_l1_facts(
                project_id=project_id,
                agent_type="supervisor",
            )
            if l1_text:
                builder.set_l1_facts(l1_text)
        except Exception:
            pass  # graceful degradation

    return builder.build_task_prompt()


@pytest.mark.asyncio
async def test_l1_facts_skipped_when_memory_absent(plugin_registry):
    """When no memory plugin is loaded, the supervisor's L1 facts
    injection code path silently no-ops (no exception, no facts in the
    assembled prompt).
    """
    orch = _StubOrchestrator(plugin_registry)
    assembled = await _drive_supervisor_l1_injection(orch, project_id="p1")
    assert "l1-facts:" not in assembled  # nothing was injected


# ---------------------------------------------------------------------------
# (d) L1 facts code path injects when a memory plugin is present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l1_facts_injected_when_memory_present(plugin_registry_with_plugin):
    registry = await plugin_registry_with_plugin(_StubMemoryPlugin)
    orch = _StubOrchestrator(registry)
    assembled = await _drive_supervisor_l1_injection(orch, project_id="p1")
    assert "l1-facts:p1:supervisor" in assembled
