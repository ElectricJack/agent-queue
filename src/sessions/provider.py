"""Session provider contract — the ABC every session backend implements.

A *session* is one OS-level agent run whose initial process is the harness
CLI (``claude``, ``codex``, …).  Providers create, observe, nudge and kill
those sessions; the daemon never blocks on one.  See
``docs/specs/design/session-runtime.md`` §5.

Three providers are planned:

``tmux``
    The default on Linux/WSL2 — an attachable pane per session.  **Not in
    this module set**: it needs a POSIX host and lands with a later wave.
``subprocess``
    Degraded fallback: detached process group, log file, no pane.  Liveness
    and transcripts still work, so tasks complete normally.
``fake``
    In-memory, scriptable; every reconciler and cascade test runs on it.

Capability gating
-----------------
Callers branch on :class:`Cap`, **never** on ``provider.name``.  A caller
that wants to peek asks ``Cap.PEEK in provider.capabilities`` and degrades
gracefully; calling an unsupported operation raises
:class:`CapabilityUnsupported` rather than returning a plausible lie.

``is_running`` vs ``process_alive``
-----------------------------------
Deliberately distinct.  A tmux pane with ``remain-on-exit on`` outlives its
dead agent, so "the runtime artifact exists" and "the agent process is
alive" are two different facts and the exit classifier needs both.

Unknown is not dead
-------------------
:class:`PartialListError` exists so a provider can say "I could not
enumerate everything" instead of returning a short list that a caller would
read as "the missing ones died".  Reaping on a failed secondary probe was
one of the Gas City post-mortem's most expensive bugs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar

__all__ = [
    "Cap",
    "DialogRule",
    "SessionSpec",
    "SessionHandle",
    "SessionProvider",
    "SessionError",
    "CapabilityUnsupported",
    "NotSubmitted",
    "PartialListError",
    "SessionDiedDuringStartup",
    "PROMPT_MODES",
    "LIFECYCLES",
    "SESSION_STATES",
]


class Cap(StrEnum):
    """What a provider can do beyond start/stop/observe."""

    ATTACH = "attach"
    PEEK = "peek"
    NUDGE = "nudge"
    ACTIVITY = "activity"
    RELAUNCH = "relaunch"


#: Valid ``SessionSpec.prompt_mode`` values (harness ``prompt_mode`` field).
PROMPT_MODES: frozenset[str] = frozenset({"arg", "flag", "none"})

#: Valid ``SessionSpec.lifecycle`` / ``sessions.lifecycle`` values.
LIFECYCLES: frozenset[str] = frozenset({"task", "named"})

#: Valid ``sessions.state`` values.  "stalled" is *derived* (lease TTL vs
#: ``last_activity``) and never stored — see design §4.3.
SESSION_STATES: frozenset[str] = frozenset(
    {"starting", "running", "draining", "stopped", "sleeping", "quarantined"}
)


@dataclass(frozen=True)
class DialogRule:
    """One startup-dialog dismissal rule, sourced from the harness markdown.

    The *runner* (``src/sessions/dialogs.py``) needs a pane to scrape and so
    lands with the tmux provider; the rule shape lives here because
    :class:`SessionSpec` carries it and the spec builder composes it.

    Attributes
    ----------
    name:
        Identifier for logs/events (e.g. ``"trust-folder"``).
    pattern:
        Substring or regex matched against the pane capture.
    keys:
        tmux ``send-keys`` arguments sent in order when *pattern* matches.
    is_regex:
        Treat *pattern* as a regex rather than a substring.
    quarantine:
        When True, matching this dialog is a terminal condition (the
        rate-limit dialog answers *Stop*, then the session is quarantined
        with ``sleep_reason=rate_limit``) rather than a dismissal.
    once:
        Fire at most once per startup.
    """

    name: str
    pattern: str
    keys: tuple[str, ...] = ()
    is_regex: bool = False
    quarantine: bool = False
    once: bool = True


@dataclass(frozen=True)
class SessionSpec:
    """Immutable launch description: profile + harness + task → one session.

    Built by :class:`~src.sessions.spec.SessionSpecBuilder`; consumed by
    :meth:`SessionProvider.start`.  Providers must not mutate it and must
    not reach back into the database — everything a launch needs is here.
    """

    session_name: str
    work_dir: str
    #: argv.  The agent is the initial process; never typed into a shell.
    command: tuple[str, ...]
    #: Full child environment (already scrubbed, ``AQ_*`` markers merged).
    env: dict[str, str] = field(default_factory=dict)
    #: Bootstrap prompt text.  ``None`` when ``prompt_mode == "none"``.
    prompt: str | None = None
    prompt_mode: str = "arg"
    ready_delay_ms: int = 0
    ready_prompt_prefix: str | None = None
    process_names: tuple[str, ...] = ()
    lifecycle: str = "task"
    dialogs: tuple[DialogRule, ...] = ()
    skip_escape_before_enter: bool = True
    #: Files the provider writes into ``work_dir`` before launch, as
    #: ``relative_path -> content``.  Prompt overflow files and harness hook
    #: material ride here so the spec stays the single launch description
    #: and providers stay free of harness knowledge.
    files: tuple[tuple[str, str], ...] = ()
    #: Instance token echoed onto the handle; the kill fence compares it.
    instance_token: str = ""


@dataclass(frozen=True)
class SessionHandle:
    """The provider-side identity of one live session.

    ``instance_token`` is the kill fence: a provider must compare it against
    the observed process before signalling, so a same-named successor is
    never hit when a PID or a session name is recycled.
    """

    name: str
    provider: str
    instance_token: str


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionError(Exception):
    """Base for every typed session-provider failure."""


class CapabilityUnsupported(SessionError):
    """Raised when an operation the provider does not advertise is called.

    Providers raise this rather than silently no-op'ing so a caller that
    forgot to gate on :class:`Cap` fails loudly in tests instead of quietly
    losing a nudge in production.
    """

    def __init__(self, provider: str, cap: Cap | str):
        self.provider = provider
        self.cap = str(cap)
        super().__init__(f"provider {provider!r} does not support {self.cap!r}")


class NotSubmitted(SessionError):
    """A nudge was pasted but Enter was not confirmed — caller re-queues.

    Never treat this as delivered.  Enter races bracketed paste, and
    assuming delivery is how a stall ladder silently stops climbing.
    """


class PartialListError(SessionError):
    """``list_running`` failed mid-way; carries whatever was enumerated.

    Callers must treat the result as *incomplete*, not authoritative:
    unknown is not dead, and adoption/reaping defer for that prefix.
    """

    def __init__(self, partial: list[SessionHandle], cause: Exception | None = None):
        self.partial = list(partial)
        self.cause = cause
        super().__init__(f"partial session listing ({len(self.partial)} known): {cause}")


class SessionDiedDuringStartup(SessionError):
    """The session's process died before it became ready.

    ``start_stderr_path`` points at the captured last output so the failure
    is diagnosable without a live pane.
    """

    def __init__(self, name: str, start_stderr_path: str | None = None, detail: str = ""):
        self.name = name
        self.start_stderr_path = start_stderr_path
        self.detail = detail
        super().__init__(
            f"session {name!r} died during startup"
            + (f": {detail}" if detail else "")
            + (f" (see {start_stderr_path})" if start_stderr_path else "")
        )


# ---------------------------------------------------------------------------
# The ABC
# ---------------------------------------------------------------------------


class SessionProvider(ABC):
    """Create, observe, nudge and kill agent sessions.

    Implementations are constructed with ``(config)`` by
    :class:`~src.sessions.SessionProviderRegistry` and must be safe to call
    concurrently from the reconciler and from command handlers.
    """

    name: ClassVar[str] = ""
    capabilities: ClassVar[frozenset[Cap]] = frozenset()

    def supports(self, cap: Cap) -> bool:
        """True when *cap* is advertised.  The only correct gate."""
        return cap in self.capabilities

    def _require(self, cap: Cap) -> None:
        if cap not in self.capabilities:
            raise CapabilityUnsupported(self.name, cap)

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    async def start(self, spec: SessionSpec) -> SessionHandle:
        """Launch detached and return once the session is created.

        Raises :class:`SessionDiedDuringStartup` when the process died
        before readiness.  A readiness *timeout* with a live process is not
        an error — some harnesses paint slowly.
        """

    @abstractmethod
    async def stop(self, h: SessionHandle, *, grace: float = 2.0) -> None:
        """SIGTERM the process tree, wait *grace*, then SIGKILL.

        Idempotent: stopping an already-stopped session is a no-op, not an
        error.  Every signal is fenced by ``h.instance_token``.

        ``grace`` defaults to 2 s deliberately — 100 ms orphans Claude.
        """

    @abstractmethod
    async def interrupt(self, h: SessionHandle) -> None:
        """Send the interactive interrupt (C-c) without killing the session."""

    # -- observation -------------------------------------------------------

    @abstractmethod
    async def is_running(self, h: SessionHandle) -> bool:
        """True when the runtime artifact (pane / process record) exists."""

    @abstractmethod
    async def process_alive(self, h: SessionHandle, process_names: tuple[str, ...]) -> bool:
        """True when the agent process itself is alive.

        Distinct from :meth:`is_running`.  On a failed secondary probe
        (``ps`` unavailable) implementations return **True** — optimistic
        alive; never reap on a failed probe.
        """

    @abstractmethod
    async def list_running(self, prefix: str) -> list[SessionHandle]:
        """Enumerate live sessions whose name starts with *prefix*.

        Raises :class:`PartialListError` (carrying the partial result) when
        enumeration could not be completed.
        """

    @abstractmethod
    async def last_activity(self, h: SessionHandle) -> float | None:
        """Unix ts of last observed output, or ``None`` when unknown.

        Implementations discount their own pokes: activity caused by a nudge
        we just sent must not be reported as agent progress.
        """

    @abstractmethod
    async def peek(self, h: SessionHandle, lines: int = 60) -> str:
        """Last *lines* of visible output (``""`` when unsupported)."""

    # -- interaction -------------------------------------------------------

    @abstractmethod
    async def nudge(self, h: SessionHandle, text: str) -> None:
        """Inject *text* and submit it.

        Raises :class:`NotSubmitted` when submission could not be
        confirmed, and :class:`CapabilityUnsupported` when the provider has
        no input channel at all.
        """

    @abstractmethod
    async def attach_command(self, h: SessionHandle) -> str:
        """Shell command a human runs to attach (``""`` when unsupported)."""

    # -- provider-side metadata -------------------------------------------

    @abstractmethod
    async def set_meta(self, h: SessionHandle, key: str, value: str) -> None:
        """Store a small string on the session itself.

        The drain-ack handshake rides here: the agent's ``aq session
        drain-ack`` marks the session, and the reconciler reads it back.

        **Durability is deliberately not part of this contract, and it does
        differ per provider.**  A tmux provider implements this with
        ``set-environment`` on the session, which the *server* owns — so the
        value survives a daemon restart.  :class:`SubprocessProvider` keeps
        it in a per-process dict inside the daemon, so a restart loses it
        and every key reads back ``None``.

        That asymmetry is acceptable rather than accidental: the only thing
        riding this channel is the drain-ack, and a lost ack self-heals.
        The agent is idle with its task already closed, so the next tick's
        new orphan step (live session, task no longer open) drains it on the
        task state instead of the marker.  Do not put anything here that
        cannot survive being silently forgotten.
        """

    @abstractmethod
    async def get_meta(self, h: SessionHandle, key: str) -> str | None:
        """Read back a value set by :meth:`set_meta` (``None`` when absent).

        See :meth:`set_meta` on durability: ``None`` may mean "never set" or
        "set, then the daemon restarted".  Callers must treat the absence of
        a marker as no information, never as a negative answer.
        """
