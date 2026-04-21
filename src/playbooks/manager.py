"""Playbook lifecycle manager — compilation, versioning, and error handling.

Manages the lifecycle of compiled playbooks: triggers compilation when
playbook markdown files change, persists compiled JSON to disk, maintains
an in-memory registry of active playbooks, and ensures that the previous
compiled version remains active when compilation fails.

The manager is the integration layer between:

- :class:`~src.playbooks.compiler.PlaybookCompiler` — LLM compilation
- :class:`~src.playbooks.store.CompiledPlaybookStore` — scope-mirrored storage
- :class:`~src.event_bus.EventBus` — error/success notifications
- Filesystem — persistence of compiled JSON artifacts

**Trigger mapping** (roadmap 5.3.1):

    The manager maintains an explicit ``trigger → playbook IDs`` mapping
    that is updated on every add/remove/compile operation.  This enables
    O(1) lookup when an event arrives, instead of scanning all active
    playbooks.

**EventBus subscription** (roadmap 5.3.2):

    The manager subscribes to the :class:`~src.event_bus.EventBus` for all
    trigger event types.  Triggers with payload filters (see spec §10
    Composability) pass their filter dicts to the EventBus ``subscribe()``
    call, so the EventBus handles filtering before the handler fires.
    When a trigger event matches, the manager checks cooldown and
    concurrency limits, then dispatches to an ``on_trigger`` callback.
    Subscriptions are automatically refreshed when playbooks are
    added, removed, or recompiled.

**Event-to-scope matching** (roadmap 5.3.3):

    When a trigger event fires, the manager checks whether the event's
    scope matches the playbook's scope before dispatching.  Events with
    ``project_id`` in their payload match system-scoped playbooks (always),
    project-scoped playbooks for the matching project, and agent-type
    playbooks matching the originating agent's type.  Events without
    ``project_id`` match system-scoped playbooks only — project and
    agent-type playbooks are skipped.

**Cooldown tracking** (roadmap 5.3.4):

    Per-playbook, per-scope cooldown prevents rapid re-triggering.  Each
    ``(playbook_id, scope)`` pair tracks the last execution completion
    time.  Both success and failure apply cooldown (to prevent error
    loops).  Events arriving during cooldown are silently dropped, not
    queued.  Cooldown of 0 or ``None`` means no restriction.

**Concurrency limits** (roadmap 5.3.5):

    A global concurrency cap (``max_concurrent_playbook_runs``) limits
    how many playbook runs execute simultaneously.  Multiple instances of
    the same playbook can run concurrently (e.g. two ``git.commit``
    events → two ``code-quality-gate`` runs), but the total across all
    playbooks is capped.  This mirrors the hook engine's
    ``max_concurrent_hooks`` gate.  When at capacity, new runs are
    rejected (not queued).  A value of 0 means unlimited.

**Error handling policy** (spec §4, roadmap 5.1.7):

    If compilation fails (invalid output, LLM error, validation failure),
    the previous compiled version remains active and an error notification
    is surfaced via the EventBus.  This is atomic — a partially valid
    compilation never replaces a working version.

See ``docs/specs/design/playbooks.md`` Section 4 for the specification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from src.playbooks.compiler import DEFAULT_MAX_TOKENS, CompilationResult, PlaybookCompiler
from src.playbooks.models import CompiledPlaybook, PlaybookScope, PlaybookTrigger

if TYPE_CHECKING:
    from src.chat_providers.base import ChatProvider
    from src.event_bus import EventBus
    from src.playbooks.store import CompiledPlaybookStore

# Type alias for the trigger callback:
#   async callback(playbook: CompiledPlaybook, event_data: dict) -> None
TriggerCallback = Callable[[CompiledPlaybook, dict[str, Any]], Any]

logger = logging.getLogger(__name__)


class PlaybookManager:
    """Manages compiled playbook versions with error-safe updates.

    Maintains an in-memory registry of active :class:`CompiledPlaybook`
    instances and persists compiled JSON to a data directory.  When a
    playbook markdown file changes, the manager:

    1. Invokes the :class:`PlaybookCompiler` to produce a new version.
    2. On **success**: replaces the active version in-memory and on disk,
       emits a ``notify.playbook_compilation_succeeded`` event.
    3. On **failure**: keeps the previous version active (both in-memory
       and on disk), emits a ``notify.playbook_compilation_failed`` event
       with file path and all error details.

    Additionally, the manager maintains an explicit trigger → playbook
    mapping (roadmap 5.3.1) that is updated on every add/remove/compile
    operation.  This enables efficient O(1) lookup via
    :meth:`get_playbooks_by_trigger` when events arrive, instead of
    scanning all active playbooks.

    **EventBus subscription** (roadmap 5.3.2):

        When an :class:`~src.event_bus.EventBus` is provided, the manager
        subscribes to all trigger event types from active playbooks.
        Triggers with payload filters (spec §10) pass their filter dicts
        to the EventBus ``subscribe()`` method.  When a trigger fires:

        1. The EventBus delivers the event (type + filter already matched).
        2. The manager verifies the playbook is still active.
        3. The manager checks event-to-scope matching (5.3.3).
        4. The manager checks cooldown and concurrency limits.
        5. If all checks pass, the ``on_trigger`` callback is invoked.

        Subscriptions are refreshed automatically when playbooks change.
        Call :meth:`subscribe_to_events` after initial loading to activate
        subscriptions, and :meth:`unsubscribe_from_events` before shutdown.

    **Cooldown tracking** (roadmap 5.3.4):

        The manager tracks the last execution completion time for each
        ``(playbook_id, scope)`` pair.  Before an event triggers a
        playbook, callers should check :meth:`is_on_cooldown`.  The
        cooldown is per-playbook *and* per-scope — a project-level
        cooldown does not block a system-level instance of the same
        playbook.  Failed runs also apply cooldown to prevent error
        loops.  Events arriving during cooldown are dropped, not queued.

    **Concurrency tracking** (roadmap 5.3.5):

        The manager tracks in-flight playbook runs as asyncio Tasks and
        enforces a global concurrency cap.  Multiple instances of the
        same playbook are allowed (keyed by run_id, not playbook_id),
        but the total number of concurrent runs is limited.  When at
        capacity, :meth:`can_start_run` returns ``False`` and callers
        should skip launching.

    Parameters
    ----------
    chat_provider:
        The :class:`~src.chat_providers.base.ChatProvider` used for LLM
        compilation calls.  When ``None``, compilation is skipped (log-only).
    event_bus:
        Optional :class:`~src.event_bus.EventBus` for emitting notifications.
    data_dir:
        Root data directory (e.g. ``~/.agent-queue``).  Compiled playbook
        JSON files are stored under ``{data_dir}/playbooks/compiled/``.
        When ``None``, persistence is disabled (in-memory only).
    store:
        Optional :class:`~src.playbooks.store.CompiledPlaybookStore` for
        scope-mirrored storage.  When provided, :meth:`load_from_store`
        uses it to load all compiled playbooks across all scopes on
        startup.  The legacy ``data_dir`` flat-directory persistence is
        used as a fallback when no store is provided.
    max_concurrent_runs:
        Maximum number of playbook runs that can execute simultaneously.
        Defaults to ``2``.  Set to ``0`` for unlimited.  Mirrors the
        hook engine's ``max_concurrent_hooks`` setting.
    on_trigger:
        Optional async callback invoked when a trigger event fires and all
        checks pass (playbook active, not on cooldown, concurrency cap not
        reached).  Signature: ``async (playbook, event_data) -> None``.
        This is the integration point for the playbook executor.
    """

    def __init__(
        self,
        *,
        chat_provider: ChatProvider | None = None,
        event_bus: EventBus | None = None,
        data_dir: str | None = None,
        store: CompiledPlaybookStore | None = None,
        max_concurrent_runs: int = 2,
        on_trigger: TriggerCallback | None = None,
        playbook_max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._chat_provider = chat_provider
        self._event_bus = event_bus
        self._data_dir = data_dir
        self._store = store

        # In-memory registry: playbook_id → active CompiledPlaybook
        self._active: dict[str, CompiledPlaybook] = {}

        # Trigger mapping: trigger event type → set of playbook IDs
        # Updated on every add/remove/compile to enable O(1) lookups.
        self._trigger_map: dict[str, set[str]] = {}

        # Track source paths for each playbook ID (for notifications)
        self._source_paths: dict[str, str] = {}

        # Scope identifier mapping: playbook_id → identifier string.
        # For project-scoped playbooks this is the project_id.
        # For agent-type playbooks the identifier is already encoded in
        # the scope string ("agent-type:coding"), but we cache it here for
        # consistency.  System-scoped playbooks have ``None``.
        # Populated by :meth:`load_from_store` and :meth:`set_scope_identifier`.
        self._scope_identifiers: dict[str, str | None] = {}

        # Cooldown tracking: (playbook_id, scope) → last completion timestamp.
        # Tracked per (playbook_id, scope) so project-level cooldowns don't
        # block system-level instances of the same playbook.  Both successful
        # and failed runs record a completion time to prevent error loops.
        self._last_execution: dict[tuple[str, str], float] = {}

        # -- Concurrency tracking (roadmap 5.3.5) --
        # In-flight playbook runs keyed by run_id.  Each value is the asyncio
        # Task executing that run.  Keyed by run_id (not playbook_id) because
        # the spec allows multiple instances of the same playbook concurrently.
        self._running: dict[str, asyncio.Task] = {}

        # Reverse lookup: run_id → playbook_id, for per-playbook introspection
        # (e.g. "how many instances of playbook X are running?").
        self._running_playbook_ids: dict[str, str] = {}

        # Global concurrency cap.  0 = unlimited.
        self._max_concurrent_runs: int = max_concurrent_runs

        # -- EventBus subscription (roadmap 5.3.2) --
        # Callback invoked when a trigger fires and passes all checks.
        self._on_trigger: TriggerCallback | None = on_trigger
        # Unsubscribe callables from EventBus.subscribe() — one per
        # (playbook, trigger) pair.  Cleared and rebuilt on each
        # subscribe_to_events() call.
        self._event_subscriptions: list[Callable[[], None]] = []
        # Whether subscriptions have been activated via subscribe_to_events().
        # Used by _refresh_subscriptions() to decide whether to auto-resubscribe
        # after playbook mutations.
        self._subscribed: bool = False

        # Default notification channel for system-scoped playbooks.
        # Set by the orchestrator after Discord bot is ready so that
        # trigger-initiated playbook runs know where to post summaries.
        self.system_notification_channel_id: str | None = None

        # Compiler instance (created lazily when provider is available)
        self._playbook_max_tokens = playbook_max_tokens
        self._compiler: PlaybookCompiler | None = None
        if chat_provider is not None:
            self._compiler = PlaybookCompiler(chat_provider, max_tokens=playbook_max_tokens)

    # -- public API ----------------------------------------------------------

    @property
    def active_playbooks(self) -> dict[str, CompiledPlaybook]:
        """Return a read-only view of all active compiled playbooks."""
        return dict(self._active)

    @property
    def trigger_map(self) -> dict[str, list[str]]:
        """Return a read-only view of the trigger → playbook ID mapping.

        Returns
        -------
        dict[str, list[str]]
            Mapping from trigger event type to sorted list of playbook IDs
            that respond to that trigger.
        """
        return {trigger: sorted(ids) for trigger, ids in self._trigger_map.items()}

    @property
    def playbook_count(self) -> int:
        """Return the number of active playbooks."""
        return len(self._active)

    def get_all_triggers(self) -> list[str]:
        """Return all registered trigger event types across all active playbooks.

        Returns
        -------
        list[str]
            Sorted list of unique trigger event types.
        """
        return sorted(self._trigger_map.keys())

    def get_playbook(self, playbook_id: str) -> CompiledPlaybook | None:
        """Return the active compiled playbook for *playbook_id*, or ``None``."""
        return self._active.get(playbook_id)

    def get_playbooks_by_trigger(self, trigger: str) -> list[CompiledPlaybook]:
        """Return all active playbooks that match the given *trigger* event type.

        Uses the pre-built trigger mapping for O(1) lookup instead of
        scanning all active playbooks.
        """
        playbook_ids = self._trigger_map.get(trigger)
        if not playbook_ids:
            return []
        return [self._active[pid] for pid in sorted(playbook_ids) if pid in self._active]

    # -- scope identifier tracking (roadmap 5.3.3) -----------------------------

    def set_scope_identifier(self, playbook_id: str, identifier: str | None) -> None:
        """Associate a scope identifier with a playbook.

        For project-scoped playbooks this is the ``project_id``.  For
        agent-type playbooks it is extracted automatically from the scope
        string, but callers may set it explicitly.  System-scoped playbooks
        should have ``None``.

        Called by :meth:`load_from_store` and available for external callers
        (e.g. the vault watcher) that add playbooks with project context.
        """
        self._scope_identifiers[playbook_id] = identifier

    def get_scope_identifier(self, playbook_id: str) -> str | None:
        """Return the scope identifier for a playbook, or ``None``."""
        return self._scope_identifiers.get(playbook_id)

    # -- cooldown tracking (roadmap 5.3.4) ------------------------------------

    def is_on_cooldown(self, playbook_id: str, scope: str = "system") -> bool:
        """Check whether a playbook is currently on cooldown.

        A playbook is on cooldown when:

        1. It has a ``cooldown_seconds`` value > 0.
        2. A previous execution completed within that window.

        The check is scoped: a project-level cooldown does not block a
        system-level instance of the same playbook (different scope keys).

        Parameters
        ----------
        playbook_id:
            The playbook to check.
        scope:
            The scope string (e.g. ``"system"``, ``"project"``,
            ``"agent-type:coding"``).  Cooldown state is tracked
            independently per scope.

        Returns
        -------
        bool
            ``True`` if the playbook is on cooldown and should not be
            triggered, ``False`` if it can run.
        """
        return self.get_cooldown_remaining(playbook_id, scope) > 0.0

    def get_cooldown_remaining(self, playbook_id: str, scope: str = "system") -> float:
        """Return the number of seconds remaining in a playbook's cooldown.

        Returns ``0.0`` when the playbook is not on cooldown (either
        because it has no ``cooldown_seconds``, the cooldown has expired,
        the cooldown is explicitly set to 0, or no execution has been
        recorded).

        Parameters
        ----------
        playbook_id:
            The playbook to check.
        scope:
            The scope string.

        Returns
        -------
        float
            Seconds remaining (≥ 0.0).
        """
        playbook = self._active.get(playbook_id)
        if playbook is None:
            return 0.0

        cooldown = playbook.cooldown_seconds
        if cooldown is None or cooldown <= 0:
            return 0.0

        key = (playbook_id, scope)
        last = self._last_execution.get(key)
        if last is None:
            return 0.0

        elapsed = time.monotonic() - last
        remaining = cooldown - elapsed
        return max(0.0, remaining)

    def record_execution(
        self,
        playbook_id: str,
        scope: str = "system",
        *,
        _clock: float | None = None,
    ) -> None:
        """Record that a playbook completed execution (success or failure).

        Both successful and failed runs record a completion time.  This
        prevents rapid re-triggering of playbooks that consistently fail
        (error loops).

        Parameters
        ----------
        playbook_id:
            The playbook that completed.
        scope:
            The scope string for per-scope cooldown tracking.
        _clock:
            Internal parameter for testing — override the monotonic clock
            value.  Production callers should never pass this.
        """
        key = (playbook_id, scope)
        self._last_execution[key] = _clock if _clock is not None else time.monotonic()
        logger.debug(
            "Recorded execution for playbook '%s' (scope=%s) — cooldown started",
            playbook_id,
            scope,
        )

    def clear_cooldown(self, playbook_id: str, scope: str | None = None) -> None:
        """Clear cooldown state for a playbook.

        When *scope* is ``None``, clears cooldown across all scopes for
        the given playbook.  When *scope* is provided, only clears that
        specific ``(playbook_id, scope)`` entry.

        Parameters
        ----------
        playbook_id:
            The playbook whose cooldown to clear.
        scope:
            Optional scope to clear.  ``None`` clears all scopes.
        """
        if scope is not None:
            self._last_execution.pop((playbook_id, scope), None)
        else:
            keys_to_remove = [k for k in self._last_execution if k[0] == playbook_id]
            for k in keys_to_remove:
                del self._last_execution[k]

    def get_triggerable_playbooks(
        self, trigger: str, scope: str = "system"
    ) -> list[CompiledPlaybook]:
        """Return playbooks matching *trigger* that are not on cooldown.

        Combines :meth:`get_playbooks_by_trigger` with cooldown checking
        to give the caller a ready-to-execute list.  Playbooks on cooldown
        are silently skipped (events during cooldown are dropped, not
        queued).

        .. note::

            This method does **not** check the global concurrency cap
            (see :meth:`can_start_run`).  Concurrency is a global gate
            ("is there room for any new run?"), whereas this method
            answers per-playbook questions ("does this playbook want to
            run?").  Callers should check both.

        Parameters
        ----------
        trigger:
            The event type to match (e.g. ``"git.commit"``).
        scope:
            The scope to check cooldowns against.

        Returns
        -------
        list[CompiledPlaybook]
            Playbooks that match the trigger and are not on cooldown.
        """
        candidates = self.get_playbooks_by_trigger(trigger)
        result = []
        for playbook in candidates:
            if self.is_on_cooldown(playbook.id, scope):
                logger.debug(
                    "Playbook '%s' skipped for trigger '%s' — on cooldown "
                    "(%.1fs remaining, scope=%s)",
                    playbook.id,
                    trigger,
                    self.get_cooldown_remaining(playbook.id, scope),
                    scope,
                )
            else:
                result.append(playbook)
        return result

    # -- concurrency tracking (roadmap 5.3.5) --------------------------------

    @property
    def max_concurrent_runs(self) -> int:
        """The configured maximum number of concurrent playbook runs.

        A value of ``0`` means unlimited (no cap enforced).
        """
        return self._max_concurrent_runs

    @max_concurrent_runs.setter
    def max_concurrent_runs(self, value: int) -> None:
        """Update the concurrency cap at runtime (e.g. after config hot-reload)."""
        self._max_concurrent_runs = value

    @property
    def running_count(self) -> int:
        """Number of currently in-flight playbook runs."""
        return len(self._running)

    @property
    def running_runs(self) -> dict[str, str]:
        """Return a read-only mapping of ``run_id → playbook_id`` for in-flight runs."""
        return dict(self._running_playbook_ids)

    def can_start_run(self) -> bool:
        """Check whether a new playbook run can start under the concurrency limit.

        Returns ``True`` when the global cap has room (or is set to ``0``
        for unlimited).  Callers should check this before calling
        :meth:`register_run`.

        Returns
        -------
        bool
            ``True`` if a new run can be launched, ``False`` if at capacity.
        """
        if self._max_concurrent_runs <= 0:
            return True  # 0 = unlimited
        return len(self._running) < self._max_concurrent_runs

    def register_run(
        self,
        run_id: str,
        playbook_id: str,
        task: asyncio.Task,
    ) -> bool:
        """Register a newly launched playbook run for concurrency tracking.

        Callers should check :meth:`can_start_run` first, but this method
        performs a final check and returns ``False`` if the cap has been
        reached (e.g. due to a race between two event handlers).

        Parameters
        ----------
        run_id:
            Unique identifier for this run (e.g. UUID).
        playbook_id:
            The playbook being executed.
        task:
            The ``asyncio.Task`` executing the run.

        Returns
        -------
        bool
            ``True`` if the run was registered, ``False`` if the
            concurrency cap would be exceeded.
        """
        if not self.can_start_run():
            logger.warning(
                "Playbook run '%s' (playbook=%s) rejected — concurrency cap reached "
                "(%d/%d running)",
                run_id,
                playbook_id,
                len(self._running),
                self._max_concurrent_runs,
            )
            return False

        self._running[run_id] = task
        self._running_playbook_ids[run_id] = playbook_id
        logger.debug(
            "Registered playbook run '%s' (playbook=%s) — %d/%s running",
            run_id,
            playbook_id,
            len(self._running),
            self._max_concurrent_runs if self._max_concurrent_runs > 0 else "∞",
        )
        return True

    def unregister_run(self, run_id: str) -> None:
        """Remove a completed run from tracking.

        Called explicitly when the caller manages task lifecycle outside
        of :meth:`reap_completed_runs` (e.g. in a callback).

        Parameters
        ----------
        run_id:
            The run to unregister.
        """
        self._running.pop(run_id, None)
        self._running_playbook_ids.pop(run_id, None)

    def reap_completed_runs(self) -> list[str]:
        """Scan for completed asyncio Tasks and remove them from tracking.

        Surfaces any unhandled exceptions as log errors (they are
        otherwise silently swallowed by asyncio).

        This should be called periodically (e.g. each orchestrator tick)
        to free concurrency slots.

        Returns
        -------
        list[str]
            The run_ids that were reaped.
        """
        done = [rid for rid, t in self._running.items() if t.done()]
        reaped: list[str] = []
        for rid in done:
            task = self._running.pop(rid)
            playbook_id = self._running_playbook_ids.pop(rid, "<unknown>")
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.error(
                        "Playbook run '%s' (playbook=%s) failed with unhandled exception: %s",
                        rid,
                        playbook_id,
                        exc,
                    )
            logger.debug(
                "Reaped playbook run '%s' (playbook=%s) — %d still running",
                rid,
                playbook_id,
                len(self._running),
            )
            reaped.append(rid)
        return reaped

    def get_runs_for_playbook(self, playbook_id: str) -> list[str]:
        """Return run_ids of in-flight runs for a specific playbook.

        Parameters
        ----------
        playbook_id:
            The playbook to query.

        Returns
        -------
        list[str]
            Sorted list of run_ids.
        """
        return sorted(rid for rid, pid in self._running_playbook_ids.items() if pid == playbook_id)

    async def shutdown_runs(self) -> None:
        """Cancel all running playbook tasks and wait for them to finish.

        Also removes all EventBus subscriptions to prevent stale handler
        references.

        Uses ``asyncio.gather(..., return_exceptions=True)`` to ensure
        CancelledError does not propagate.  Clears all tracking state.
        """
        self.unsubscribe_from_events()
        self._subscribed = False
        for rid, task in self._running.items():
            if not task.done():
                logger.info("Cancelling playbook run '%s'", rid)
                task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()
        self._running_playbook_ids.clear()

    # -- EventBus subscription (roadmap 5.3.2) --------------------------------

    def subscribe_to_events(self) -> int:
        """Subscribe to the EventBus for all trigger event types.

        Creates one EventBus subscription per ``(playbook, trigger)`` pair.
        For triggers with a :attr:`~PlaybookTrigger.filter`, the filter dict
        is passed to :meth:`EventBus.subscribe` so the EventBus only
        dispatches events whose payload matches.

        Previous subscriptions are removed before creating new ones (safe to
        call repeatedly, e.g. after adding or removing playbooks).

        Returns
        -------
        int
            Number of subscriptions created.

        Raises
        ------
        RuntimeError
            If no EventBus was provided to the constructor.
        """
        self.unsubscribe_from_events()
        self._subscribed = True

        if self._event_bus is None:
            logger.debug("No EventBus configured — event subscriptions skipped")
            return 0

        count = 0
        for playbook in self._active.values():
            for trigger in playbook.triggers:
                handler = self._make_trigger_handler(playbook.id, trigger)
                unsub = self._event_bus.subscribe(
                    trigger.event_type,
                    handler,
                    filter=trigger.filter,
                )
                self._event_subscriptions.append(unsub)
                count += 1

        if count:
            logger.info(
                "Subscribed to %d trigger event(s) across %d playbook(s)",
                count,
                len(self._active),
            )
        return count

    def unsubscribe_from_events(self) -> None:
        """Remove all EventBus subscriptions.

        Safe to call even when no subscriptions exist.  Called automatically
        by :meth:`subscribe_to_events` before rebuilding, and should be
        called during shutdown to prevent stale handler references.
        """
        for unsub in self._event_subscriptions:
            unsub()
        if self._event_subscriptions:
            logger.debug("Removed %d event subscription(s)", len(self._event_subscriptions))
        self._event_subscriptions.clear()

    @property
    def subscription_count(self) -> int:
        """Number of active EventBus subscriptions."""
        return len(self._event_subscriptions)

    @property
    def on_trigger(self) -> TriggerCallback | None:
        """The current trigger callback, or ``None``."""
        return self._on_trigger

    @on_trigger.setter
    def on_trigger(self, callback: TriggerCallback | None) -> None:
        """Update the trigger callback at runtime."""
        self._on_trigger = callback

    def _make_trigger_handler(
        self, playbook_id: str, trigger: PlaybookTrigger
    ) -> Callable[[dict[str, Any]], Any]:
        """Create a closure that handles a specific trigger for a specific playbook.

        Each EventBus subscription gets its own handler bound to the
        ``(playbook_id, trigger)`` pair.  The EventBus has already matched
        the event type and applied the payload filter before this runs.

        The handler:
        1. Verifies the playbook is still active (it may have been removed
           since the subscription was created).
        2. Checks per-playbook cooldown.
        3. Checks global concurrency cap.
        4. Invokes the ``on_trigger`` callback if all checks pass.
        """

        async def handler(data: dict[str, Any]) -> None:
            await self._handle_trigger_event(playbook_id, trigger, data)

        return handler

    async def _handle_trigger_event(
        self,
        playbook_id: str,
        trigger: PlaybookTrigger,
        data: dict[str, Any],
    ) -> None:
        """Handle a trigger event dispatched by the EventBus.

        Called when an event matches a playbook's trigger (type + optional
        payload filter already verified by the EventBus).  Performs
        cooldown and concurrency checks, then dispatches to the
        ``on_trigger`` callback.

        Parameters
        ----------
        playbook_id:
            The playbook this subscription belongs to.
        trigger:
            The :class:`PlaybookTrigger` that matched.
        data:
            The event payload dict from the EventBus.
        """
        playbook = self._active.get(playbook_id)
        if playbook is None:
            logger.debug(
                "Trigger '%s' fired but playbook '%s' is no longer active — skipping",
                trigger.event_type,
                playbook_id,
            )
            return

        # -- Event-to-scope matching (roadmap 5.3.3) --
        # Events with project_id match system + matching project + matching
        # agent-type.  Events without project_id match system only.
        # See spec §7 "Event-to-Scope Matching".
        #
        # Timer/cron events carry project_id=null because they originate from
        # a system-level scheduler, but per spec §7 project-scoped playbooks
        # still fire on them — "as if" the tick had been scoped to the
        # playbook's own project. Inject that project_id ahead of the scope
        # check so _matches_scope accepts it.
        if data.get("project_id") is None and trigger.event_type.startswith(("timer.", "cron.")):
            scope_enum, _ = playbook.parse_scope()
            if scope_enum == PlaybookScope.PROJECT:
                scope_id = self._scope_identifiers.get(playbook_id)
                if scope_id:
                    data = {**data, "project_id": scope_id}

        if not self._matches_scope(playbook, data):
            return

        # Build cooldown scope key from the playbook's own scope rather than
        # from event data.  For project-scoped playbooks include the identifier
        # so different projects have independent cooldowns.
        scope = self._cooldown_scope_key(playbook)
        # Check cooldown (roadmap 5.3.4)
        if self.is_on_cooldown(playbook_id, scope):
            remaining = self.get_cooldown_remaining(playbook_id, scope)
            logger.debug(
                "Trigger '%s' for playbook '%s' skipped — on cooldown (%.1fs remaining, scope=%s)",
                trigger.event_type,
                playbook_id,
                remaining,
                scope,
            )
            return

        # Check concurrency (roadmap 5.3.5)
        if not self.can_start_run():
            logger.debug(
                "Trigger '%s' for playbook '%s' skipped — concurrency cap reached (%d/%s running)",
                trigger.event_type,
                playbook_id,
                self.running_count,
                self._max_concurrent_runs if self._max_concurrent_runs > 0 else "∞",
            )
            return

        # Inject project_id for project-scoped playbooks so that events
        # emitted during the run route to the correct project channel.
        if "project_id" not in data:
            scope_id = self._scope_identifiers.get(playbook_id)
            if scope_id:
                data = {**data, "project_id": scope_id}

        # Inject system notification channel for system-scoped playbooks.
        if "notification_channel_id" not in data and self.system_notification_channel_id:
            scope_enum, _ = playbook.parse_scope()
            if scope_enum == PlaybookScope.SYSTEM:
                data = {**data, "notification_channel_id": self.system_notification_channel_id}

        # Dispatch to callback
        if self._on_trigger is not None:
            logger.debug(
                "Trigger '%s' matched playbook '%s' (filter=%s) — dispatching",
                trigger.event_type,
                playbook_id,
                trigger.filter,
            )
            try:
                result = self._on_trigger(playbook, data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.error(
                    "on_trigger callback failed for playbook '%s' (trigger=%s)",
                    playbook_id,
                    trigger.event_type,
                    exc_info=True,
                )
        else:
            logger.debug(
                "Trigger '%s' matched playbook '%s' but no on_trigger callback set",
                trigger.event_type,
                playbook_id,
            )

    def _matches_scope(
        self,
        playbook: CompiledPlaybook,
        data: dict[str, Any],
    ) -> bool:
        """Check whether an event's scope matches the playbook's scope.

        Implements the event-to-scope matching rules from spec §7:

        - **System-scoped** playbooks match all events (with or without
          ``project_id``).
        - **Project-scoped** playbooks match only events that carry a
          ``project_id`` matching the playbook's project.  Events without
          ``project_id`` are skipped.
        - **Agent-type-scoped** playbooks match only events that carry both
          a ``project_id`` **and** an ``agent_type`` matching the playbook's
          agent type.  Events without ``project_id`` are skipped.

        Parameters
        ----------
        playbook:
            The playbook to check.
        data:
            The event payload dict from the EventBus.

        Returns
        -------
        bool
            ``True`` if the event should trigger this playbook.
        """
        scope_enum, scope_type_id = playbook.parse_scope()

        if scope_enum == PlaybookScope.SYSTEM:
            return True

        event_project_id = data.get("project_id")

        if scope_enum == PlaybookScope.PROJECT:
            if event_project_id is None:
                logger.debug(
                    "Skipping project-scoped playbook '%s' — event has no project_id",
                    playbook.id,
                )
                return False
            playbook_project_id = self._scope_identifiers.get(playbook.id)
            if playbook_project_id is not None and playbook_project_id != event_project_id:
                logger.debug(
                    "Skipping project-scoped playbook '%s' — event project '%s' "
                    "!= playbook project '%s'",
                    playbook.id,
                    event_project_id,
                    playbook_project_id,
                )
                return False
            return True

        if scope_enum == PlaybookScope.AGENT_TYPE:
            if event_project_id is None:
                logger.debug(
                    "Skipping agent-type-scoped playbook '%s' — event has no project_id",
                    playbook.id,
                )
                return False
            event_agent_type = data.get("agent_type")
            if event_agent_type is None or event_agent_type != scope_type_id:
                logger.debug(
                    "Skipping agent-type-scoped playbook '%s' — event agent_type '%s' "
                    "!= playbook type '%s'",
                    playbook.id,
                    event_agent_type,
                    scope_type_id,
                )
                return False
            return True

        # Unknown scope — treat as system (matches all)
        return True

    def _cooldown_scope_key(self, playbook: CompiledPlaybook) -> str:
        """Compute the cooldown scope key for a playbook.

        Uses the playbook's scope and identifier to produce a unique key
        for cooldown tracking.  Project-scoped playbooks include the
        project_id so different projects have independent cooldowns.

        Returns
        -------
        str
            A scope key like ``"system"``, ``"project:myapp"``, or
            ``"agent-type:coding"``.
        """
        scope_enum, scope_type_id = playbook.parse_scope()
        if scope_enum == PlaybookScope.PROJECT:
            identifier = self._scope_identifiers.get(playbook.id)
            if identifier:
                return f"project:{identifier}"
            return "project"
        # For system and agent-type, the scope string itself works:
        # "system" or "agent-type:coding"
        return playbook.scope

    def _refresh_subscriptions(self) -> None:
        """Rebuild EventBus subscriptions if they were previously active.

        Called automatically after playbook mutations (add, remove, compile)
        to keep subscriptions in sync with the active playbook set.
        Does nothing if :meth:`subscribe_to_events` has not been called.
        """
        if self._subscribed and self._event_bus is not None:
            self.subscribe_to_events()

    async def load_from_disk(self) -> int:
        """Load previously compiled playbooks from the data directory.

        Reads all ``.json`` files from the compiled playbooks directory and
        populates the in-memory registry and trigger mapping.  Called at
        startup to restore state from a previous run.

        When no ``data_dir`` is configured, returns 0.

        Returns
        -------
        int
            Number of playbooks successfully loaded.
        """
        compiled_dir = self._compiled_dir()
        if compiled_dir is None or not compiled_dir.is_dir():
            return 0

        loaded = 0
        for json_path in sorted(compiled_dir.glob("*.json")):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                playbook = CompiledPlaybook.from_dict(data)
                errors = playbook.validate()
                if errors:
                    logger.warning(
                        "Skipping invalid compiled playbook %s: %s",
                        json_path.name,
                        "; ".join(errors),
                    )
                    continue
                self._active[playbook.id] = playbook
                self._index_triggers(playbook)
                # Best-effort scope identifier extraction for legacy flat dir.
                # Agent-type identifiers live in the scope string; project
                # identifiers are unknown here (only the store has them).
                _, type_id = playbook.parse_scope()
                self._scope_identifiers[playbook.id] = type_id
                loaded += 1
                logger.debug(
                    "Loaded compiled playbook '%s' v%d from disk",
                    playbook.id,
                    playbook.version,
                )
            except Exception:
                logger.warning(
                    "Failed to load compiled playbook from %s",
                    json_path.name,
                    exc_info=True,
                )

        if loaded:
            logger.info("Loaded %d compiled playbook(s) from disk", loaded)
            self._refresh_subscriptions()
        return loaded

    async def load_from_store(self) -> int:
        """Load all compiled playbooks from the :class:`CompiledPlaybookStore`.

        Walks every scope (system, orchestrator, agent-types, projects) in
        the store and populates the in-memory registry and trigger mapping.
        Playbooks that fail validation are skipped with a warning.

        This is the preferred startup loading method when a store is
        configured.  Falls back to :meth:`load_from_disk` when no store
        is available.

        Returns
        -------
        int
            Number of playbooks successfully loaded.
        """
        if self._store is None:
            logger.debug("No CompiledPlaybookStore configured, falling back to load_from_disk")
            return await self.load_from_disk()

        all_playbooks = self._store.list_all()
        loaded = 0
        for scope, identifier, playbook in all_playbooks:
            errors = playbook.validate()
            if errors:
                logger.warning(
                    "Skipping invalid compiled playbook '%s' (scope=%s, id=%s): %s",
                    playbook.id,
                    scope,
                    identifier,
                    "; ".join(errors),
                )
                continue
            self._active[playbook.id] = playbook
            self._index_triggers(playbook)
            # Track the scope identifier for event-to-scope matching (5.3.3)
            self._scope_identifiers[playbook.id] = identifier
            loaded += 1
            logger.debug(
                "Loaded compiled playbook '%s' v%d from store (scope=%s, identifier=%s)",
                playbook.id,
                playbook.version,
                scope,
                identifier,
            )

        if loaded:
            logger.info("Loaded %d compiled playbook(s) from store across all scopes", loaded)
            self._refresh_subscriptions()
        return loaded

    async def reconcile_compilations(
        self,
        vault_root: str,
    ) -> dict:
        """Walk the vault for playbook markdown files and compile any that
        aren't already active in the in-memory registry.

        Fixes the gap where freshly-installed playbook files never trigger
        initial compilation: the vault watcher takes its initial snapshot
        after :func:`~src.vault.ensure_vault_layout` has copied default
        playbooks in, so those files appear "already present" and never
        emit a ``created`` event.

        This method is idempotent — already-compiled playbooks are
        skipped based on their frontmatter ``id`` matching an entry in
        :attr:`_active`.

        Parameters
        ----------
        vault_root:
            Absolute path to the vault root (typically
            ``{data_dir}/vault``).

        Returns
        -------
        dict
            Summary with ``compiled`` (ids newly compiled),
            ``skipped`` (ids already active), and ``errors``
            (``[(source_path, [msgs])]``).
        """
        from src.playbooks.handler import PLAYBOOK_PATTERNS
        import fnmatch

        result: dict = {"compiled": [], "skipped": [], "errors": []}

        if not os.path.isdir(vault_root):
            return result

        # Walk every .md file under vault_root and match against the
        # playbook path patterns.  Using os.walk plus fnmatch keeps this
        # dependency-free — no need to pull the VaultWatcher in here.
        for dirpath, _dirnames, filenames in os.walk(vault_root):
            for filename in filenames:
                if not filename.endswith(".md"):
                    continue
                abs_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(abs_path, vault_root).replace("\\", "/")

                if not any(fnmatch.fnmatch(rel_path, pattern) for pattern in PLAYBOOK_PATTERNS):
                    continue

                try:
                    with open(abs_path, encoding="utf-8") as f:
                        markdown = f.read()
                except Exception as exc:
                    result["errors"].append((rel_path, [f"read failed: {exc}"]))
                    continue

                frontmatter, _ = PlaybookCompiler._parse_frontmatter(markdown)
                playbook_id = frontmatter.get("id", "").strip()
                if not playbook_id:
                    result["errors"].append((rel_path, ["missing or empty frontmatter `id`"]))
                    continue

                if playbook_id in self._active:
                    result["skipped"].append(playbook_id)
                    continue

                logger.info(
                    "Reconcile: compiling uncompiled playbook %r (%s)",
                    playbook_id,
                    rel_path,
                )
                try:
                    from src.playbooks.handler import derive_playbook_scope

                    _, scope_identifier = derive_playbook_scope(rel_path)
                    compile_result = await self.compile_playbook(
                        markdown,
                        source_path=abs_path,
                        rel_path=rel_path,
                        scope_identifier=scope_identifier,
                    )
                    if compile_result.success:
                        result["compiled"].append(playbook_id)
                    else:
                        result["errors"].append((rel_path, list(compile_result.errors)))
                except Exception as exc:
                    logger.warning("Reconcile: compilation failed for %s", rel_path, exc_info=True)
                    result["errors"].append((rel_path, [str(exc)]))

        if result["compiled"]:
            logger.info(
                "Reconcile: compiled %d uncompiled playbook(s): %s",
                len(result["compiled"]),
                ", ".join(result["compiled"]),
            )
        elif result["errors"]:
            logger.warning(
                "Reconcile: %d playbook(s) failed to compile",
                len(result["errors"]),
            )

        return result

    async def compile_playbook(
        self,
        markdown: str,
        *,
        source_path: str = "",
        rel_path: str = "",
        force: bool = False,
        scope_identifier: str | None = None,
    ) -> CompilationResult:
        """Compile a playbook markdown file and update the active version.

        Before invoking the LLM compiler, checks whether the source markdown
        has changed by comparing the SHA-256 hash of the new content against
        the hash stored in the currently active compiled version.  If the
        hashes match (and *force* is ``False``), compilation is skipped and
        the existing compiled playbook is returned immediately.

        On success, the new compiled playbook replaces the previous version
        in the in-memory registry and is persisted to disk.

        On failure, the previous version remains active (unchanged in both
        memory and disk).  An error notification is emitted.

        Parameters
        ----------
        markdown:
            Raw content of the playbook ``.md`` file, including YAML
            frontmatter.
        source_path:
            Absolute path to the source ``.md`` file (for notifications).
        rel_path:
            Vault-relative path (for logging).
        force:
            When ``True``, skip the source hash check and always invoke the
            compiler.  Useful for manual recompilation commands.
        scope_identifier:
            Optional scope identifier for event-to-scope matching (roadmap
            5.3.3).  For project-scoped playbooks this is the ``project_id``;
            for agent-type playbooks it is extracted from the scope string
            automatically if not provided.

        Returns
        -------
        CompilationResult
            The compilation result from the underlying compiler.  When
            compilation is skipped, ``result.skipped`` is ``True`` and
            ``result.playbook`` is the existing active version.
        """
        if self._compiler is None:
            logger.info(
                "Playbook compilation skipped (no chat provider): %s",
                rel_path or source_path,
            )
            return CompilationResult(
                success=False,
                errors=["No chat provider configured for playbook compilation"],
            )

        # Determine playbook ID from frontmatter for version lookup
        frontmatter, _ = PlaybookCompiler._parse_frontmatter(markdown)
        playbook_id = frontmatter.get("id", "")

        # Get existing version for version increment
        existing = self._active.get(playbook_id)
        existing_version = existing.version if existing else 0

        # --- Source hash change detection (roadmap 5.1.5) ---
        # Compute the hash of the incoming markdown and compare against the
        # active compiled version.  If unchanged, skip the expensive LLM call.
        if not force and playbook_id:
            source_hash = PlaybookCompiler._compute_source_hash(markdown)

            if existing is not None and existing.source_hash == source_hash:
                logger.info(
                    "Playbook '%s' unchanged (hash=%s), skipping recompilation%s",
                    playbook_id,
                    source_hash,
                    f" ({rel_path})" if rel_path else "",
                )
                return CompilationResult(
                    success=True,
                    playbook=existing,
                    source_hash=source_hash,
                    skipped=True,
                )

        # Compile
        result = await self._compiler.compile(
            markdown,
            existing_version=existing_version,
        )

        if result.success and result.playbook is not None:
            # Success — update active version, trigger map, and persist
            old_playbook = self._active.get(result.playbook.id)
            if old_playbook is not None:
                self._unindex_triggers(old_playbook)
            self._active[result.playbook.id] = result.playbook
            self._index_triggers(result.playbook)
            self._source_paths[result.playbook.id] = source_path
            self._persist_compiled(result.playbook)
            # Track scope identifier for event-to-scope matching (5.3.3).
            # Prefer explicit identifier; fall back to parsing scope string.
            if scope_identifier is not None:
                self._scope_identifiers[result.playbook.id] = scope_identifier
            else:
                _, type_id = result.playbook.parse_scope()
                self._scope_identifiers[result.playbook.id] = type_id

            logger.info(
                "Playbook '%s' v%d now active (hash=%s, nodes=%d)%s",
                result.playbook.id,
                result.playbook.version,
                result.source_hash,
                len(result.playbook.nodes),
                f" [replaces v{existing_version}]" if existing_version else "",
            )

            await self._emit_compilation_succeeded(
                result.playbook,
                source_path=source_path,
                retries_used=result.retries_used,
            )
            self._refresh_subscriptions()
        else:
            # Failure — previous version stays active
            previous_version = existing.version if existing else None
            logger.error(
                "Playbook compilation failed for '%s' (%s): %s%s",
                playbook_id or "<unknown>",
                rel_path or source_path,
                "; ".join(result.errors),
                f" [v{previous_version} remains active]" if previous_version else "",
            )

            await self._emit_compilation_failed(
                playbook_id=playbook_id,
                source_path=source_path,
                errors=result.errors,
                previous_version=previous_version,
                source_hash=result.source_hash,
                retries_used=result.retries_used,
            )

        return result

    async def remove_playbook(self, playbook_id: str) -> bool:
        """Remove a playbook from the active registry and disk.

        Called when a playbook markdown file is deleted from the vault.

        Parameters
        ----------
        playbook_id:
            The ID of the playbook to remove.

        Returns
        -------
        bool
            ``True`` if the playbook was found and removed, ``False`` if
            it was not in the registry.
        """
        removed = self._active.pop(playbook_id, None)
        self._source_paths.pop(playbook_id, None)
        self._scope_identifiers.pop(playbook_id, None)

        if removed is not None:
            self._unindex_triggers(removed)
            self._remove_compiled(playbook_id)
            self.clear_cooldown(playbook_id)  # Clean up cooldown state
            self._refresh_subscriptions()
            logger.info(
                "Playbook '%s' removed from active registry (was v%d)",
                playbook_id,
                removed.version,
            )
            return True

        return False

    def playbook_id_by_source_path(self, source_path: str) -> str | None:
        """Find the active playbook id whose source .md sits at *source_path*.

        Used by vault-delete handlers to resolve the correct id even when
        the .md's frontmatter ``id`` doesn't match its filename stem
        (the handler's fallback-id heuristic).  Returns ``None`` when no
        match is found.
        """
        target = os.path.normpath(source_path)
        for pid, path in self._source_paths.items():
            if os.path.normpath(path) == target:
                return pid
        return None

    async def prune_orphan_compilations(self, vault_root: str) -> dict:
        """Delete compiled JSON for playbooks whose source .md is gone.

        Fixes the gap where a .md file deleted out-of-band (outside the
        vault-watcher lifecycle, e.g. manual `rm`, git checkout between
        daemon runs) leaves its compiled JSON behind.  The orphan
        silently loads on startup and keeps firing triggers for a
        playbook that no longer exists in the vault.

        Walks ``{data_dir}/playbooks/compiled/*.json``; for each compiled
        entry confirms that at least one `.md` file under *vault_root*
        carries a matching frontmatter id.  Anything without a backing
        `.md` is removed from disk and from the active registry.

        Parameters
        ----------
        vault_root:
            Absolute path to the vault root (typically
            ``{data_dir}/vault``).

        Returns
        -------
        dict
            Summary with ``pruned`` (list of ids removed) and
            ``checked`` (total compiled entries inspected).
        """
        compiled_dir = self._compiled_dir()
        result: dict = {"pruned": [], "checked": 0}
        if compiled_dir is None or not compiled_dir.is_dir():
            return result
        if not os.path.isdir(vault_root):
            return result

        # Build the set of ids currently present in the vault.
        from src.playbooks.handler import PLAYBOOK_PATTERNS
        import fnmatch

        vault_ids: set[str] = set()
        for dirpath, _dirnames, filenames in os.walk(vault_root):
            for filename in filenames:
                if not filename.endswith(".md"):
                    continue
                abs_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(abs_path, vault_root).replace("\\", "/")
                if not any(fnmatch.fnmatch(rel_path, p) for p in PLAYBOOK_PATTERNS):
                    continue
                try:
                    with open(abs_path, encoding="utf-8") as f:
                        frontmatter, _ = PlaybookCompiler._parse_frontmatter(f.read())
                except Exception:
                    continue
                pid = (frontmatter.get("id") or "").strip()
                if pid:
                    vault_ids.add(pid)

        # Inspect each compiled JSON and drop orphans.
        for json_path in sorted(compiled_dir.glob("*.json")):
            result["checked"] += 1
            compiled_id = json_path.stem
            if compiled_id in vault_ids:
                continue
            logger.info(
                "Pruning orphan compiled playbook %r — no matching .md in vault",
                compiled_id,
            )
            active = self._active.pop(compiled_id, None)
            self._source_paths.pop(compiled_id, None)
            self._scope_identifiers.pop(compiled_id, None)
            if active is not None:
                self._unindex_triggers(active)
                self.clear_cooldown(compiled_id)
            try:
                json_path.unlink()
            except OSError:
                logger.warning(
                    "Failed to unlink orphan compiled playbook %s",
                    json_path,
                    exc_info=True,
                )
                continue
            result["pruned"].append(compiled_id)

        if result["pruned"]:
            self._refresh_subscriptions()
            logger.info(
                "Pruned %d orphan compiled playbook(s): %s",
                len(result["pruned"]),
                ", ".join(result["pruned"]),
            )
        return result

    # -- trigger mapping -----------------------------------------------------

    def _index_triggers(self, playbook: CompiledPlaybook) -> None:
        """Add a playbook's triggers to the trigger mapping.

        For each trigger event type in the playbook, adds the playbook's
        ID to the corresponding set in ``_trigger_map``.
        """
        for trigger in playbook.triggers:
            event_type = trigger.event_type
            if event_type not in self._trigger_map:
                self._trigger_map[event_type] = set()
            self._trigger_map[event_type].add(playbook.id)

    def _unindex_triggers(self, playbook: CompiledPlaybook) -> None:
        """Remove a playbook's triggers from the trigger mapping.

        For each trigger event type in the playbook, removes the playbook's
        ID from the corresponding set in ``_trigger_map``.  Cleans up empty
        sets to keep the mapping tidy.
        """
        for trigger in playbook.triggers:
            event_type = trigger.event_type
            ids = self._trigger_map.get(event_type)
            if ids is not None:
                ids.discard(playbook.id)
                if not ids:
                    del self._trigger_map[event_type]

    def _rebuild_trigger_map(self) -> None:
        """Rebuild the trigger mapping from scratch.

        Clears the existing mapping and re-indexes all active playbooks.
        Useful after bulk operations (e.g. loading from disk).
        """
        self._trigger_map.clear()
        for playbook in self._active.values():
            self._index_triggers(playbook)

    # -- persistence ---------------------------------------------------------

    def _compiled_dir(self) -> Path | None:
        """Return the path to the compiled playbooks directory, or None."""
        if self._data_dir is None:
            return None
        return Path(self._data_dir) / "playbooks" / "compiled"

    def _persist_compiled(self, playbook: CompiledPlaybook) -> None:
        """Write the compiled playbook JSON to disk."""
        compiled_dir = self._compiled_dir()
        if compiled_dir is None:
            return

        try:
            compiled_dir.mkdir(parents=True, exist_ok=True)
            json_path = compiled_dir / f"{playbook.id}.json"
            json_path.write_text(
                json.dumps(playbook.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
            logger.debug("Persisted compiled playbook to %s", json_path)
        except OSError:
            logger.error(
                "Failed to persist compiled playbook '%s'",
                playbook.id,
                exc_info=True,
            )

    def _remove_compiled(self, playbook_id: str) -> None:
        """Remove the compiled playbook JSON from disk."""
        compiled_dir = self._compiled_dir()
        if compiled_dir is None:
            return

        json_path = compiled_dir / f"{playbook_id}.json"
        try:
            if json_path.exists():
                json_path.unlink()
                logger.debug("Removed compiled playbook file %s", json_path)
        except OSError:
            logger.error(
                "Failed to remove compiled playbook file for '%s'",
                playbook_id,
                exc_info=True,
            )

    # -- notifications -------------------------------------------------------

    async def _emit_compilation_failed(
        self,
        *,
        playbook_id: str,
        source_path: str,
        errors: list[str],
        previous_version: int | None,
        source_hash: str,
        retries_used: int,
    ) -> None:
        """Emit a ``notify.playbook_compilation_failed`` event."""
        if self._event_bus is None:
            return
        try:
            from src.notifications.events import PlaybookCompilationFailedEvent

            event = PlaybookCompilationFailedEvent(
                playbook_id=playbook_id,
                source_path=source_path,
                errors=errors,
                previous_version=previous_version,
                source_hash=source_hash,
                retries_used=retries_used,
            )
            await self._event_bus.emit(event.event_type, event.model_dump(mode="json"))
        except Exception:
            logger.debug("Failed to emit playbook compilation failed event", exc_info=True)

    async def _emit_compilation_succeeded(
        self,
        playbook: CompiledPlaybook,
        *,
        source_path: str,
        retries_used: int,
    ) -> None:
        """Emit a ``notify.playbook_compilation_succeeded`` event."""
        if self._event_bus is None:
            return
        try:
            from src.notifications.events import PlaybookCompilationSucceededEvent

            event = PlaybookCompilationSucceededEvent(
                playbook_id=playbook.id,
                source_path=source_path,
                version=playbook.version,
                source_hash=playbook.source_hash,
                node_count=len(playbook.nodes),
                retries_used=retries_used,
            )
            await self._event_bus.emit(event.event_type, event.model_dump(mode="json"))
        except Exception:
            logger.debug("Failed to emit playbook compilation succeeded event", exc_info=True)
