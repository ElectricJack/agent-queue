"""Platforms layer: pluggable AI agent backends.

The orchestrator interacts with platforms exclusively through the
:class:`Platform` ABC defined in :mod:`src.platforms.base`.  This module
exposes :class:`PlatformRegistry` for looking up platform classes by
string name; the registry is the single source of truth for which
platforms a running daemon supports.

Plugin-as-platform support (registering platforms from external plugins
via ``PluginContext.register_platform``) is a future swap of the
internal dict for a plugin-discovery hook — the registry's external
shape stays the same.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.platforms.base import Capability, MessageCallback, Platform

if TYPE_CHECKING:
    from src.models import AgentProfile

__all__ = ["Capability", "MessageCallback", "Platform", "PlatformRegistry", "default_registry"]


class PlatformRegistry:
    """Looks up :class:`Platform` classes by name.

    Construction takes an explicit ``platforms`` dict so tests can build
    isolated registries.  Production wiring (in :mod:`src.main` /
    :mod:`src.supervisor`) calls :func:`default_registry` to populate
    the in-tree set.
    """

    def __init__(self, platforms: dict[str, type[Platform]]):
        self._platforms = dict(platforms)

    def get(self, name: str) -> type[Platform] | None:
        return self._platforms.get(name)

    def names(self) -> list[str]:
        return list(self._platforms.keys())

    def create(
        self,
        name: str,
        profile: AgentProfile | None,
        llm_logger=None,
    ) -> Platform:
        cls = self._platforms.get(name)
        if cls is None:
            raise ValueError(
                f"Unknown platform: {name!r}. Available: {sorted(self._platforms.keys())}"
            )
        return cls(profile=profile, llm_logger=llm_logger)


def default_registry() -> PlatformRegistry:
    """Return a :class:`PlatformRegistry` populated with all in-tree platforms.

    Imports of platform modules are lazy so test code can construct a
    bare registry without pulling in heavy SDK dependencies.
    """
    from src.platforms.claude_sdk import ClaudeSDKPlatform
    from src.platforms.claude_cli import ClaudeCLIPlatform
    from src.platforms.codex_cli import CodexCLIPlatform

    return PlatformRegistry(
        platforms={
            ClaudeSDKPlatform.name: ClaudeSDKPlatform,
            ClaudeCLIPlatform.name: ClaudeCLIPlatform,
            CodexCLIPlatform.name: CodexCLIPlatform,
        }
    )
