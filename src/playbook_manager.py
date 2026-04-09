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

**Error handling policy** (spec §4, roadmap 5.1.7):

    If compilation fails (invalid output, LLM error, validation failure),
    the previous compiled version remains active and an error notification
    is surfaced via the EventBus.  This is atomic — a partially valid
    compilation never replaces a working version.

See ``docs/specs/design/playbooks.md`` Section 4 for the specification.
"""

from __future__ import annotations

import json
import logging
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
    """

    def __init__(
        self,
        *,
        chat_provider: ChatProvider | None = None,
        event_bus: EventBus | None = None,
        data_dir: str | None = None,
        store: CompiledPlaybookStore | None = None,
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
