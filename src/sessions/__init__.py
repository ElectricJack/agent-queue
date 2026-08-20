"""Session runtime — agents as independent OS sessions, not blocked streams.

The daemon *starts, observes, nudges and adopts* sessions; it never holds an
agent's life inside one ``asyncio.Task``.  See
``docs/specs/design/session-runtime.md``.

This package's public surface:

- :mod:`src.sessions.provider` — the :class:`SessionProvider` ABC + types
- :mod:`src.sessions.fake` / :mod:`src.sessions.subprocess` — providers
- :mod:`src.sessions.harness_parser` / ``harness_registry`` — vault harnesses
- :mod:`src.sessions.spec` — :class:`SessionSpecBuilder`
- :mod:`src.sessions.exit_classifier` — process exit → typed verdict
- :mod:`src.sessions.reconciler` — the cascade step

The ``tmux`` provider (POSIX-only) is registered lazily by name; importing
this module on Windows must never pull it in.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.sessions.provider import (
    Cap,
    CapabilityUnsupported,
    DialogRule,
    NotSubmitted,
    PartialListError,
    SessionDiedDuringStartup,
    SessionError,
    SessionHandle,
    SessionProvider,
    SessionSpec,
)

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)

__all__ = [
    "Cap",
    "CapabilityUnsupported",
    "DialogRule",
    "NotSubmitted",
    "PartialListError",
    "SessionDiedDuringStartup",
    "SessionError",
    "SessionHandle",
    "SessionProvider",
    "SessionProviderRegistry",
    "SessionSpec",
    "default_session_registry",
]


class SessionProviderRegistry:
    """Looks up :class:`SessionProvider` classes by name.

    Mirrors :class:`~src.runtimes.RuntimeRegistry`: an explicit dict so
    tests build isolated registries, plus :func:`default_session_registry`
    for production wiring.

    Providers are **instantiated once per name and cached**.  That is not an
    optimisation — the tmux provider owns per-session locks and a state
    cache whose whole point is one ``list-panes`` + one ``ps`` per tick, and
    a fresh instance per call would throw both away.
    """

    def __init__(self, providers: dict[str, type[SessionProvider]], config=None):
        self._providers = dict(providers)
        self._config = config
        self._instances: dict[str, SessionProvider] = {}

    def get(self, name: str) -> type[SessionProvider] | None:
        return self._providers.get(name)

    def names(self) -> list[str]:
        return sorted(self._providers)

    def register(self, cls: type[SessionProvider]) -> None:
        """Add (or replace) a provider class.  Drops any cached instance."""
        self._providers[cls.name] = cls
        self._instances.pop(cls.name, None)

    def create(self, name: str, config=None) -> SessionProvider:
        """Return the (cached) provider instance registered under *name*."""
        cached = self._instances.get(name)
        if cached is not None:
            return cached
        cls = self._providers.get(name)
        if cls is None:
            raise ValueError(
                f"Unknown session provider: {name!r}. Available: {self.names()}"
            )
        instance = cls(config=config if config is not None else self._config)
        self._instances[name] = instance
        return instance


def default_session_registry(config=None) -> SessionProviderRegistry:
    """Registry populated with every in-tree provider available on this host.

    ``tmux`` is registered only when its module imports — it is POSIX-only,
    and a Windows dev machine must still be able to construct the registry
    and run the suite (design §10, "Windows dev machines").  ``subprocess``
    and ``fake`` are always present.
    """
    from src.sessions.fake import FakeProvider
    from src.sessions.subprocess import SubprocessProvider

    providers: dict[str, type[SessionProvider]] = {
        SubprocessProvider.name: SubprocessProvider,
        FakeProvider.name: FakeProvider,
    }

    try:  # pragma: no cover - depends on which wave has landed / which OS
        from src.sessions.tmux import TmuxProvider

        providers[TmuxProvider.name] = TmuxProvider
    except ImportError:
        logger.debug("tmux session provider unavailable on this host")

    return SessionProviderRegistry(providers, config=config)
