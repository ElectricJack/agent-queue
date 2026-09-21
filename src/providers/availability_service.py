"""The daemon side of provider availability (``docs/specs/provider-failover.md``).

:mod:`src.providers.availability` is the pure reducer; this module is
everything around it that touches the world:

* **the snapshot** — ``Orchestrator.provider_availability`` holds every
  provider's row in memory, loaded at start and updated on every write, so
  the scheduler, pool sizing, the pre-launch check and claim admission never
  query the database for it (D7);
* **collectors** — ``record`` is the one entry point every evidence source
  calls (D2); ``record_startup_death`` / ``record_exit`` /
  ``note_session_authenticated`` turn the orchestrator's own observations
  into typed evidence;
* **the clock** — ``tick`` re-evaluates every provider once per orchestrator
  cycle (override expiry, reset clocks, usage snapshots, level decay) and
  schedules the auth probe (D5);
* **the canary** — while a provider is on probation at most one launch is
  admitted until one succeeds (D4);
* **announcing** — a change of effective state is persisted with its
  transition, emitted as ``provider.state_changed`` and, when it crosses
  between halves, as ``notify.provider_state`` plus one idempotent message
  to the global supervisor and the human (D19);
* **escalating** — when a human must act (a logged-out CLI, a provider
  failing for too long, every provider down) one durable escalation per
  outage, filed under the project with the most stranded work and resolved
  when the condition clears (D19's active Discord half).  The passive half,
  the hourly digest's provider facts, is read straight from the transition
  log by ``collect_digest_activity``.

Mode (D22): ``off`` records nothing and suppresses nothing; ``observe``
records, derives, persists and announces but never refuses a launch;
``enforce`` also suppresses launches against an unavailable provider.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, fields
from typing import Any

from src.models import TaskStatus
from src.providers.availability import (
    AUTH_PROBE,
    AVAILABLE,
    DISABLED,
    EXHAUSTED,
    EXIT_RATE_LIMIT,
    FAILING,
    LAUNCH_FAILURE,
    LAUNCH_SUCCESS,
    PROBE_AUTHENTICATED,
    PROBE_CANNOT_TELL,
    PROBE_NOT_AUTHENTICATED,
    SIGNAL_AUTH,
    STARTUP_DIALOG,
    UNAUTHENTICATED,
    UNAVAILABLE,
    USAGE_SNAPSHOT,
    Evidence,
    ProviderAvailability,
    Reduction,
    Transition,
    clear_override,
    dialog_from_startup_death,
    half,
    provider_key,
    reduce,
    set_override,
    usage_view,
    vendor_of,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CANARY_TIMEOUT_SECONDS",
    "ESCALATION_SOURCE_KIND",
    "ProviderAvailabilityService",
    "availability_from_row",
    "availability_to_row",
]

#: A probation canary that neither succeeded nor failed within this long is
#: presumed lost (a launch that hung, a daemon hiccup) and the next launch
#: may be the canary instead.  Generous: a slow first turn is normal.
CANARY_TIMEOUT_SECONDS = 600.0
#: How long the set of tracked providers is cached between re-reads.
_TRACKED_TTL_SECONDS = 60.0
#: Sessions already credited with a ``launch_success``, bounded.
_SEEN_SESSIONS_MAX = 4096
#: The principal system writes are attributed to.
SYSTEM_ACTOR = "system"
#: Where the state-change messages come from (D19).
NOTIFY_FROM = ("system", "playbook:provider-failover")
#: ``escalations.source_kind`` of every provider outage incident (D19).  Its
#: ``source_identity`` is ``<provider>:<generation>`` -- the generation the
#: outage had when the incident was raised -- and its ``incident_key`` is
#: ``provider:<provider>:<generation>``: the key is unique per project for all
#: time, so a bare ``provider:<provider>`` would refuse the provider's second
#: outage in the same project forever.
ESCALATION_SOURCE_KIND = "provider_availability"
#: How often ``tick`` re-checks the escalations with no transition to prompt
#: it: long enough that a quiet fleet costs nothing, short enough that
#: "failing for 30 minutes" is noticed within a minute of the 30.
ESCALATION_RECONCILE_SECONDS = 60.0
_OPEN_ESCALATION_STATES = ("needs_human", "reply_received", "resolving")
#: What a derived hold can apply to (D18): queued work, not yet running.
QUEUED_STATUSES = (TaskStatus.DEFINED, TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.PAUSED)

_VENDOR_ALIASES = {"anthropic": "claude", "openai": "codex", "google": "gemini"}


# -- row <-> dict --------------------------------------------------------------


def availability_to_row(row: ProviderAvailability) -> dict[str, Any]:
    data = asdict(row)
    data["evidence"] = [dict(entry) for entry in row.evidence]
    return data


def availability_from_row(row: Mapping[str, Any]) -> ProviderAvailability:
    names = {f.name for f in fields(ProviderAvailability)}
    data = {k: v for k, v in row.items() if k in names}
    evidence = data.get("evidence") or []
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError:
            evidence = []
    data["evidence"] = tuple(dict(e) for e in evidence if isinstance(e, Mapping))
    for key in ("level", "consecutive_failures", "generation", "notified_generation"):
        if data.get(key) is None:
            data[key] = 0
    return ProviderAvailability(**data)


def stale_after_seconds(provider: str, app_config: Any) -> float:
    """The freshness horizon for *provider*'s usage snapshots.

    The same two numbers ``GET /api/providers/usage`` uses: Codex readings
    only advance while a Codex session runs, Claude's arrive from a timer.
    """
    providers = getattr(app_config, "providers", None)
    if provider == "codex":
        return float(getattr(providers, "codex_stale_after_seconds", 4 * 3600.0))
    claude = getattr(providers, "claude", None)
    return float(getattr(claude, "stale_after_seconds", 1500.0))


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(float(ts)))


def _busiest_project(held: Iterable[Mapping[str, Any]]) -> str | None:
    """The project with the most *held* tasks, ties by id; ``None`` when nothing is held.

    Escalations are project-scoped, so a fleet-wide outage is filed where it
    strands the most work (D19).
    """
    counts: dict[str, int] = {}
    for entry in held:
        project_id = entry.get("project_id")
        if project_id:
            counts[project_id] = counts.get(project_id, 0) + 1
    if not counts:
        return None
    return min(counts, key=lambda pid: (-counts[pid], pid))


class ProviderAvailabilityService:
    """Owns the availability snapshot and every write to it."""

    def __init__(
        self,
        *,
        db: Any = None,
        db_getter: Callable[[], Any] | None = None,
        config_getter: Callable[[], Any],
        bus: Any = None,
        harness_registry: Any = None,
        logins: Callable[[], Iterable[Any]] | None = None,
        probe: Callable[[Any, float], Awaitable[Any]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # A getter, so an owner that swaps its database handle (the
        # orchestrator in tests) is followed rather than pinned.
        self._db_getter = db_getter or (lambda: db)
        self._config_getter = config_getter
        self._bus = bus
        self.harness_registry = harness_registry
        self._logins = logins
        self._probe_impl = probe
        self._clock = clock
        self._rows: dict[str, ProviderAvailability] = {}
        self._persisted: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._probe_tasks: dict[str, asyncio.Task] = {}
        self._last_probe_started: dict[str, float] = {}
        self._canary: dict[str, float] = {}
        self._seen_sessions: dict[str, None] = {}
        self._last_usage: dict[str, tuple] = {}
        self._tracked: set[str] = set()
        self._tracked_at: float = 0.0
        self._flap_notice_at: dict[str, float] = {}
        #: Fire-and-forget evidence writes, held so the loop cannot collect
        #: them mid-flight and so a test (or shutdown) can wait for them.
        self._background: set[asyncio.Task] = set()
        self._loaded = False
        #: The re-route engine (:class:`src.providers.reroute.ProviderRerouteService`),
        #: wired by the orchestrator.  With it, a hold names the kind the
        #: next sweep would give it (D18); without it the three mechanism
        #: kinds below are the whole answer.
        self.reroute: Any = None
        #: Serialises escalation reconciles: the half-change notification, the
        #: command and the tick may all ask at once, and exactly one files.
        self._escalation_lock = asyncio.Lock()
        #: A transition happened since the last reconcile, so the next tick
        #: reconciles at once rather than waiting out the interval.
        self._escalation_dirty = True
        self._escalation_checked_at = 0.0
        #: Whether the last reconcile left a provider incident open; ``None``
        #: until one has run (a restart must look, not assume).
        self._escalations_open: bool | None = None
        #: Called with each transition that changed half; the orchestrator
        #: wires it to :meth:`notify_state_change`.  Overridable in tests.
        self.on_half_change: Callable[[Transition], Awaitable[Any]] | None = None

    @property
    def _db(self) -> Any:
        return self._db_getter()

    # -- configuration -------------------------------------------------------

    @property
    def app_config(self) -> Any:
        return self._config_getter()

    @property
    def config(self) -> Any:
        from src.config import ProviderFailoverConfig

        return getattr(self.app_config, "provider_failover", None) or ProviderFailoverConfig()

    @property
    def tracking(self) -> bool:
        return bool(getattr(self.config, "tracking", False))

    @property
    def enforcing(self) -> bool:
        return bool(getattr(self.config, "enforcing", False))

    def now(self) -> float:
        return float(self._clock())

    # -- keys ------------------------------------------------------------------

    def resolve_provider(self, name: str) -> str | None:
        """A provider key for *name*, accepting a vendor alias when unambiguous (D0)."""
        key = str(name or "").strip().lower()
        if not key:
            return None
        known = set(self._rows) | set(self._tracked)
        if key in known:
            return key
        alias = _VENDOR_ALIASES.get(key)
        if alias is not None:
            return alias
        for provider, row in self._rows.items():
            if row.vendor == key:
                return provider
        return key

    def provider_for_harness(self, harness_name: str | None, project_id: str | None = None) -> str:
        """The provider key a harness id draws on (``harness.base or harness.id``)."""
        name = str(harness_name or "").strip()
        if not name:
            return ""
        registry = self.harness_registry
        harness = registry.get(name, project_id) if registry is not None else None
        return provider_key(harness) if harness is not None else name

    def provider_for_profile(
        self, profile: Any, *, agent: Any = None, project_id: str | None = None
    ) -> str:
        """The provider a launch for *profile* (run by *agent*) would draw on."""
        harness = str(getattr(agent, "harness", "") or "") or str(
            getattr(profile, "harness", "") or ""
        )
        return self.provider_for_harness(harness, project_id)

    # -- snapshot reads (no I/O) ---------------------------------------------

    def row(self, provider: str) -> ProviderAvailability | None:
        return self._rows.get(provider)

    def rows(self) -> dict[str, ProviderAvailability]:
        return dict(self._rows)

    def effective_state(self, provider: str, now: float | None = None) -> str:
        row = self._rows.get(provider)
        if row is None or not self.tracking:
            return AVAILABLE
        return row.effective_state(self.now() if now is None else now)

    def is_unavailable(self, provider: str, now: float | None = None) -> bool:
        return bool(provider) and self.effective_state(provider, now) in UNAVAILABLE

    def suppresses(self, provider: str, now: float | None = None) -> bool:
        """True when nothing may launch against *provider* (enforce mode only)."""
        return self.enforcing and self.is_unavailable(provider, now)

    def unavailable_providers(self, now: float | None = None) -> frozenset[str]:
        if not self.tracking:
            return frozenset()
        at = self.now() if now is None else now
        return frozenset(p for p, row in self._rows.items() if row.effective_state(at) in UNAVAILABLE)

    def suppressed_providers(self, now: float | None = None) -> frozenset[str]:
        return self.unavailable_providers(now) if self.enforcing else frozenset()

    def llm_block_reason(self) -> str | None:
        """Why direct-path calls must fail fast right now, or ``None`` (D13a).

        ``LLMClient.availability_gate``.  Only in ``enforce`` mode, and only
        while the reserved ``llm`` key is in the unavailable half; once its
        ``until`` passes the reducer moves it to probation and the next call
        is the canary.
        """
        if not self.suppresses("llm"):
            return None
        at = self.now()
        row = self._rows.get("llm")
        reason = row.effective_reason(at) if row is not None else ""
        return f"provider llm is {self.effective_state('llm', at)}: {reason}".rstrip(": ")

    def admit_launch(self, provider: str, *, now: float | None = None) -> tuple[bool, str | None]:
        """May a session start against *provider* right now?

        Refuses an unavailable provider, and on probation admits exactly one
        launch (the canary) until one succeeds (D4).  Admission marks the
        canary as in flight; ``launch_success`` or any failure evidence for
        the provider releases it.
        """
        if not provider or not self.enforcing:
            return True, None
        at = self.now() if now is None else now
        row = self._rows.get(provider)
        if row is None:
            return True, None
        state = row.effective_state(at)
        if state in UNAVAILABLE:
            return False, (
                f"provider {provider} is {state}: {row.effective_reason(at)}"
            )
        if row.on_probation(at):
            started = self._canary.get(provider)
            if started is not None and at - started < CANARY_TIMEOUT_SECONDS:
                return False, (
                    f"provider {provider} is recovering; its canary launch is still in flight"
                )
            self._canary[provider] = at
        return True, None

    def release_canary(self, provider: str) -> None:
        self._canary.pop(provider, None)

    # -- lifecycle -------------------------------------------------------------

    def _lock(self, provider: str) -> asyncio.Lock:
        lock = self._locks.get(provider)
        if lock is None:
            lock = self._locks[provider] = asyncio.Lock()
        return lock

    async def load(self) -> None:
        """Read every stored row into the snapshot.  State survives a restart."""
        try:
            stored = await self._db.list_provider_availability()
        except Exception:
            logger.warning("provider availability: could not load stored state", exc_info=True)
            return
        for raw in stored:
            try:
                row = availability_from_row(raw)
            except Exception:
                logger.warning("provider availability: unreadable row %r", raw, exc_info=True)
                continue
            self._rows[row.provider] = row
            self._persisted.add(row.provider)
        self._loaded = True

    async def initialize(self) -> None:
        """Load stored state, seed rows for tracked providers, probe once (D5)."""
        await self.load()
        if not self.tracking:
            return
        for provider in await self.tracked_providers(force=True):
            if provider not in self._rows:
                await self._apply(provider, None)
            self._start_probe(provider, reason="startup")

    async def tracked_providers(self, *, force: bool = False) -> set[str]:
        """Provider keys some worker or role profile launches against, plus stored rows."""
        now = self.now()
        if not force and now - self._tracked_at < _TRACKED_TTL_SECONDS:
            return set(self._tracked) | set(self._rows)
        tracked: set[str] = set()
        try:
            profiles = await self._db.list_profiles()
        except Exception:
            logger.debug("provider availability: could not list profiles", exc_info=True)
            profiles = []
        for profile in profiles:
            if getattr(profile, "template", False):
                continue
            key = self.provider_for_profile(profile)
            if key:
                tracked.add(key)
        self._tracked = tracked
        self._tracked_at = now
        return tracked | set(self._rows)

    async def tick(self) -> list[Transition]:
        """Re-evaluate every provider against the clock; start due probes."""
        if not self.tracking:
            return []
        if not self._loaded:
            await self.load()
        transitions: list[Transition] = []
        cfg = self.config
        now = self.now()
        for provider in sorted(await self.tracked_providers()):
            if provider == "llm":
                evidence = None
            else:
                evidence = await self._usage_evidence(provider, now)
            result = await self._apply(provider, evidence)
            if result is not None and result.transition is not None:
                transitions.append(result.transition)
            if provider == "llm":
                continue
            row = self._rows.get(provider)
            if row is None:
                continue
            last = self._last_probe_started.get(provider, 0.0)
            if row.state == UNAUTHENTICATED:
                if now - last >= float(cfg.recovery.auth_probe_interval_seconds):
                    self._start_probe(provider, reason="recovery")
            elif (
                row.launchable(now)
                and cfg.auth_probe.interval_seconds > 0
                and now - last >= float(cfg.auth_probe.interval_seconds)
            ):
                self._start_probe(provider, reason="interval")
        await self._tick_escalations(now)
        return transitions

    async def close(self) -> None:
        tasks = [task for task in self._probe_tasks.values() if not task.done()]
        tasks += [task for task in self._background if not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- evidence --------------------------------------------------------------

    async def record(
        self,
        provider: str,
        kind: str,
        signal: str | None = None,
        *,
        detail: Mapping[str, Any] | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        observed_at: float | None = None,
    ) -> Reduction | None:
        """Fold one piece of evidence into *provider*'s state (D2).

        Every collector calls this.  Never raises: a failure to record
        evidence must not break the launch path that observed it.
        """
        if not provider or not self.tracking:
            return None
        evidence = Evidence(
            kind=kind,
            signal=signal,
            observed_at=self.now() if observed_at is None else float(observed_at),
            detail=dict(detail or {}),
            task_id=task_id,
            session_id=session_id,
            project_id=project_id,
        )
        if kind in (LAUNCH_SUCCESS, LAUNCH_FAILURE, STARTUP_DIALOG, EXIT_RATE_LIMIT):
            self.release_canary(provider)
        try:
            result = await self._apply(provider, evidence)
        except Exception:
            logger.exception("provider availability: could not record %s for %s", kind, provider)
            return None
        if (
            kind == STARTUP_DIALOG
            and signal == SIGNAL_AUTH
            and result is not None
            and result.row.state != UNAUTHENTICATED
        ):
            # Corroborate at once (D3 rule (a), D5): the probe's answer is
            # what lets one login dialog trip the provider.
            self._start_probe(provider, reason="corroborate")
        return result

    async def record_startup_death(
        self,
        exc: BaseException,
        *,
        harness: str | None,
        project_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        profile_id: str | None = None,
    ) -> tuple[str, str | None]:
        """Classify a ``SessionDiedDuringStartup`` into evidence; return ``(provider, signal)``.

        A quarantine dialog with a known meaning is ``startup_dialog``
        (strong); any other startup death is ``launch_failure`` (weak, D2).
        """
        provider = self.provider_for_harness(harness, project_id)
        dialog, signal = dialog_from_startup_death(exc)
        detail: dict[str, Any] = {}
        if dialog:
            detail["dialog"] = dialog
        if profile_id:
            detail["profile_id"] = profile_id
        if harness:
            detail["harness"] = harness
        if signal:
            await self.record(
                provider,
                STARTUP_DIALOG,
                signal,
                detail=detail,
                project_id=project_id,
                task_id=task_id,
                session_id=session_id,
            )
        else:
            text = str(getattr(exc, "detail", "") or "")
            if text:
                detail["detail"] = text[:200]
            await self.record(
                provider,
                LAUNCH_FAILURE,
                detail=detail,
                project_id=project_id,
                task_id=task_id,
                session_id=session_id,
            )
        return provider, signal

    def attributes_startup_death(self, exc: BaseException, harness: str | None,
                                 project_id: str | None = None) -> bool:
        """True when a startup death is the provider's fault, not the key's.

        A typed dialog signal, or any death while the provider is already
        unavailable.  Such a death must not arm the pool key quarantine or
        count toward ``sessions.max_restarts`` (D13).
        """
        if not self.tracking:
            return False
        _dialog, signal = dialog_from_startup_death(exc)
        if signal:
            return True
        return self.is_unavailable(self.provider_for_harness(harness, project_id))

    async def record_rate_limit_exit(self, session: Any, *, reason: str = "") -> None:
        """An ``exit_classifier`` ``RATE_LIMIT`` verdict (medium, D2)."""
        provider = self.provider_for_harness(
            getattr(session, "harness", None), getattr(session, "project_id", None)
        )
        await self.record(
            provider,
            EXIT_RATE_LIMIT,
            "usage",
            detail={"reason": reason[:200]} if reason else None,
            session_id=getattr(session, "id", None),
            task_id=getattr(session, "task_id", None),
            project_id=getattr(session, "project_id", None),
        )

    async def note_session_authenticated(self, session_id: str | None) -> None:
        """A session made its first authenticated API call: ``launch_success``.

        Called on every session-token validation; only the first per session
        does any work, so the hot path is one dict lookup.
        """
        if not session_id or not self.tracking or session_id in self._seen_sessions:
            return
        self._seen_sessions[session_id] = None
        if len(self._seen_sessions) > _SEEN_SESSIONS_MAX:
            for stale in list(self._seen_sessions)[: _SEEN_SESSIONS_MAX // 2]:
                self._seen_sessions.pop(stale, None)
        try:
            session = await self._db.get_session(session_id)
        except Exception:
            logger.debug("provider availability: session %s unreadable", session_id, exc_info=True)
            return
        if session is None or not getattr(session, "harness", None):
            return
        provider = self.provider_for_harness(session.harness, session.project_id)
        await self.record(
            provider,
            LAUNCH_SUCCESS,
            session_id=session_id,
            task_id=getattr(session, "task_id", None),
            project_id=getattr(session, "project_id", None),
        )

    def note_llm_outcome(self, signal: str, detail: dict) -> None:
        """``LLMClient.on_outcome``: direct-path calls as ``llm_call`` evidence (D13a).

        Successes are only recorded while the ``llm`` key is not plainly
        healthy -- a busy playbook makes many calls, and a success on an
        available provider changes nothing.
        """
        if not self.tracking:
            return
        row = self._rows.get("llm")
        if signal == "ok" and (
            row is None or (row.state == AVAILABLE and not row.consecutive_failures)
        ):
            return
        self._spawn(self.record("llm", "llm_call", signal, detail=detail))

    def _spawn(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        task = loop.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def note_session_authenticated_soon(self, session_id: str | None) -> None:
        """Fire-and-forget form for synchronous-ish hot paths (token validation)."""
        if not session_id or not self.tracking or session_id in self._seen_sessions:
            return
        self._spawn(self.note_session_authenticated(session_id))

    # -- operator ---------------------------------------------------------------

    async def set_state(
        self,
        provider: str,
        state: str,
        *,
        by: str,
        reason: str = "",
        until: float | None = None,
    ) -> Reduction:
        """``aq provider set-state``: ``disabled`` / ``available`` / ``auto`` (D6)."""
        now = self.now()
        async with self._lock(provider):
            row = self._rows.get(provider) or self._new_row(provider, now)
            if state == "auto":
                account, scoped = await self._usage(provider, now)
                result = clear_override(
                    row, by=by, now=now, config=self.config, account=account, scoped=scoped
                )
            else:
                result = set_override(row, state, until=until, by=by, reason=reason, now=now)
            await self._persist(provider, result)
        await self._announce(result.transition)
        return result

    async def recheck(self, provider: str) -> dict[str, Any]:
        """``aq provider recheck``: run the auth probe now and fold its answer in."""
        signal, detail = await self._run_probe(provider)
        result = None
        if signal is not None:
            result = await self.record(provider, AUTH_PROBE, signal, detail=detail)
        row = self._rows.get(provider)
        return {
            "provider": provider,
            "probe": signal or "not_probeable",
            "probe_detail": detail,
            "state": self.effective_state(provider),
            "transition": result.transition.payload() if result and result.transition else None,
            "row": row,
        }

    # -- probes -------------------------------------------------------------------

    def _login_for(self, provider: str) -> Any:
        if self._logins is not None:
            logins = list(self._logins())
        else:
            from src.install.logins import provider_logins

            logins = list(provider_logins())
        for login in logins:
            if getattr(login, "provider_id", None) == provider:
                return login
        return None

    def _start_probe(self, provider: str, *, reason: str) -> None:
        """Start the auth probe for *provider* in the background, never two at once."""
        running = self._probe_tasks.get(provider)
        if running is not None and not running.done():
            return
        if self._probe_impl is None and self._login_for(provider) is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._last_probe_started[provider] = self.now()

        async def _probe_and_record() -> None:
            signal, detail = await self._run_probe(provider)
            if signal is not None:
                await self.record(
                    provider, AUTH_PROBE, signal, detail={**detail, "trigger": reason}
                )

        self._probe_tasks[provider] = loop.create_task(_probe_and_record())

    async def wait_for_probes(self) -> None:
        """Let every in-flight probe and background evidence write finish."""
        while True:
            pending = [task for task in self._probe_tasks.values() if not task.done()]
            pending += [task for task in self._background if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run_probe(self, provider: str) -> tuple[str | None, dict[str, Any]]:
        """``(signal, detail)``; ``(None, {})`` when *provider* has no login probe.

        ``probe_login`` is a synchronous ``subprocess.run`` built for the
        installer, so it runs in a thread under a timeout (D5).  A timeout,
        an ``OSError`` or a missing CLI is ``cannot_tell`` — never evidence.
        """
        timeout = float(self.config.auth_probe.timeout_seconds)
        self._last_probe_started[provider] = self.now()
        if self._probe_impl is not None:
            try:
                answer = await asyncio.wait_for(self._probe_impl(provider, timeout), timeout * 3)
            except (TimeoutError, OSError, subprocess.SubprocessError):
                return PROBE_CANNOT_TELL, {"error": "probe did not answer"}
            if answer is None:
                return None, {}
            return str(answer), {}
        return await self._login_probe(provider, timeout)

    async def _login_probe(self, provider: str, timeout: float) -> tuple[str | None, dict[str, Any]]:
        """The real probe: ``probe_login`` for *provider*'s login adapter.

        Its own method so a test suite can stub the one place the daemon
        shells out to a provider CLI (``tests/conftest.py`` does).
        """
        login = self._login_for(provider)
        if login is None:
            return None, {}
        from src.install.logins import probe_login
        from src.install.providers import user_bin_aware_which

        def runner(command):
            return subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

        def _probe():
            return probe_login(login, which=user_bin_aware_which(), runner=runner)

        try:
            probe = await asyncio.wait_for(asyncio.to_thread(_probe), timeout * 3 + 5)
        except (TimeoutError, OSError, subprocess.SubprocessError) as exc:
            return PROBE_CANNOT_TELL, {"error": type(exc).__name__}
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("auth probe for %s failed", provider, exc_info=True)
            return PROBE_CANNOT_TELL, {"error": type(exc).__name__}
        detail = {"method": probe.method, "source": probe.source}
        if not probe.installed:
            return PROBE_CANNOT_TELL, {"error": "cli not installed"}
        signal = PROBE_AUTHENTICATED if probe.authenticated else PROBE_NOT_AUTHENTICATED
        return signal, {k: v for k, v in detail.items() if v}

    # -- internals -------------------------------------------------------------

    def _new_row(self, provider: str, now: float) -> ProviderAvailability:
        registry = self.harness_registry
        harness = registry.get(provider) if registry is not None else None
        vendor = vendor_of(harness) if harness is not None else vendor_of(provider)
        if provider == "llm":
            vendor = str(getattr(getattr(self.app_config, "llm", None), "provider", "") or "")
        return ProviderAvailability(provider=provider, vendor=vendor, since=now, updated_at=now)

    async def _usage(self, provider: str, now: float):
        if provider == "llm":
            return None, None
        try:
            rows = await self._db.latest_provider_usage(provider)
        except Exception:
            logger.debug("provider availability: usage read failed", exc_info=True)
            return None, None
        return usage_view(rows, now=now, stale_after=stale_after_seconds(provider, self.app_config))

    async def _usage_evidence(self, provider: str, now: float) -> Evidence | None:
        """A ``usage_snapshot`` ring entry when the account reading moved and matters.

        The reading itself reaches the reducer on every tick through
        ``account``; the ring entry is the audit record, written only when
        the reading changed and is high enough (or the provider unhealthy
        enough) for someone to ask about it later.
        """
        account, _scoped = await self._usage(provider, now)
        if account is None:
            return None
        key = (account.window, account.scope, account.used_percent, account.resets_at)
        if self._last_usage.get(provider) == key:
            return None
        self._last_usage[provider] = key
        row = self._rows.get(provider)
        degraded = float(self.config.usage.degraded_percent)
        if account.used_percent < degraded and (row is None or row.state == AVAILABLE):
            return None
        return Evidence(
            kind=USAGE_SNAPSHOT,
            observed_at=now,
            detail={
                "window": account.label(),
                "used_percent": account.used_percent,
                "resets_at": account.resets_at,
            },
        )

    def _peer_success_projects(self, provider: str, now: float) -> frozenset[str]:
        window = float(self.config.launch.window_seconds)
        projects: set[str] = set()
        for other, row in self._rows.items():
            if other == provider:
                continue
            for entry in row.evidence:
                if (
                    entry.get("kind") == LAUNCH_SUCCESS
                    and entry.get("project_id")
                    and now - float(entry.get("at") or 0.0) <= window
                ):
                    projects.add(str(entry["project_id"]))
        return frozenset(projects)

    async def _apply(self, provider: str, evidence: Evidence | None) -> Reduction | None:
        now = self.now()
        async with self._lock(provider):
            row = self._rows.get(provider) or self._new_row(provider, now)
            account, scoped = await self._usage(provider, now)
            result = reduce(
                row,
                evidence,
                now=now,
                config=self.config,
                account=account,
                scoped=scoped,
                peer_success_projects=self._peer_success_projects(provider, now),
            )
            await self._persist(provider, result)
        await self._announce(result.transition)
        return result

    async def _persist(self, provider: str, result: Reduction) -> None:
        if not result.changed and provider in self._persisted:
            self._rows[provider] = result.row
            return
        transition = None
        if result.transition is not None:
            transition = asdict(result.transition)
        await self._db.save_provider_availability(availability_to_row(result.row), transition)
        self._rows[provider] = result.row
        self._persisted.add(provider)

    async def _announce(self, transition: Transition | None) -> None:
        if transition is None:
            return
        self._escalation_dirty = True
        logger.warning(
            "provider %s: %s -> %s (%s) %s",
            transition.provider,
            transition.from_state,
            transition.to_state,
            transition.reason_code or "-",
            transition.reason,
        )
        if transition.to_state in UNAVAILABLE or transition.from_state in UNAVAILABLE:
            self.release_canary(transition.provider)
        payload = transition.payload()
        if self._bus is not None:
            try:
                await self._bus.emit("provider.state_changed", dict(payload))
            except Exception:
                logger.debug("provider.state_changed emit failed", exc_info=True)
        try:
            await self._db.log_event("provider.state_changed", payload=json.dumps(payload))
        except Exception:
            logger.debug("provider.state_changed log_event failed", exc_info=True)
        if not transition.half_changed:
            return
        if self._bus is not None:
            try:
                from src.notifications.events import ProviderStateEvent

                event = ProviderStateEvent(
                    severity="warning" if transition.to_state in UNAVAILABLE else "info",
                    provider=transition.provider,
                    vendor=transition.vendor,
                    from_state=transition.from_state,
                    to_state=transition.to_state,
                    reason_code=transition.reason_code,
                    reason=transition.reason,
                    since=transition.since,
                    until=transition.until,
                    generation=transition.generation,
                    remediation=self.remediation(transition.provider, transition.to_state),
                    message=self.headline(transition),
                )
                await self._bus.emit("notify.provider_state", event.model_dump(mode="json"))
            except Exception:
                logger.debug("notify.provider_state emit failed", exc_info=True)
        if self.on_half_change is not None:
            try:
                await self.on_half_change(transition)
            except Exception:
                logger.warning("provider state notification failed", exc_info=True)

    # -- the derived hold (D11 mechanism 2, D18) ---------------------------------

    async def hold_for(
        self,
        task: Any,
        *,
        project: Any = None,
        profiles: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Why *task* is held by its provider, or ``None`` when it is not.

        A hold is derived, never a status: a queued task whose effective
        profile (:meth:`queued_route`) is on a provider nothing may launch
        against keeps its status and gains this explanation.  ``kind`` says
        why it is not moving -- with the re-route engine wired, whatever the
        next sweep would decide (``provider_pinned``,
        ``awaiting_failover_capacity`` with ``ahead``, ...;
        :data:`src.providers.reroute.HOLD_KINDS`); without it,
        ``all_providers_unavailable``, ``no_equivalent_rung`` (a role
        profile, or a class no other provider has a rung for -- the Astra
        case), or ``failover_inactive`` (the re-route engine is not active).
        *project* and *profiles* are snapshots a caller walking many tasks
        already has; each is read here when not given.
        """
        from src.profiles.catalog import worker_route

        if not self.enforcing:
            return None
        if getattr(task, "status", None) not in QUEUED_STATUSES or getattr(
            task, "assigned_agent_id", None
        ):
            return None
        if project is None:
            try:
                project = await self._db.get_project(task.project_id)
            except Exception:  # an unreadable project cannot be explained
                logger.debug("provider hold: project unreadable", exc_info=True)
                return None
        if profiles is None:
            try:
                profiles = {p.id: p for p in await self._db.list_profiles()}
            except Exception:  # without profiles there is no provider to name
                logger.debug("provider hold: profiles unreadable", exc_info=True)
                return None
        resolved = self.queued_route(task, project=project, profiles=profiles)
        if resolved is None:
            return None
        profile, provider = resolved
        if not self.suppresses(provider):
            return None
        now = self.now()
        row = self._rows.get(provider)
        session_providers = self._session_providers(profiles.values())
        route = worker_route(
            profile.id,
            harness=getattr(profile, "harness", ""),
            default_class=task.intelligence_class or getattr(profile, "default_class", ""),
            lifecycle=getattr(profile, "lifecycle", "task"),
            template=bool(getattr(profile, "template", False)),
            read_only=bool(getattr(profile, "read_only", False)),
        )
        ahead = None
        detail = ""
        planned = None
        if self.reroute is not None and task.profile_id:
            # The planner over the provider's queue: the kind (and ``ahead``)
            # the next sweep would give this task.  An unrouted task follows
            # the derived project default instead (D13), so it has no plan.
            try:
                planned = await self.reroute.hold_kind(task)
            except Exception:  # an explanation must never break explain
                logger.debug("provider hold: planner failed", exc_info=True)
        if planned is not None:
            kind = str(planned.get("kind") or "failover_inactive")
            ahead = planned.get("ahead")
            detail = str(planned.get("detail") or "")
        elif session_providers and all(self.is_unavailable(p, now) for p in session_providers):
            kind = "all_providers_unavailable"
        elif route is None or not self._has_equivalent_rung(route[1], provider, profiles):
            kind = "no_equivalent_rung"
        else:
            kind = "failover_inactive"
        state = self.effective_state(provider, now)
        return {
            "provider": provider,
            "vendor": row.vendor if row is not None else "",
            "state": state,
            "since": row.effective_since(now) if row is not None else None,
            "until": row.effective_until(now) if row is not None else None,
            "kind": kind,
            "ahead": ahead,
            "detail": detail,
            "profile_id": profile.id,
            "reason": row.effective_reason(now) if row is not None else "",
            "remediation": self.remediation(provider, state),
        }

    def queued_route(
        self, task: Any, *, project: Any, profiles: Mapping[str, Any]
    ) -> tuple[Any, str] | None:
        """``(profile, provider)`` a queued, unassigned *task* would launch on, else ``None``.

        The one resolution the derived hold (:meth:`hold_for`) and the held
        count (:meth:`affected` -- ``aq provider status``, the state-change
        notice, the escalation) share, so the two cannot disagree: the
        task's own profile or, for an unrouted task, the project default --
        followed to its equivalent rung while the default's provider is down
        (D13), so such a task is held only when there is none.  ``None`` for
        a task that is running or assigned, or that resolves to no known
        profile.  Synchronous and I/O-free: *project* and *profiles* are
        snapshots the caller already has.
        """
        if getattr(task, "status", None) not in QUEUED_STATUSES:
            return None
        if getattr(task, "assigned_agent_id", None):
            return None
        profile_id = task.profile_id or getattr(project, "default_profile_id", None)
        if not profile_id:
            return None
        if not task.profile_id and self.reroute is not None:
            profile_id = self.reroute.resolve_default_profile_id(
                profile_id, profiles, project_id=task.project_id
            )
        profile = profiles.get(profile_id)
        if profile is None:
            return None
        return profile, self.provider_for_profile(profile, project_id=task.project_id)

    def _session_providers(self, profiles: Iterable[Any]) -> set[str]:
        """Every provider some enabled profile launches against.

        Including ones that never produced evidence and so have no row yet,
        which are available by definition.  The ``llm`` key is not a session
        provider: the direct path being down strands routing, not execution
        (D15), so it never counts toward "every provider".
        """
        return {
            self.provider_for_profile(p)
            for p in profiles
            if getattr(p, "enabled", True) and not getattr(p, "template", False)
        } - {"", "llm"}

    def _has_equivalent_rung(
        self, class_id: str, provider: str, profiles: Mapping[str, Any]
    ) -> bool:
        """Does an enabled worker profile run *class_id* on another provider?"""
        from src.profiles.catalog import worker_route

        for other in profiles.values():
            if not getattr(other, "enabled", True):
                continue
            route = worker_route(
                other.id,
                harness=getattr(other, "harness", ""),
                default_class=getattr(other, "default_class", ""),
                lifecycle=getattr(other, "lifecycle", "task"),
                template=bool(getattr(other, "template", False)),
                read_only=bool(getattr(other, "read_only", False)),
            )
            if route is None or route[1] != class_id:
                continue
            if self.provider_for_profile(other) != provider:
                return True
        return False

    # -- notification (D19, state half) -----------------------------------------

    async def affected(self, provider: str) -> dict[str, Any]:
        """Queued work and role profiles on *provider*: what an outage strands.

        ``held`` is every queued, unassigned task whose :meth:`queued_route`
        is on *provider* -- in ``enforce`` mode, with *provider* unavailable,
        exactly the tasks :meth:`hold_for` holds on it, whatever their queued
        status; in ``observe`` mode, the tasks it would hold.  ``roles`` are
        the role (non-worker) profiles on *provider*, which cannot launch.
        """
        from src.profiles.catalog import worker_route

        roles: list[str] = []
        profiles: dict[str, Any] = {}
        try:
            for profile in await self._db.list_profiles():
                profiles[profile.id] = profile
                if self.provider_for_profile(profile) != provider:
                    continue
                if worker_route(
                    profile.id,
                    harness=getattr(profile, "harness", ""),
                    default_class=getattr(profile, "default_class", ""),
                    lifecycle=getattr(profile, "lifecycle", "task"),
                    template=bool(getattr(profile, "template", False)),
                    read_only=bool(getattr(profile, "read_only", False)),
                ) is None:
                    roles.append(profile.id)
        except Exception:
            logger.debug("provider availability: could not list profiles", exc_info=True)
        held: list[dict[str, Any]] = []
        try:
            projects = {p.id: p for p in await self._db.list_projects()}
            for status in QUEUED_STATUSES:
                for task in await self._db.list_tasks(status=status):
                    resolved = self.queued_route(
                        task, project=projects.get(task.project_id), profiles=profiles
                    )
                    if resolved is None or resolved[1] != provider:
                        continue
                    held.append(
                        {
                            "task_id": task.id,
                            "project_id": task.project_id,
                            "profile_id": resolved[0].id,
                        }
                    )
        except Exception:
            logger.debug("provider availability: could not count held tasks", exc_info=True)
        return {"roles": sorted(roles), "held": held}

    async def notify_state_change(
        self, provider: str, generation: int | None = None
    ) -> dict[str, Any]:
        """Tell the global supervisor and the human that *provider* changed half.

        Idempotent per ``(provider, generation)`` through
        ``claim_provider_notification``: the transition that caused it, an
        event replay and a periodic timer (the ``provider-failover``
        playbook, ``bold-rapids.3``) never message twice.  Flap-damped per
        ``notify.flap_threshold`` over ``recovery.flap_window_seconds``.

        Also reconciles *provider*'s escalation (``escalation`` in the
        result), whatever the messages did: an escalation is keyed by the
        outage rather than by the message, so it is just as idempotent, and a
        damped or already-sent message must not stop a human being paged.
        """
        result = await self._notify_messages(provider, generation)
        if result.get("success"):
            outcomes = await self.reconcile_escalations(provider)
            result["escalation"] = outcomes[0] if outcomes else None
        return result

    async def _notify_messages(
        self, provider: str, generation: int | None = None
    ) -> dict[str, Any]:
        """The message half of :meth:`notify_state_change`."""
        cfg = self.config
        row = self._rows.get(provider)
        if row is None:
            return {"success": False, "error": f"unknown provider {provider!r}"}
        generation = row.generation if generation is None else int(generation)
        if not getattr(cfg.notify, "supervisor", True):
            return {"success": True, "outcome": "disabled", "provider": provider}
        messages_cfg = getattr(self.app_config, "messages", None)
        if messages_cfg is not None and not getattr(messages_cfg, "enabled", True):
            return {"success": True, "outcome": "messages_disabled", "provider": provider}
        now = self.now()
        window = float(cfg.recovery.flap_window_seconds)
        try:
            recent = await self._db.list_provider_transitions(
                provider, since=now - window, limit=200
            )
        except Exception:
            logger.debug("provider availability: transitions unreadable", exc_info=True)
            recent = []
        this = next((t for t in recent if int(t.get("generation") or -1) == generation), None)
        if this is not None and half(this["from_state"]) == half(this["to_state"]):
            return {"success": True, "outcome": "not_a_half_change", "provider": provider}
        half_changes = [t for t in recent if half(t["from_state"]) != half(t["to_state"])]
        flapping = len(half_changes) > int(cfg.notify.flap_threshold)
        if flapping:
            last_notice = self._flap_notice_at.get(provider)
            if last_notice is not None and now - last_notice < window:
                return {"success": True, "outcome": "flap_damped", "provider": provider}
        if not await self._db.claim_provider_notification(provider, generation):
            return {"success": True, "outcome": "already_notified", "provider": provider}
        view = self.describe(provider, now)
        affected = await self.affected(provider)
        if flapping:
            self._flap_notice_at[provider] = now
            subject = f"Provider {provider} is flapping"
            body = (
                f"Provider {provider} is flapping ({len(half_changes)} changes in "
                f"{window / 3600:g} h); per-change messages are paused until it holds one "
                f"state for a full window. Now: {view['state']} — {view['reason']}.\n"
                f"`aq provider status {provider} --verbose` shows the evidence."
            )
        else:
            subject = f"Provider {provider}: {view['state']}"
            lines = [
                f"Provider {provider} ({view['vendor'] or '-'}) is now {view['state']}.",
                f"Reason: {view['reason'] or '-'}",
                f"Since: {_fmt_ts(view['since'])}",
                f"Expected recovery: {_fmt_ts(view['until']) if view['until'] else 'none known'}",
            ]
            if view["remediation"]:
                lines.append(f"Remediation: {view['remediation']}")
            if view["state"] in UNAVAILABLE:
                lines.append(
                    f"Launches against {provider} are "
                    + ("suppressed." if self.enforcing else "NOT suppressed (observe mode).")
                )
                lines.append(f"Queued tasks routed to it (held): {len(affected['held'])}")
                if affected["roles"]:
                    lines.append(
                        "Role profiles on this provider cannot launch: "
                        + ", ".join(affected["roles"])
                    )
            lines.append(
                f"Commands: `aq provider status {provider}`, `aq provider reroute --dry-run`, "
                f"`aq provider set-state {provider} disabled|available|auto --reason ...`, "
                f"`aq provider recheck {provider}`."
            )
            body = "\n".join(lines)
        from_kind, from_id = NOTIFY_FROM
        sent = []
        for to_kind, to_id in (("session", "supervisor-global"), ("user", "dashboard")):
            try:
                message = await self._db.create_message(
                    project_id=None,
                    from_kind=from_kind,
                    from_id=from_id,
                    to_kind=to_kind,
                    to_id=to_id,
                    subject=subject,
                    body=body,
                )
                sent.append(getattr(message, "id", None))
            except Exception:
                logger.warning("provider availability: message to %s:%s failed", to_kind, to_id,
                               exc_info=True)
        return {
            "success": True,
            "outcome": "flapping" if flapping else "notified",
            "provider": provider,
            "generation": generation,
            "message_ids": sent,
            "held": len(affected["held"]),
            "roles": affected["roles"],
        }

    # -- escalation (D19, the active Discord half) ------------------------------------

    def escalation_verdict(
        self, provider: str, session_providers: Iterable[str], now: float | None = None
    ) -> tuple[str, str] | None:
        """``(trigger, severity)`` when a human must act about *provider*, else ``None``.

        Only when there is something to decide (D19): the CLI is logged out;
        launches have been ``failing`` for longer than
        ``notify.escalate_failing_after_seconds``; or every session provider
        is unavailable and none is due back within
        ``notify.escalate_all_down_after_seconds`` (D15).  Never for
        ``exhausted`` on its own -- a known reset leaves nothing to decide --
        nor for ``disabled``, which an operator chose.  Severity is
        ``critical`` while everything is down, ``high`` otherwise.  The
        ``llm`` key has no queued tasks to strand, so it is never filed.
        """
        if not self.tracking or provider == "llm":
            return None
        row = self._rows.get(provider)
        if row is None:
            return None
        at = self.now() if now is None else now
        state = row.effective_state(at)
        if state not in UNAVAILABLE or state == DISABLED:
            return None
        notify = self.config.notify
        session = set(session_providers)
        all_down = bool(session) and all(self.is_unavailable(p, at) for p in session)
        severity = "critical" if all_down else "high"
        if state == UNAUTHENTICATED and notify.escalate_unauthenticated:
            return UNAUTHENTICATED, severity
        if state == FAILING and at - row.effective_since(at) >= float(
            notify.escalate_failing_after_seconds
        ):
            return FAILING, severity
        if all_down and provider in session:
            untils = [
                until
                for p in session
                if (other := self._rows.get(p)) is not None
                and (until := other.effective_until(at)) is not None
            ]
            earliest = min(untils) if untils else None
            if earliest is None or earliest - at > float(notify.escalate_all_down_after_seconds):
                return "all_providers_unavailable", severity
        return None

    async def _tick_escalations(self, now: float) -> None:
        """Reconcile at once after a transition, otherwise once a minute.

        A quiet fleet -- nothing unavailable and no incident left open -- is
        answered from memory, so the five-second cycle costs no query.
        """
        if not self._escalation_dirty and now - self._escalation_checked_at < (
            ESCALATION_RECONCILE_SECONDS
        ):
            return
        self._escalation_dirty = False
        self._escalation_checked_at = now
        if self._escalations_open is False and not self.unavailable_providers(now):
            return
        await self.reconcile_escalations()

    async def reconcile_escalations(self, provider: str | None = None) -> list[dict[str, Any]]:
        """Bring the provider escalations in line with the snapshot (D19).

        For each provider (or just *provider*): file one escalation when a
        human must act and none is open, keep an open one's severity current,
        and resolve it when the condition has cleared.  Idempotent -- the
        half-change notification, an event replay, the command and the
        periodic tick may all call it; an open incident is found by its
        source kind wherever it was filed, and a generation whose incident a
        human already closed is never filed again.  Never raises.
        """
        if not self.tracking:
            return []
        async with self._escalation_lock:
            now = self.now()
            try:
                open_rows = await self._db.list_escalations(
                    source_kind=ESCALATION_SOURCE_KIND,
                    states=list(_OPEN_ESCALATION_STATES),
                    limit=500,
                )
                session = self._session_providers(await self._db.list_profiles())
            except Exception:
                logger.warning("provider escalations: could not read state", exc_info=True)
                return []
            by_provider: dict[str, list[dict[str, Any]]] = {}
            for incident in open_rows:
                key = str(incident.get("source_identity") or "").rpartition(":")[0]
                by_provider.setdefault(key, []).append(incident)
            if provider is not None:
                wanted = [provider]
            else:
                wanted = sorted(set(self._rows) | set(by_provider))
            outcomes = []
            for key in wanted:
                try:
                    outcome = await self._reconcile_escalation(
                        key, by_provider.get(key, []), session, now
                    )
                except Exception:
                    logger.warning(
                        "provider escalations: reconcile for %s failed", key, exc_info=True
                    )
                    outcome = {"provider": key, "outcome": "error"}
                if outcome.get("outcome") in ("created", "open"):
                    by_provider[key] = [outcome]
                elif outcome.get("outcome") == "resolved":
                    by_provider.pop(key, None)
                outcomes.append(outcome)
            if provider is None:
                self._escalations_open = any(by_provider.values())
            return outcomes

    async def _reconcile_escalation(
        self,
        provider: str,
        open_incidents: list[dict[str, Any]],
        session: set[str],
        now: float,
    ) -> dict[str, Any]:
        verdict = self.escalation_verdict(provider, session, now)
        if verdict is None:
            if not open_incidents:
                return {"provider": provider, "outcome": "not_needed"}
            resolved = []
            for incident in open_incidents:
                row = await self._resolve_escalation(incident, provider, now)
                if row is not None:
                    resolved.append(row["id"])
            if len(resolved) < len(open_incidents):
                # A reply or a supervisor turn moved one underneath us: the
                # next reconcile re-reads it and tries again.
                return {"provider": provider, "outcome": "open", "escalation_ids": resolved}
            return {"provider": provider, "outcome": "resolved", "escalation_ids": resolved}
        trigger, severity = verdict
        if open_incidents:
            for incident in open_incidents:
                if incident.get("severity") != severity:
                    await self._reseverity(incident, severity, now)
            return {
                "provider": provider,
                "outcome": "open",
                "trigger": trigger,
                "severity": severity,
                "escalation_ids": [incident["id"] for incident in open_incidents],
            }
        row = self._rows[provider]
        escalation_id = f"escalation-provider-{provider}-{row.generation}"
        existing = await self._db.get_escalation(escalation_id)
        if existing is not None:
            # This outage's incident was already closed -- by a human, or by
            # the supervisor after one replied.  The same generation is the
            # same outage: paging again would reopen a decision already made.
            return {
                "provider": provider,
                "outcome": "closed",
                "escalation_ids": [escalation_id],
                "state": existing["state"],
            }
        affected = await self.affected(provider)
        project_id = _busiest_project(affected["held"])
        if project_id is None:
            return {"provider": provider, "outcome": "no_affected_tasks", "trigger": trigger}
        incident, created = await self._db.create_escalation(
            id=escalation_id,
            project_id=project_id,
            task_id=None,
            source_kind=ESCALATION_SOURCE_KIND,
            source_identity=f"{provider}:{row.generation}",
            incident_key=f"provider:{provider}:{row.generation}",
            supervisor_owner=f"supervisor-{project_id}",
            summary=self._escalation_summary(provider, trigger, now)[:4000],
            investigation=self._escalation_investigation(
                provider, trigger, session, affected, now
            )[:8000],
            decision_requested=self._escalation_decision(provider, trigger, now)[:4000],
            choices=None,
            severity=severity,
            now=now,
        )
        if created:
            await self._emit_escalation(
                "escalation.created.v1",
                {
                    "escalation_id": incident["id"],
                    "project_id": incident["project_id"],
                    "task_id": None,
                    "source_kind": incident["source_kind"],
                    "source_identity": incident["source_identity"],
                    "incident_key": incident["incident_key"],
                    "state": incident["state"],
                    "revision": incident["revision"],
                },
            )
        return {
            "provider": provider,
            "outcome": "created" if created else "open",
            "trigger": trigger,
            "severity": severity,
            "project_id": incident["project_id"],
            "escalation_ids": [incident["id"]],
        }

    async def _resolve_escalation(
        self, incident: Mapping[str, Any], provider: str, now: float
    ) -> dict[str, Any] | None:
        row = self._rows.get(provider)
        state = row.effective_state(now) if row is not None else AVAILABLE
        reason = row.effective_reason(now) if row is not None else ""
        if state not in UNAVAILABLE:
            outcome = f"Provider {provider} is launchable again ({state}"
            outcome += f": {reason})." if reason else ")."
        elif state == DISABLED:
            outcome = f"Provider {provider} was disabled by an operator: {reason}."
        else:
            until = row.effective_until(now) if row is not None else None
            outcome = (
                f"Provider {provider} is now {state}: {reason}"
                + (f"; expected recovery {_fmt_ts(until)}" if until else "")
                + ". Nothing is left for a human to decide."
            )
        resolved = await self._db.resolve_escalation_on_recovery(
            incident["id"],
            expected_revision=int(incident["revision"]),
            source_kind=ESCALATION_SOURCE_KIND,
            terminal_outcome=outcome,
            terminal_evidence={
                "provider": provider,
                "state": state,
                "reason_code": row.effective_reason_code(now) if row is not None else "",
                "generation": row.generation if row is not None else None,
                "at": now,
            },
            now=now,
        )
        if resolved is not None:
            await self._emit_escalation(
                "escalation.updated.v1",
                {
                    "escalation_id": resolved["id"],
                    "project_id": resolved["project_id"],
                    "task_id": resolved.get("task_id"),
                    "state": resolved["state"],
                    "revision": resolved["revision"],
                    "terminal_outcome": resolved.get("terminal_outcome"),
                },
            )
        return resolved

    async def _reseverity(self, incident: Mapping[str, Any], severity: str, now: float) -> None:
        """Keep an open incident's severity current (``critical`` while all is down)."""
        try:
            updated = await self._db.transition_escalation(
                incident["id"],
                expected_revision=int(incident["revision"]),
                new_state=incident["state"],
                severity=severity,
                now=now,
            )
        except Exception:
            logger.debug("provider escalations: severity update refused", exc_info=True)
            return
        if updated is not None:
            await self._emit_escalation(
                "escalation.updated.v1",
                {
                    "escalation_id": updated["id"],
                    "project_id": updated["project_id"],
                    "task_id": updated.get("task_id"),
                    "state": updated["state"],
                    "revision": updated["revision"],
                },
            )

    async def _emit_escalation(self, event_type: str, payload: dict[str, Any]) -> None:
        """A version-1 state hint after the row committed, like the command layer's."""
        body = {"version": 1, **payload}
        if self._bus is not None:
            try:
                await self._bus.emit(event_type, body)
            except Exception:
                logger.debug("%s emit failed", event_type, exc_info=True)
        try:
            await self._db.log_event(
                event_type,
                project_id=payload.get("project_id"),
                payload=str(payload.get("escalation_id") or ""),
            )
        except Exception:
            logger.debug("%s log_event failed", event_type, exc_info=True)

    def _escalation_summary(self, provider: str, trigger: str, now: float) -> str:
        row = self._rows[provider]
        state = row.effective_state(now)
        vendor = f" ({row.vendor})" if row.vendor else ""
        if trigger == "all_providers_unavailable":
            return f"Every provider is unavailable; {provider}{vendor} is {state}"
        if trigger == FAILING:
            minutes = (now - row.effective_since(now)) / 60.0
            return f"Provider {provider}{vendor} has been failing for {minutes:.0f} min"
        return f"Provider {provider}{vendor} is {state} and needs a login"

    def _escalation_investigation(
        self,
        provider: str,
        trigger: str,
        session: set[str],
        affected: Mapping[str, Any],
        now: float,
    ) -> str:
        view = self.describe(provider, now)
        until = _fmt_ts(view["until"]) if view["until"] else "none known"
        lines = [
            (
                f"{provider} is {view['state']} ({view['reason_code'] or '-'}): "
                f"{view['reason'] or '-'}"
            ),
            f"Since {_fmt_ts(view['since'])}; expected recovery: {until}.",
        ]
        if trigger == "all_providers_unavailable" or (
            session and all(self.is_unavailable(p, now) for p in session)
        ):
            others = ", ".join(
                f"{p} {self.effective_state(p, now)}" for p in sorted(session)
            )
            lines.append(f"Every session provider is unavailable: {others}.")
        held = affected.get("held") or []
        per_project: dict[str, int] = {}
        for entry in held:
            per_project[entry["project_id"]] = per_project.get(entry["project_id"], 0) + 1
        lines.append(
            f"Queued tasks routed to {provider}: {len(held)}"
            + (
                " (" + ", ".join(f"{pid} {n}" for pid, n in sorted(per_project.items())) + ")"
                if per_project
                else ""
            )
            + "."
        )
        if affected.get("roles"):
            lines.append(
                "Role profiles on this provider cannot launch: " + ", ".join(affected["roles"])
            )
        lines.append(
            f"Launches against {provider} are "
            + ("suppressed." if self.enforcing else "NOT suppressed (observe mode).")
        )
        evidence = [
            f"{entry.get('kind')}{'/' + str(entry['signal']) if entry.get('signal') else ''}"
            for entry in view["evidence"][:5]
        ]
        if evidence:
            lines.append("Latest evidence: " + ", ".join(evidence) + ".")
        lines.append(
            f"`aq provider status {provider} --verbose` shows the evidence and transitions."
        )
        return "\n".join(lines)

    def _escalation_decision(self, provider: str, trigger: str, now: float) -> str:
        state = self.effective_state(provider, now)
        remediation = self.remediation(provider, state) or f"inspect provider {provider}"
        text = remediation[0].upper() + remediation[1:]
        if trigger == "all_providers_unavailable":
            text += (
                " Every provider is unavailable, so no queued work can run until one returns;"
                " `aq provider set-state <provider> available --for 1h --reason ...` re-admits"
                " one you know to be healthy."
            )
        return text

    # -- words ---------------------------------------------------------------------

    def remediation(self, provider: str, state: str) -> str:
        """What a human does about *state* (D1's "Carries" column)."""
        if state == UNAUTHENTICATED:
            login = self._login_for(provider)
            command = getattr(login, "login_command", None) or f"{provider} login"
            return (
                f"run `{command}` on the host; if the account is out of usage the login "
                f"will not stick until it resets. Then `aq provider recheck {provider}`."
            )
        if state == EXHAUSTED:
            return (
                f"wait for the usage window to reset, or `aq provider set-state {provider} "
                "available --for 1h --reason ...` if this is a false positive"
            )
        if state == FAILING:
            return (
                "launches die during startup for a reason AQ cannot name; check the CLI "
                f"on the host (`aq session logs`), then `aq provider set-state {provider} auto`"
            )
        if state == DISABLED:
            return f"`aq provider set-state {provider} auto` clears the override"
        return ""

    def headline(self, transition: Transition) -> str:
        until = f"; expected recovery {_fmt_ts(transition.until)}" if transition.until else ""
        if transition.to_state in UNAVAILABLE:
            return (
                f"Provider {transition.provider} ({transition.vendor or '-'}) is "
                f"{transition.to_state}: {transition.reason}{until}"
            )
        return (
            f"Provider {transition.provider} ({transition.vendor or '-'}) is launchable again "
            f"({transition.to_state}{': ' + transition.reason if transition.reason else ''})"
        )

    def describe(self, provider: str, now: float | None = None) -> dict[str, Any]:
        """The status view of one provider: every field the CLI, API and doctor show."""
        at = self.now() if now is None else now
        row = self._rows.get(provider) or ProviderAvailability(provider=provider, since=at)
        state = row.effective_state(at) if self.tracking else AVAILABLE
        override = None
        if row.override_active(at):
            override = {
                "state": row.override_state,
                "until": row.override_until,
                "by": row.override_by,
                "reason": row.override_reason,
                "set_at": row.override_set_at,
            }
        return {
            "provider": provider,
            "vendor": row.vendor,
            "state": state,
            "half": half(state),
            "reason_code": row.effective_reason_code(at),
            "reason": row.effective_reason(at),
            "since": row.effective_since(at),
            "until": row.effective_until(at),
            "derived_state": row.state,
            "derived_reason": row.reason,
            "derived_until": row.until,
            "override": override,
            "level": row.level,
            "generation": row.generation,
            "consecutive_failures": row.consecutive_failures,
            "last_failure_at": row.last_failure_at,
            "last_success_at": row.last_success_at,
            "last_probe_at": row.last_probe_at,
            "probation": row.on_probation(at),
            "remediation": self.remediation(provider, state),
            "evidence": [dict(e) for e in row.evidence],
            "mode": getattr(self.config, "mode", "enforce"),
            "updated_at": row.updated_at,
        }
