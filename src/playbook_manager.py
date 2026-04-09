"""Playbook lifecycle manager — compilation, versioning, and error handling.

Manages the lifecycle of compiled playbooks: triggers compilation when
playbook markdown files change, persists compiled JSON to disk, maintains
an in-memory registry of active playbooks, and ensures that the previous
compiled version remains active when compilation fails.

The manager is the integration layer between:

- :class:`~src.playbook_compiler.PlaybookCompiler` — LLM compilation
- :class:`~src.playbook_store.CompiledPlaybookStore` — scope-mirrored storage
- :class:`~src.event_bus.EventBus` — error/success notifications
- Filesystem — persistence of compiled JSON artifacts

**Trigger mapping** (roadmap 5.3.1):

    The manager maintains an explicit ``trigger → playbook IDs`` mapping
    that is updated on every add/remove/compile operation.  This enables
    O(1) lookup when an event arrives, instead of scanning all active
    playbooks.

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
import time
from pathlib import Path
from typing import TYPE_CHECKING

from src.playbook_compiler import CompilationResult, PlaybookCompiler
from src.playbook_models import CompiledPlaybook

if TYPE_CHECKING:
    from src.chat_providers.base import ChatProvider
    from src.event_bus import EventBus
    from src.playbook_store import CompiledPlaybookStore

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
        Optional :class:`~src.playbook_store.CompiledPlaybookStore` for
        scope-mirrored storage.  When provided, :meth:`load_from_store`
        uses it to load all compiled playbooks across all scopes on
        startup.  The legacy ``data_dir`` flat-directory persistence is
        used as a fallback when no store is provided.
    max_concurrent_runs:
        Maximum number of playbook runs that can execute simultaneously.
        Defaults to ``2``.  Set to ``0`` for unlimited.  Mirrors the
        hook engine's ``max_concurrent_hooks`` setting.
    """

    def __init__(
        self,
        *,
        chat_provider: ChatProvider | None = None,
        event_bus: EventBus | None = None,
        data_dir: str | None = None,
        store: CompiledPlaybookStore | None = None,
        max_concurrent_runs: int = 2,
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

        # Compiler instance (created lazily when provider is available)
        self._compiler: PlaybookCompiler | None = None
        if chat_provider is not None:
            self._compiler = PlaybookCompiler(chat_provider)

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

        Uses ``asyncio.gather(..., return_exceptions=True)`` to ensure
        CancelledError does not propagate.  Clears all tracking state.
        """
        for rid, task in self._running.items():
            if not task.done():
                logger.info("Cancelling playbook run '%s'", rid)
                task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()
        self._running_playbook_ids.clear()

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
        return loaded

    async def compile_playbook(
        self,
        markdown: str,
        *,
        source_path: str = "",
        rel_path: str = "",
        force: bool = False,
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

        if removed is not None:
            self._unindex_triggers(removed)
            self._remove_compiled(playbook_id)
            self.clear_cooldown(playbook_id)  # Clean up cooldown state
            logger.info(
                "Playbook '%s' removed from active registry (was v%d)",
                playbook_id,
                removed.version,
            )
            return True

        return False

    # -- trigger mapping -----------------------------------------------------

    def _index_triggers(self, playbook: CompiledPlaybook) -> None:
        """Add a playbook's triggers to the trigger mapping.

        For each trigger event type in the playbook, adds the playbook's
        ID to the corresponding set in ``_trigger_map``.
        """
        for trigger in playbook.triggers:
            if trigger not in self._trigger_map:
                self._trigger_map[trigger] = set()
            self._trigger_map[trigger].add(playbook.id)

    def _unindex_triggers(self, playbook: CompiledPlaybook) -> None:
        """Remove a playbook's triggers from the trigger mapping.

        For each trigger event type in the playbook, removes the playbook's
        ID from the corresponding set in ``_trigger_map``.  Cleans up empty
        sets to keep the mapping tidy.
        """
        for trigger in playbook.triggers:
            ids = self._trigger_map.get(trigger)
            if ids is not None:
                ids.discard(playbook.id)
                if not ids:
                    del self._trigger_map[trigger]

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
