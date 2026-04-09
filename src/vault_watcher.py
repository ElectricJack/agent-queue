"""Unified vault file watcher with path-based dispatch.

Monitors the entire ``~/.agent-queue/vault/`` directory tree for changes and
dispatches them to registered handlers based on glob path patterns.  This is
the single watcher described in the playbooks spec Section 17 — one watcher,
one debounce strategy, one log stream.

Handlers register interest via glob patterns relative to the vault root::

    watcher.register_handler("*/playbooks/*.md", on_playbook_changed)
    watcher.register_handler("*/profile.md", on_profile_changed)
    watcher.register_handler("*/memory/**/*.md", on_memory_changed)
    watcher.register_handler("projects/*/README.md", on_readme_changed)

When files matching a pattern are created, modified, or deleted, the watcher
accumulates changes over a configurable debounce window, then dispatches a
single batch per handler.  This avoids handler storms during bulk operations
like ``git checkout`` or large file syncs.

Uses the same mtime-based polling approach as :class:`~src.file_watcher.FileWatcher`
for portability across Linux, macOS, and WSL.

Integration:
    The VaultWatcher is created by the orchestrator at ``initialize()`` and
    runs a background polling loop via :meth:`start`.  It is stopped at
    ``shutdown()``.  Specific path handlers are wired in subsequent tasks.

See ``docs/specs/design/playbooks.md`` Section 17 for the specification.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VaultChange:
    """A single file change detected in the vault.

    Attributes:
        path: Absolute filesystem path to the changed file.
        rel_path: Path relative to the vault root (e.g.
            ``projects/my-app/playbooks/deploy.md``).
        operation: One of ``"created"``, ``"modified"``, or ``"deleted"``.
    """

    path: str
    rel_path: str
    operation: str  # "created" | "modified" | "deleted"


@dataclass
class _HandlerEntry:
    """Internal registration for a path-pattern handler.

    Attributes:
        handler_id: Unique identifier for this registration.
        pattern: Glob pattern matched against relative vault paths.
        handler: Async callable invoked with a list of matching changes.
    """

    handler_id: str
    pattern: str
    handler: Callable[[list[VaultChange]], object]


@dataclass
class _FileState:
    """Tracks mtime and size for a single file."""

    mtime: float = 0.0
    size: int = 0


class VaultWatcher:
    """Polls the vault directory tree for changes and dispatches to handlers.

    Uses mtime-based polling (not inotify) for portability.  Polling happens
    on each :meth:`check` call, which can be driven by a background loop
    (:meth:`start`) or called manually (useful for testing).

    Change detection:
        Scans the entire vault tree recursively, comparing mtime + size
        against the last known snapshot.  New files, modified files, and
        deleted files are all detected.

    Debouncing:
        Detected changes are accumulated in a pending buffer.  Once the
        debounce window elapses (no new changes for ``debounce_seconds``),
        accumulated changes are grouped by matching handler pattern and
        dispatched in a single batch per handler.

    Parameters
    ----------
    vault_root:
        Absolute path to the vault directory (e.g. ``~/.agent-queue/vault``).
    poll_interval:
        Minimum seconds between filesystem scans.  Default 5.0.
    debounce_seconds:
        Seconds to wait after the last detected change before dispatching.
        Default 2.0.
    """

    def __init__(
        self,
        vault_root: str,
        poll_interval: float = 5.0,
        debounce_seconds: float = 2.0,
    ) -> None:
        self.vault_root = vault_root
        self.poll_interval = poll_interval
        self.debounce_seconds = debounce_seconds

        # Registered handlers: {handler_id: _HandlerEntry}
        self._handlers: dict[str, _HandlerEntry] = {}

        # File state snapshot: {relative_path: _FileState}
        self._file_states: dict[str, _FileState] = {}

        # Pending changes awaiting debounce flush: [(VaultChange, timestamp)]
        self._pending: list[tuple[VaultChange, float]] = []

        # Last poll time
        self._last_poll: float = 0.0

        # Whether initial snapshot has been taken
        self._initialized: bool = False

        # Background task handle
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def register_handler(
        self,
        pattern: str,
        handler: Callable[[list[VaultChange]], object],
        handler_id: str | None = None,
    ) -> str:
        """Register a handler for vault paths matching *pattern*.

        The *pattern* is a glob expression matched against paths relative
        to the vault root.  Use ``**`` for recursive directory matching
        and ``*`` for single-level wildcards.

        Examples::

            watcher.register_handler("*/playbooks/*.md", on_playbook_changed)
            watcher.register_handler("projects/*/memory/**/*.md", on_memory)
            watcher.register_handler("system/memory/*.md", on_system_memory)

        Parameters
        ----------
        pattern:
            Glob pattern matched against relative vault paths.
        handler:
            Async or sync callable receiving ``list[VaultChange]``.
        handler_id:
            Optional explicit ID.  Auto-generated if omitted.

        Returns
        -------
        str
            The handler ID (for use with :meth:`unregister_handler`).
        """
        hid = handler_id or str(uuid.uuid4())
        self._handlers[hid] = _HandlerEntry(
            handler_id=hid,
            pattern=pattern,
            handler=handler,
        )
        logger.debug("VaultWatcher: registered handler %s for pattern %r", hid, pattern)
        return hid

    def unregister_handler(self, handler_id: str) -> bool:
        """Remove a previously registered handler.

        Returns ``True`` if the handler was found and removed, ``False``
        if no handler with that ID exists.
        """
        entry = self._handlers.pop(handler_id, None)
        if entry:
            logger.debug(
                "VaultWatcher: unregistered handler %s (pattern %r)",
                handler_id,
                entry.pattern,
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background polling loop.

        Takes an initial snapshot of the vault tree (so only changes
        *after* start are detected) and begins periodic polling.
        """
        if self._task and not self._task.done():
            logger.warning("VaultWatcher: already running")
            return

        if not self._initialized:
            self._snapshot()
            self._initialized = True
            logger.info(
                "VaultWatcher: initial snapshot of %s (%d files)",
                self.vault_root,
                len(self._file_states),
            )

        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "VaultWatcher: started (poll=%.1fs, debounce=%.1fs)",
            self.poll_interval,
            self.debounce_seconds,
        )

    async def stop(self) -> None:
        """Stop the background polling loop and flush pending changes."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

        # Flush any remaining pending changes
        if self._pending:
            await self._flush_pending(force=True)

        logger.info("VaultWatcher: stopped")

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def check(self) -> list[VaultChange]:
        """Poll the vault for changes and dispatch to handlers.

        This is the main entry point for change detection.  It can be
        called directly (e.g. in tests) or is called automatically by
        the background loop started via :meth:`start`.

        Returns a list of *new* changes detected in this cycle (before
        debounce filtering).  Changes are not dispatched to handlers
        until the debounce window elapses.
        """
        now = time.time()

        # Respect poll interval
        if now - self._last_poll < self.poll_interval:
            # Still flush pending if debounce window has elapsed
            await self._flush_pending()
            return []

        self._last_poll = now

        # Take initial snapshot if not done yet
        if not self._initialized:
            self._snapshot()
            self._initialized = True
            return []

        # Scan and detect changes
        changes = self._detect_changes()

        if changes:
            for change in changes:
                self._pending.append((change, now))
            logger.debug(
                "VaultWatcher: detected %d change(s) in vault",
                len(changes),
            )

        # Attempt to flush pending changes (respects debounce)
        await self._flush_pending()

        return changes

    # ------------------------------------------------------------------
    # Internal: scanning and change detection
    # ------------------------------------------------------------------

    def _snapshot(self) -> None:
        """Take a full snapshot of the vault directory tree."""
        self._file_states = self._scan_tree()

    def _scan_tree(self) -> dict[str, _FileState]:
        """Walk the vault directory and return a snapshot of all files."""
        snapshot: dict[str, _FileState] = {}

        if not os.path.isdir(self.vault_root):
            return snapshot

        try:
            for dirpath, dirnames, filenames in os.walk(self.vault_root):
                # Skip hidden directories (e.g. .obsidian)
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]

                for fname in filenames:
                    if fname.startswith("."):
                        continue

                    full_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(full_path, self.vault_root)

                    try:
                        stat = os.stat(full_path)
                        snapshot[rel_path] = _FileState(
                            mtime=stat.st_mtime,
                            size=stat.st_size,
                        )
                    except (OSError, FileNotFoundError):
                        pass
        except (OSError, PermissionError) as e:
            logger.warning("VaultWatcher: cannot scan vault tree: %s", e)

        return snapshot

    def _detect_changes(self) -> list[VaultChange]:
        """Compare current filesystem state against the last snapshot.

        Updates the internal snapshot as a side-effect.
        """
        new_snapshot = self._scan_tree()
        changes: list[VaultChange] = []

        # Check for new and modified files
        for rel_path, new_state in new_snapshot.items():
            old_state = self._file_states.get(rel_path)
            abs_path = os.path.join(self.vault_root, rel_path)

            if old_state is None:
                changes.append(
                    VaultChange(
                        path=abs_path,
                        rel_path=rel_path,
                        operation="created",
                    )
                )
            elif new_state.mtime != old_state.mtime or new_state.size != old_state.size:
                changes.append(
                    VaultChange(
                        path=abs_path,
                        rel_path=rel_path,
                        operation="modified",
                    )
                )

        # Check for deleted files
        for rel_path in self._file_states:
            if rel_path not in new_snapshot:
                abs_path = os.path.join(self.vault_root, rel_path)
                changes.append(
                    VaultChange(
                        path=abs_path,
                        rel_path=rel_path,
                        operation="deleted",
                    )
                )

        # Update snapshot
        self._file_states = new_snapshot

        return changes

    # ------------------------------------------------------------------
    # Internal: debouncing and dispatch
    # ------------------------------------------------------------------

    async def _flush_pending(self, force: bool = False) -> None:
        """Dispatch accumulated changes if the debounce window has elapsed.

        Parameters
        ----------
        force:
            If ``True``, flush immediately regardless of debounce timing.
            Used during shutdown to avoid losing queued changes.
        """
        if not self._pending:
            return

        now = time.time()
        latest_time = max(t for _, t in self._pending)

        if not force and (now - latest_time) < self.debounce_seconds:
            return  # Still within debounce window

        # Deduplicate: keep the latest operation for each path
        # If created then modified → created
        # If created then deleted → remove entirely
        # If modified then deleted → deleted
        final: dict[str, VaultChange] = {}
        for change, _ in self._pending:
            existing = final.get(change.rel_path)
            if existing is None:
                final[change.rel_path] = change
            elif existing.operation == "created" and change.operation == "modified":
                pass  # Keep "created"
            elif existing.operation == "created" and change.operation == "deleted":
                del final[change.rel_path]  # Cancel out
            else:
                final[change.rel_path] = change

        self._pending.clear()

        if not final:
            return

        all_changes = list(final.values())
        logger.info(
            "VaultWatcher: dispatching %d change(s) after debounce",
            len(all_changes),
        )

        # Group changes by matching handlers and dispatch
        await self._dispatch(all_changes)

    async def _dispatch(self, changes: list[VaultChange]) -> None:
        """Route changes to registered handlers based on pattern matching."""
        for entry in list(self._handlers.values()):
            matched = [c for c in changes if self._matches_pattern(c.rel_path, entry.pattern)]
            if not matched:
                continue

            logger.debug(
                "VaultWatcher: dispatching %d change(s) to handler %s (pattern %r)",
                len(matched),
                entry.handler_id,
                entry.pattern,
            )

            try:
                result = entry.handler(matched)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "VaultWatcher: handler %s (pattern %r) raised an exception",
                    entry.handler_id,
                    entry.pattern,
                )

    @staticmethod
    def _matches_pattern(rel_path: str, pattern: str) -> bool:
        """Test whether a relative vault path matches a glob pattern.

        Handles ``**`` for recursive matching.  ``**`` matches zero or
        more path segments (directories), while ``*`` matches within a
        single segment.

        Path separators are normalised to ``/`` for consistent matching
        across platforms.

        Examples::

            _matches_pattern("system/playbooks/deploy.md", "*/playbooks/*.md")  → True
            _matches_pattern("projects/app/memory/k/a.md", "**/memory/**/*.md") → True
            _matches_pattern("a/b/c/d.md", "**/*.md")                          → True
        """
        # Normalise separators
        rel_path = rel_path.replace(os.sep, "/")
        pattern = pattern.replace(os.sep, "/")

        if "**" not in pattern:
            return fnmatch.fnmatch(rel_path, pattern)

        # Split pattern into segments and match recursively
        return VaultWatcher._match_segments(
            rel_path.split("/"),
            pattern.split("/"),
        )

    @staticmethod
    def _match_segments(path_parts: list[str], pattern_parts: list[str]) -> bool:
        """Recursively match path segments against pattern segments.

        ``**`` in *pattern_parts* matches zero or more path segments.
        Other segments use :func:`fnmatch.fnmatch` for wildcard matching.
        """
        if not pattern_parts:
            return not path_parts

        head = pattern_parts[0]
        rest_pattern = pattern_parts[1:]

        if head == "**":
            # Skip consecutive ** segments
            while rest_pattern and rest_pattern[0] == "**":
                rest_pattern = rest_pattern[1:]

            # ** matches zero or more path segments
            # Try matching the rest of the pattern at every position
            for i in range(len(path_parts) + 1):
                if VaultWatcher._match_segments(path_parts[i:], rest_pattern):
                    return True
            return False

        if not path_parts:
            return False

        if fnmatch.fnmatch(path_parts[0], head):
            return VaultWatcher._match_segments(path_parts[1:], rest_pattern)

        return False

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Background coroutine that calls :meth:`check` periodically."""
        while True:
            try:
                await asyncio.sleep(self.poll_interval)
                await self.check()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("VaultWatcher: error in poll loop")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_handler_count(self) -> int:
        """Return the number of registered handlers."""
        return len(self._handlers)

    def get_tracked_file_count(self) -> int:
        """Return the number of files currently being tracked."""
        return len(self._file_states)

    def get_pending_change_count(self) -> int:
        """Return the number of changes awaiting debounce flush."""
        return len(self._pending)
