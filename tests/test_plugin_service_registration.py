"""Tests for the plugin-registered service mechanism (spec §5.1, §5.2).

Plugins register typed services that core consumers can fetch via
the registry.  Returns None if no plugin has registered the service.
"""

from __future__ import annotations

import pytest

from src.plugins.base import Plugin, PluginContext


class _DummyService:
    """Concrete service returned by a test plugin."""

    def __init__(self, label: str) -> None:
        self.label = label

    async def ping(self) -> str:
        return f"pong:{self.label}"


class _ProvidingPlugin(Plugin):
    plugin_name = "test-provider"

    async def initialize(self, ctx: PluginContext) -> None:
        ctx.register_service("test_service", _DummyService("hello"))

    async def shutdown(self, ctx: PluginContext) -> None:
        pass


@pytest.mark.asyncio
async def test_registered_service_visible_via_registry(plugin_registry_with_plugin):
    """A plugin's registered service is reachable via registry.get_service."""
    registry = await plugin_registry_with_plugin(_ProvidingPlugin)

    svc = registry.get_service("test_service")
    assert svc is not None
    assert isinstance(svc, _DummyService)
    assert await svc.ping() == "pong:hello"


@pytest.mark.asyncio
async def test_unregistered_service_returns_none(plugin_registry):
    """Asking for a service no plugin registered returns None, not raises."""
    assert plugin_registry.get_service("nonexistent") is None


@pytest.mark.asyncio
async def test_service_cleared_on_unload(plugin_registry_with_plugin):
    """Unloading the plugin removes its registered services."""
    registry = await plugin_registry_with_plugin(_ProvidingPlugin)
    assert registry.get_service("test_service") is not None

    await registry.unload_plugin("test-provider")
    assert registry.get_service("test_service") is None


@pytest.mark.asyncio
async def test_register_service_rejects_empty_name(plugin_registry_with_plugin):
    """register_service('', obj) raises ValueError."""

    class _BadPlugin(Plugin):
        plugin_name = "bad-plugin"

        async def initialize(self, ctx: PluginContext) -> None:
            ctx.register_service("", _DummyService("x"))

        async def shutdown(self, ctx: PluginContext) -> None:
            pass

    with pytest.raises(ValueError, match="non-empty"):
        await plugin_registry_with_plugin(_BadPlugin)
