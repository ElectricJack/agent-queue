"""Runtimes layer: pluggable AI agent backends.

The orchestrator interacts with runtimes exclusively through the
:class:`Runtime` ABC defined in :mod:`src.runtimes.base`.  This module
exposes :class:`RuntimeRegistry` for looking up runtime classes by
string name; the registry is the single source of truth for which
runtimes a running daemon supports.

Plugin-as-runtime support (registering runtimes from external plugins
via ``PluginContext.register_runtime``) is a future swap of the
internal dict for a plugin-discovery hook — the registry's external
shape stays the same.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.runtimes.base import Capability, MessageCallback, Runtime

if TYPE_CHECKING:
    from src.models import AgentProfile

__all__ = ["Capability", "MessageCallback", "Runtime", "RuntimeRegistry", "default_registry"]


def _accepts_config(cls: type) -> bool:
    """True when *cls*'s constructor takes a ``config`` keyword."""
    import inspect

    try:
        params = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False
    if "config" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class RuntimeRegistry:
    """Looks up :class:`Runtime` classes by name.

    Construction takes an explicit ``runtimes`` dict so tests can build
    isolated registries.  Production wiring (in :mod:`src.main`) calls
    :func:`default_registry` to populate the in-tree set.

    Some runtimes — notably the in-process :class:`Supervisor` — are
    daemon-wide singletons rather than instances-per-task.  Pass them
    via the ``singletons`` dict at construction time; ``create(name,
    profile=...)`` returns the registered singleton verbatim instead of
    constructing fresh.  The singleton's ``start(task)`` / ``wait()`` /
    ``stop()`` lifecycle methods rely on ContextVars to keep per-task
    state isolated across concurrent dispatches.

    ``config`` is the daemon's :class:`~src.config.AppConfig`.  It is handed
    to every runtime the registry constructs so runtime-side policy that lives
    in config — notably the ``security.env_scrub_enabled`` kill switch and
    ``security.env_allowlist`` read by :func:`src.env_scrub.scrub_env` — is
    actually reachable from the subprocess launch path (trust-and-ops R6).
    ``None`` (tests, ad-hoc construction) means runtimes fall back to the
    shipped defaults.
    """

    def __init__(
        self,
        runtimes: dict[str, type[Runtime]],
        singletons: "dict[str, Runtime] | None" = None,
        config=None,
    ):
        self._runtimes = dict(runtimes)
        self._singletons: dict[str, Runtime] = dict(singletons or {})
        self._config = config

    def get(self, name: str) -> type[Runtime] | None:
        return self._runtimes.get(name)

    def names(self) -> list[str]:
        # Runtimes come from both class and singleton dicts so callers
        # validating profile.runtime see the full available set.
        return list(set(self._runtimes.keys()) | set(self._singletons.keys()))

    def create(
        self,
        name: str,
        profile: AgentProfile | None,
        llm_logger=None,
    ) -> Runtime:
        # Singleton runtimes (e.g. Supervisor) are returned verbatim — the
        # caller's ``profile`` rides on TaskContext.profile so the singleton
        # can read it inside ``start(task)`` without a constructor argument.
        if name in self._singletons:
            return self._singletons[name]
        cls = self._runtimes.get(name)
        if cls is None:
            raise ValueError(
                f"Unknown runtime: {name!r}. Available: {sorted(self.names())}"
            )
        # ``config`` is passed by keyword to every runtime that declares it.
        # Runtimes predating the kwarg (e.g. a plugin-registered one) keep
        # working — they fall back to the shipped env-scrub defaults.  The
        # signature is inspected rather than the call being wrapped in
        # ``except TypeError``, which would swallow a genuine TypeError from
        # inside the constructor.
        if _accepts_config(cls):
            return cls(profile=profile, llm_logger=llm_logger, config=self._config)
        return cls(profile=profile, llm_logger=llm_logger)


def default_registry(
    supervisor: "Runtime | None" = None,
    config=None,
) -> RuntimeRegistry:
    """Return a :class:`RuntimeRegistry` populated with all in-tree runtimes.

    Imports of runtime modules are lazy so test code can construct a
    bare registry without pulling in heavy SDK dependencies.

    ``supervisor`` is the daemon-wide :class:`Supervisor` instance.  When
    provided it's registered as a singleton (one shared brain across all
    supervisor-runtime tasks).  When *None*, supervisor-runtime tasks
    fail with a clear "unknown runtime" error instead of misbehaving.

    ``config`` is the daemon ``AppConfig``; it reaches every constructed
    runtime so the env-scrub policy (``security.*``) is honoured at the real
    subprocess launch site rather than only in tests.
    """
    from src.runtimes.acpx import ACPXRuntime
    from src.runtimes.claude_sdk import ClaudeSDKRuntime

    singletons: dict[str, Runtime] = {}
    if supervisor is not None:
        singletons[supervisor.name] = supervisor

    return RuntimeRegistry(
        runtimes={
            ACPXRuntime.name: ACPXRuntime,
            ClaudeSDKRuntime.name: ClaudeSDKRuntime,
        },
        config=config,
        singletons=singletons,
    )
