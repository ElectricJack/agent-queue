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
    isolated registries.  Production wiring (in :mod:`src.main`) calls
    :func:`default_registry` to populate the in-tree set.

    Some platforms — notably the in-process :class:`Supervisor` — are
    daemon-wide singletons rather than instances-per-task.  Pass them
    via the ``singletons`` dict at construction time; ``create(name,
    profile=...)`` returns the registered singleton verbatim instead of
    constructing fresh.  The singleton's ``start(task)`` / ``wait()`` /
    ``stop()`` lifecycle methods rely on ContextVars to keep per-task
    state isolated across concurrent dispatches.
    """

    def __init__(
        self,
        platforms: dict[str, type[Platform]],
        singletons: "dict[str, Platform] | None" = None,
    ):
        self._platforms = dict(platforms)
        self._singletons: dict[str, Platform] = dict(singletons or {})

    def get(self, name: str) -> type[Platform] | None:
        return self._platforms.get(name)

    def names(self) -> list[str]:
        # Platforms come from both class and singleton dicts so callers
        # validating profile.platform see the full available set.
        return list(set(self._platforms.keys()) | set(self._singletons.keys()))

    def create(
        self,
        name: str,
        profile: AgentProfile | None,
        llm_logger=None,
    ) -> Platform:
        # Singleton platforms (e.g. Supervisor) are returned verbatim — the
        # caller's ``profile`` rides on TaskContext.profile so the singleton
        # can read it inside ``start(task)`` without a constructor argument.
        if name in self._singletons:
            return self._singletons[name]
        cls = self._platforms.get(name)
        if cls is None:
            raise ValueError(
                f"Unknown platform: {name!r}. Available: {sorted(self.names())}"
            )
        return cls(profile=profile, llm_logger=llm_logger)


def default_registry(
    supervisor: "Platform | None" = None,
) -> PlatformRegistry:
    """Return a :class:`PlatformRegistry` populated with all in-tree platforms.

    Imports of platform modules are lazy so test code can construct a
    bare registry without pulling in heavy SDK dependencies.

    ``supervisor`` is the daemon-wide :class:`Supervisor` instance.  When
    provided it's registered as a singleton (one shared brain across all
    supervisor-platform tasks).  When *None*, supervisor-platform tasks
    fail with a clear "unknown platform" error instead of misbehaving.
    """
    from src.platforms.claude_sdk import ClaudeSDKPlatform
    from src.platforms.claude_cli import ClaudeCLIPlatform
    from src.platforms.codex_cli import CodexCLIPlatform

    singletons: dict[str, Platform] = {}
    if supervisor is not None:
        singletons[supervisor.name] = supervisor

    return PlatformRegistry(
        platforms={
            ClaudeSDKPlatform.name: ClaudeSDKPlatform,
            ClaudeCLIPlatform.name: ClaudeCLIPlatform,
            CodexCLIPlatform.name: CodexCLIPlatform,
        },
        singletons=singletons,
    )
