"""Filesystem watcher for hook-driven automation.

Monitors files and directories for changes using mtime polling (same approach
as ConfigWatcher) and emits EventBus events when changes are detected.  This
enables hooks to react to file modifications, new files, and deletions without
requiring OS-specific inotify/FSEvents integration.

Supports two watch types:

- **File watches**: monitor specific files (e.g., ``config.yaml``,
  ``pyproject.toml``).  Emits ``file.changed`` events with the file path,
  operation (modified/created/deleted), and optional diff.

- **Folder watches**: monitor directories (optionally recursive).  Emits
  ``folder.changed`` events with a list of changed files, aggregated over
  a debounce window to avoid event storms from bulk operations (e.g.,
  ``git checkout`` touching many files at once).

Watch rules are stored in the database as JSON in the hook's trigger config::

    {
        "type": "event",
        "event_type": "file.changed",
        "watch": {
            "paths": ["pyproject.toml", "src/config.py"],
            "project_id": "my-project"
        }
    }

    {
        "type": "event",
        "event_type": "folder.changed",
        "watch": {
            "paths": ["src/", "tests/"],
            "recursive": true,
            "extensions": [".py", ".ts"],
            "project_id": "my-project"
        }
    }

Integration:
    The FileWatcher is created by HookEngine at ``initialize()`` and scans
    watches on each ``tick()`` call.  It holds a reference to the EventBus
    for emitting change events.  The orchestrator's existing tick loop
    drives the polling cycle.

See ``specs/hooks.md`` for the file/folder watch trigger specification.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from src.event_bus import EventBus

logger = logging.getLogger(__name__)


@dataclass
class WatchRule:
    """A single file or folder watch configuration.

    Attributes:
        watch_id: Unique identifier (typically the hook ID).
        project_id: The project this watch belongs to.
        paths: List of file/folder paths to monitor.
        recursive: Whether folder watches descend into subdirectories.
        extensions: Optional filter — only track files with these extensions.
        watch_type: ``"file"`` or ``"folder"``.
        base_dir: Base directory to resolve relative paths against.
    """

    watch_id: str
    project_id: str
    paths: list[str]
    recursive: bool = False
    extensions: list[str] | None = None
    watch_type: str = "file"  # "file" or "folder"
    base_dir: str = ""


@dataclass
class _FileState:
    """Tracks mtime and size for a single file."""

    mtime: float = 0.0
    size: int = 0
    exists: bool = True


class FileWatcher:
    """Polls filesystem for changes and emits EventBus events.

    Uses mtime-based polling (not inotify) for portability across Linux,
    macOS, and WSL.  Polling happens on each ``check()`` call, which is
    driven by the hook engine's ``tick()`` cycle (~5s).

    Change detection:
        - **Files**: compares mtime + size against last known state.
        - **Folders**: scans directory listing and compares against snapshot.
        - **Debouncing**: folder changes are aggregated over a configurable
          window (default 5s) before emitting a single event.

    Event payloads:
        ``file.changed``:
            ``{path, project_id, operation, old_mtime, new_mtime, size}``
        ``folder.changed``:
            ``{path, project_id, changes: [{path, operation}], count}``
    """

    def __init__(
        self,
        bus: EventBus,
        debounce_seconds: float = 5.0,
        poll_interval: float = 10.0,
    ):
        self.bus = bus
        self.debounce_seconds = debounce_seconds
        self.poll_interval = poll_interval

        # Watch rules keyed by watch_id
        self._watches: dict[str, WatchRule] = {}

        # File state snapshots: {absolute_path: _FileState}
        self._file_states: dict[str, _FileState] = {}

        # Folder state snapshots: {watch_dir: {relative_path: _FileState}}
        self._folder_states: dict[str, dict[str, _FileState]] = {}

        # Pending folder changes for debouncing: {watch_dir: [(path, op, time)]}
        self._pending_folder_changes: dict[str, list[tuple[str, str, float]]] = {}

        # Last poll time
        self._last_poll: float = 0.0

    def add_watch(self, rule: WatchRule) -> None:
        """Register a new watch rule.

        Immediately snapshots the current state of watched paths so the
        first poll only detects changes that happen AFTER registration.
        """
        self._watches[rule.watch_id] = rule

        for path in rule.paths:
            abs_path = self._resolve_path(path, rule.base_dir)
            if rule.watch_type == "file":
                self._snapshot_file(abs_path)
            elif rule.watch_type == "folder":
                self._snapshot_folder(abs_path, rule)

    def remove_watch(self, watch_id: str) -> None:
        """Remove a watch rule and clean up its state."""
        rule = self._watches.pop(watch_id, None)
        if not rule:
            return

        for path in rule.paths:
            abs_path = self._resolve_path(path, rule.base_dir)
            if rule.watch_type == "file":
                self._file_states.pop(abs_path, None)
            elif rule.watch_type == "folder":
                self._folder_states.pop(abs_path, None)
                self._pending_folder_changes.pop(abs_path, None)

    async def check(self) -> None:
        """Poll all watch rules for changes.

        Called by HookEngine.tick() on each orchestrator cycle.  Respects
        ``poll_interval`` to avoid excessive filesystem access.
        """
        now = time.time()
        if now - self._last_poll < self.poll_interval:
            return
        self._last_poll = now

        for watch_id, rule in list(self._watches.items()):
            try:
                if rule.watch_type == "file":
                    await self._check_file_watch(rule)
                elif rule.watch_type == "folder":
                    await self._check_folder_watch(rule, now)
            except Exception as e:
                logger.warning("FileWatcher error checking %s: %s", watch_id, e)

        # Flush debounced folder events
        await self._flush_pending_folder_changes(now)

    async def _check_file_watch(self, rule: WatchRule) -> None:
        """Check individual file watches for changes."""
        for path in rule.paths:
            abs_path = self._resolve_path(path, rule.base_dir)
            old_state = self._file_states.get(abs_path)

            try:
                stat = os.stat(abs_path)
                new_state = _FileState(
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    exists=True,
                )
            except FileNotFoundError:
                new_state = _FileState(exists=False)

            if old_state is None:
                # First time seeing this file — snapshot and move on
                self._file_states[abs_path] = new_state
                continue

            operation = self._detect_file_operation(old_state, new_state)
            if operation:
                await self.bus.emit(
                    "file.changed",
                    {
                        "path": abs_path,
                        "relative_path": path,
                        "project_id": rule.project_id,
                        "operation": operation,
                        "old_mtime": old_state.mtime,
                        "new_mtime": new_state.mtime,
                        "size": new_state.size,
                        "watch_id": rule.watch_id,
                    },
                )
                self._file_states[abs_path] = new_state

    async def _check_folder_watch(self, rule: WatchRule, now: float) -> None:
        """Check folder watches for added/modified/deleted files."""
        for path in rule.paths:
            abs_path = self._resolve_path(path, rule.base_dir)
            if not os.path.isdir(abs_path):
                continue

            old_snapshot = self._folder_states.get(abs_path, {})
            new_snapshot = self._scan_folder(abs_path, rule)

            changes = []

            # Check for new or modified files
            for rel_path, new_state in new_snapshot.items():
                old_state = old_snapshot.get(rel_path)
                if old_state is None:
                    changes.append((rel_path, "created"))
                elif new_state.mtime != old_state.mtime or new_state.size != old_state.size:
                    changes.append((rel_path, "modified"))

            # Check for deleted files
            for rel_path in old_snapshot:
                if rel_path not in new_snapshot:
                    changes.append((rel_path, "deleted"))

            if changes:
                pending = self._pending_folder_changes.setdefault(abs_path, [])
                for rel_path, op in changes:
                    pending.append((rel_path, op, now))

            # Always update the snapshot
            self._folder_states[abs_path] = new_snapshot

    async def _flush_pending_folder_changes(self, now: float) -> None:
        """Emit folder.changed events for debounced changes."""
        flushed_dirs = []

        for watch_dir, pending in self._pending_folder_changes.items():
            if not pending:
                continue

            # Check if enough time has passed since the last change
            latest_change_time = max(t for _, _, t in pending)
            if now - latest_change_time < self.debounce_seconds:
                continue  # Still accumulating changes

            # Find the watch rule for this directory
            rule = self._find_rule_for_path(watch_dir)
            if not rule:
                flushed_dirs.append(watch_dir)
                continue

            # Deduplicate: if a file was created then modified, just report created
            # If modified then deleted, just report deleted
            final_changes: dict[str, str] = {}
            for rel_path, op, _ in pending:
                existing_op = final_changes.get(rel_path)
                if existing_op == "created" and op == "modified":
                    continue  # Keep "created"
                if existing_op == "created" and op == "deleted":
                    del final_changes[rel_path]  # Cancel out
                    continue
                final_changes[rel_path] = op

            if final_changes:
                await self.bus.emit(
                    "folder.changed",
                    {
                        "path": watch_dir,
                        "project_id": rule.project_id,
                        "changes": [
                            {"path": p, "operation": op} for p, op in final_changes.items()
                        ],
                        "count": len(final_changes),
                        "watch_id": rule.watch_id,
                    },
                )

            flushed_dirs.append(watch_dir)

        for d in flushed_dirs:
            self._pending_folder_changes.pop(d, None)

    def _scan_folder(self, folder_path: str, rule: WatchRule) -> dict[str, _FileState]:
        """Scan a directory and return a snapshot of all matching files."""
        snapshot: dict[str, _FileState] = {}
        extensions = set(rule.extensions) if rule.extensions else None

        try:
            if rule.recursive:
                for dirpath, dirnames, filenames in os.walk(folder_path):
                    # Skip hidden directories
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    for fname in filenames:
                        if fname.startswith("."):
                            continue
                        if extensions and not any(fname.endswith(ext) for ext in extensions):
                            continue
                        full_path = os.path.join(dirpath, fname)
                        rel_path = os.path.relpath(full_path, folder_path)
                        try:
                            stat = os.stat(full_path)
                            snapshot[rel_path] = _FileState(
                                mtime=stat.st_mtime,
                                size=stat.st_size,
                                exists=True,
                            )
                        except (OSError, FileNotFoundError):
                            pass
            else:
                for fname in os.listdir(folder_path):
                    if fname.startswith("."):
                        continue
                    full_path = os.path.join(folder_path, fname)
                    if not os.path.isfile(full_path):
                        continue
                    if extensions and not any(fname.endswith(ext) for ext in extensions):
                        continue
                    try:
                        stat = os.stat(full_path)
                        snapshot[fname] = _FileState(
                            mtime=stat.st_mtime,
                            size=stat.st_size,
                            exists=True,
                        )
                    except (OSError, FileNotFoundError):
                        pass
        except (OSError, PermissionError) as e:
            logger.warning("Cannot scan folder %s: %s", folder_path, e)

        return snapshot

    def _snapshot_file(self, abs_path: str) -> None:
        """Take an initial snapshot of a file's state."""
        try:
            stat = os.stat(abs_path)
            self._file_states[abs_path] = _FileState(
                mtime=stat.st_mtime, size=stat.st_size, exists=True
            )
        except FileNotFoundError:
            self._file_states[abs_path] = _FileState(exists=False)

    def _snapshot_folder(self, abs_path: str, rule: WatchRule) -> None:
        """Take an initial snapshot of a folder's contents."""
        if os.path.isdir(abs_path):
            self._folder_states[abs_path] = self._scan_folder(abs_path, rule)
        else:
            self._folder_states[abs_path] = {}

    @staticmethod
    def _detect_file_operation(old: _FileState, new: _FileState) -> str | None:
        """Compare two file states and return the operation, or None."""
        if old.exists and not new.exists:
            return "deleted"
        if not old.exists and new.exists:
            return "created"
        if old.exists and new.exists:
            if old.mtime != new.mtime or old.size != new.size:
                return "modified"
        return None

    @staticmethod
    def _resolve_path(path: str, base_dir: str) -> str:
        """Resolve a potentially relative path against a base directory."""
        if os.path.isabs(path):
            return path
        if base_dir:
            return os.path.join(base_dir, path)
        return os.path.abspath(path)

    def _find_rule_for_path(self, abs_path: str) -> WatchRule | None:
        """Find the watch rule that contains a given absolute path."""
        for rule in self._watches.values():
            for path in rule.paths:
                resolved = self._resolve_path(path, rule.base_dir)
                if resolved == abs_path:
                    return rule
        return None

    def get_watch_count(self) -> int:
        """Return the number of active watch rules."""
        return len(self._watches)
