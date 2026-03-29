"""Plugin system for extending AgentQueue with installable packages.

Plugins are first-class participants in the AgentQueue ecosystem. They
register commands, tools, hooks, and event types into existing infrastructure
through the PluginContext API.

Architecture
------------
- **base.py** — Plugin ABC, PluginContext, PluginInfo, PluginStatus
- **registry.py** — PluginRegistry: discover, load, unload, install, update, remove
- **loader.py** — Git clone, requirements install, module import utilities

Usage::

    from src.plugins import PluginRegistry

    registry = PluginRegistry(db=db, bus=bus, config=config)
    await registry.discover_plugins()
    await registry.load_all()
"""

from src.plugins.base import (
    Plugin,
    PluginContext,
    PluginInfo,
    PluginStatus,
    PluginPermission,
)
from src.plugins.registry import PluginRegistry

__all__ = [
    "Plugin",
    "PluginContext",
    "PluginInfo",
    "PluginRegistry",
    "PluginStatus",
    "PluginPermission",
]
