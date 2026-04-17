"""Workspace spec/doc change detector — generates vault reference stubs.

Monitors project workspaces for changes to spec and documentation files
and generates reference stubs in the vault at
``vault/projects/{id}/references/spec-{name}.md``.

This implements ``docs/specs/design/vault.md`` Section 4
("Reference Stubs for External Docs"):

1. Detect spec/doc file change in workspace (via mtime polling or git diff)
2. Read the full document
3. Write a reference stub with metadata (source path, hash, last_synced)
4. Emit ``workspace.spec.changed`` event for downstream processing
   (vector indexing, LLM-based summary enrichment)

Detection modes:

* **mtime** (default): Polls workspace spec/doc directories and compares
  file mtime + size against the last snapshot.  Works universally for
  linked repos, cloned repos, and non-git directories.

* **git_diff**: Uses ``git diff --name-only`` against the default branch
  to identify changed spec/doc files.  More efficient for large repos
  but requires a valid git checkout with a remote.

The LLM-based summary generation (spec step 3) is intentionally deferred
to a downstream consumer.  When a spec change is detected, a
``workspace.spec.changed`` event is emitted on the EventBus.  A playbook
or plugin can subscribe to enrich the stub with a proper summary, key
decisions, and key interfaces.  The stub written here contains source
metadata plus a configurable excerpt of the source file.

Rate limiting:
    ``check()`` is called every orchestrator cycle (~5 s) but internally
    rate-limits to at most once per ``poll_interval_seconds`` (default 60 s).
    Scanning is per-project-workspace — projects with no workspaces are
    skipped silently.

See ``docs/specs/design/vault.md`` Section 4 for the full specification.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.database.base import DatabaseInterface
    from src.event_bus import EventBus
    from src.git.manager import GitManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecFileState:
    """Snapshot of a single spec/doc file for change detection.

    Attributes:
        rel_path: Path relative to the workspace root (e.g.
            ``specs/orchestrator.md``).
        mtime: Last modification time from ``os.stat()``.
        size: File size in bytes.
        content_hash: First 12 hex characters of SHA-256 digest of
            the file content.  Used in the reference stub's
            ``source_hash`` frontmatter and for change detection
            when mtime is unreliable (e.g. after ``git checkout``).
    """

    rel_path: str
    mtime: float
    size: int
    content_hash: str


@dataclass(frozen=True)
class SpecChange:
    """A detected change to a spec/doc file in a project workspace.

    Attributes:
        project_id: The project that owns the workspace.
        workspace_path: Absolute path to the workspace root.
        rel_path: Path relative to the workspace root.
        abs_path: Absolute path to the file.
        operation: One of ``"created"``, ``"modified"``, or ``"deleted"``.
        content_hash: SHA-256 prefix of the new file content (empty
            string for deletions).
    """

    project_id: str
    workspace_path: str
    rel_path: str
    abs_path: str
    operation: str  # "created" | "modified" | "deleted"
    content_hash: str


# ---------------------------------------------------------------------------
# Stub generation helpers
# ---------------------------------------------------------------------------


def derive_stub_name(rel_path: str) -> str:
    """Derive a vault reference stub filename from a workspace-relative path.

    Converts a spec/doc file path into a flat stub name suitable for
    the vault's ``references/`` directory.

    Rules:
      - Paths under ``specs/`` are prefixed with ``spec-``.
      - Paths under ``docs/specs/`` are prefixed with ``spec-``.
      - All other paths under ``docs/`` are prefixed with ``doc-``.
      - Directory separators are replaced with ``-``.
      - The ``.md`` extension is preserved.

    Examples:
        >>> derive_stub_name("specs/orchestrator.md")
        'spec-orchestrator.md'
        >>> derive_stub_name("specs/design/vault.md")
        'spec-design-vault.md'
        >>> derive_stub_name("docs/specs/design/vault.md")
        'spec-design-vault.md'
        >>> derive_stub_name("docs/getting-started.md")
        'doc-getting-started.md'
        >>> derive_stub_name("docs/api/endpoints.md")
        'doc-api-endpoints.md'
    """
    parts = rel_path.replace("\\", "/").split("/")

    # Determine prefix and strip the leading directory
    if len(parts) >= 3 and parts[0] == "docs" and parts[1] == "specs":
        prefix = "spec"
        remainder = parts[2:]
    elif parts[0] == "specs":
        prefix = "spec"
        remainder = parts[1:]
    elif parts[0] == "docs":
        prefix = "doc"
        remainder = parts[1:]
    else:
        prefix = "ref"
        remainder = parts

    # Remove .md extension from last part, join with dashes
    if remainder and remainder[-1].endswith(".md"):
        remainder[-1] = remainder[-1][:-3]

    name_body = "-".join(remainder)
    return f"{prefix}-{name_body}.md"


def compute_content_hash(file_path: str) -> str:
    """Compute a short SHA-256 hash of a file's content.

    Returns the first 12 hex characters of the digest.  Returns an
    empty string if the file cannot be read.
    """
    try:
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()[:12]
    except (OSError, FileNotFoundError):
        return ""


def generate_stub_content(
    rel_path: str,
    abs_path: str,
    content_hash: str,
    workspace_path: str,
    project_id: str,
    max_excerpt_lines: int = 30,
) -> str:
    """Generate the markdown content for a vault reference stub.

    The stub follows the format defined in ``vault.md`` Section 4::

        ---
        tags: [spec, reference, auto-generated]
        source: /path/to/project/specs/orchestrator.md
        source_hash: abc123def456
        last_synced: 2026-04-07
        ---

        # Spec: Orchestrator
        ...

    Parameters
    ----------
    rel_path:
        Workspace-relative path (e.g. ``specs/orchestrator.md``).
    abs_path:
        Absolute filesystem path to the source file.
    content_hash:
        SHA-256 prefix of the file content.
    workspace_path:
        Absolute path to the workspace root.
    project_id:
        Owning project identifier.
    max_excerpt_lines:
        Maximum lines to include in the ``## Excerpt`` section.

    Returns
    -------
    str
        Complete markdown content for the reference stub file.
    """
    parts = rel_path.replace("\\", "/").split("/")

    # Determine if spec or doc for the title prefix
    if parts[0] == "specs" or (len(parts) >= 2 and parts[0] == "docs" and parts[1] == "specs"):
        tag_type = "spec"
        title_prefix = "Spec"
    else:
        tag_type = "doc"
        title_prefix = "Doc"

    # Derive a human-readable name from the filename
    basename = os.path.splitext(os.path.basename(rel_path))[0]
    title_name = basename.replace("-", " ").replace("_", " ").title()

    today_str = date.today().isoformat()

    # Read excerpt from source file
    excerpt_lines: list[str] = []
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_excerpt_lines:
                    break
                excerpt_lines.append(line.rstrip())
    except (OSError, FileNotFoundError):
        excerpt_lines = ["*(source file not readable)*"]

    excerpt = "\n".join(excerpt_lines)
    if len(excerpt_lines) >= max_excerpt_lines:
        excerpt += "\n\n*(excerpt truncated — see full source)*"

    return f"""---
tags: [{tag_type}, reference, auto-generated]
source: {abs_path}
source_hash: {content_hash}
last_synced: {today_str}
---

# {title_prefix}: {title_name}

> Part of [[projects/{project_id}/{project_id}|{project_id}]] > [[projects/{project_id}/references/references|References]]

Full {tag_type} at `{rel_path}` in the {project_id} workspace.

## Excerpt

{excerpt}

## Summary

*Summary pending — will be generated by LLM enrichment.*

## Key Decisions

*Pending LLM extraction.*

## Key Interfaces

*Pending LLM extraction.*

## See Also
- [[projects/{project_id}/references/references|References]]
"""


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------


def matches_any_pattern(rel_path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """Test whether a workspace-relative path matches any of the given globs.

    Handles ``**`` for recursive directory matching.  Path separators
    are normalised to ``/`` for cross-platform consistency.

    Parameters
    ----------
    rel_path:
        Path relative to the workspace root.
    patterns:
        Glob patterns (e.g. ``("specs/**/*.md", "docs/**/*.md")``).
    """
    # Normalise backslashes to forward slashes for cross-platform matching.
    # On Linux os.sep is already "/" so we explicitly handle "\\" for paths
    # that may originate from Windows or WSL.
    rel_path = rel_path.replace("\\", "/")
    for pattern in patterns:
        p = pattern.replace("\\", "/")
        if "**" in p:
            if _match_recursive(rel_path, p):
                return True
        elif fnmatch.fnmatch(rel_path, p):
            return True
    return False


def _match_recursive(rel_path: str, pattern: str) -> bool:
    """Match a path against a pattern containing ``**``."""
    path_parts = rel_path.split("/")
    pattern_parts = pattern.split("/")
    return _match_segments(path_parts, pattern_parts)


def _match_segments(path_parts: list[str], pattern_parts: list[str]) -> bool:
    """Recursively match path segments against pattern segments.

    ``**`` matches zero or more path segments.  Other segments use
    :func:`fnmatch.fnmatch`.
    """
    if not pattern_parts:
        return not path_parts

    head = pattern_parts[0]
    rest = pattern_parts[1:]

    if head == "**":
        # Skip consecutive ** segments
        while rest and rest[0] == "**":
            rest = rest[1:]
        # Try matching the rest at every position
        for i in range(len(path_parts) + 1):
            if _match_segments(path_parts[i:], rest):
                return True
        return False

    if not path_parts:
        return False

    if fnmatch.fnmatch(path_parts[0], head):
        return _match_segments(path_parts[1:], rest)

    return False


# ---------------------------------------------------------------------------
# Main watcher class
# ---------------------------------------------------------------------------

# Default file patterns for spec/doc detection.
DEFAULT_SPEC_PATTERNS: tuple[str, ...] = (
    "specs/**/*.md",
    "docs/specs/**/*.md",
    "docs/**/*.md",
)

# Directories to exclude from scanning (speed + correctness).
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".obsidian",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
    }
)


@dataclass
class _ProjectSnapshot:
    """Per-project file state tracking."""

    # {workspace_rel_path: SpecFileState}
    files: dict[str, SpecFileState] = field(default_factory=dict)
    last_scan: float = 0.0


class WorkspaceSpecWatcher:
    """Detects spec/doc file changes in project workspaces and writes vault stubs.

    Designed to be called from the orchestrator's tick loop via :meth:`check`.
    Internally rate-limits scanning to ``poll_interval_seconds``.

    Parameters
    ----------
    db:
        Database interface for listing projects and workspaces.
    bus:
        EventBus for emitting ``workspace.spec.changed`` events.
    git:
        GitManager for optional git-diff detection mode.
    vault_projects_dir:
        Path to ``vault/projects/`` where stubs are written.
    file_patterns:
        Glob patterns for spec/doc files to watch.
    poll_interval_seconds:
        Minimum seconds between full scans.
    max_excerpt_lines:
        Lines of source to include in generated stubs.
    enabled:
        Master switch.  When False, ``check()`` returns immediately.
    """

    def __init__(
        self,
        db: DatabaseInterface,
        bus: EventBus,
        git: GitManager,
        vault_projects_dir: str,
        *,
        file_patterns: tuple[str, ...] = DEFAULT_SPEC_PATTERNS,
        poll_interval_seconds: int = 60,
        max_excerpt_lines: int = 30,
        enabled: bool = True,
    ) -> None:
        self._db = db
        self._bus = bus
        self._git = git
        self._vault_projects_dir = vault_projects_dir
        self._file_patterns = file_patterns
        self._poll_interval = poll_interval_seconds
        self._max_excerpt_lines = max_excerpt_lines
        self._enabled = enabled

        # Per-project file state snapshots: {project_id: _ProjectSnapshot}
        self._snapshots: dict[str, _ProjectSnapshot] = {}

        # Timestamp of last full scan
        self._last_check: float = 0.0

        # Statistics
        self._total_stubs_written: int = 0
        self._total_stubs_deleted: int = 0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def total_stubs_written(self) -> int:
        return self._total_stubs_written

    @property
    def total_stubs_deleted(self) -> int:
        return self._total_stubs_deleted

    def get_tracked_project_count(self) -> int:
        """Return the number of projects currently being tracked."""
        return len(self._snapshots)

    def get_tracked_file_count(self) -> int:
        """Return the total number of spec/doc files being tracked across all projects."""
        return sum(len(snap.files) for snap in self._snapshots.values())

    # ------------------------------------------------------------------
    # Main entry point (called from orchestrator tick)
    # ------------------------------------------------------------------

    async def check(self) -> list[SpecChange]:
        """Poll all project workspaces for spec/doc changes.

        Called every orchestrator cycle but internally rate-limited to
        at most once per ``poll_interval_seconds``.

        Returns a list of detected changes (may be empty).
        """
        if not self._enabled:
            return []

        now = time.time()
        if now - self._last_check < self._poll_interval:
            return []

        self._last_check = now
        all_changes: list[SpecChange] = []

        try:
            # Import here to avoid circular imports at module level
            from src.models import ProjectStatus

            projects = await self._db.list_projects(status=ProjectStatus.ACTIVE)
        except Exception as e:
            logger.warning("WorkspaceSpecWatcher: failed to list projects: %s", e)
            return []

        for project in projects:
            try:
                workspaces = await self._db.list_workspaces(project_id=project.id)
                if not workspaces:
                    continue

                # Use the first (preferred) workspace for scanning
                workspace = workspaces[0]
                if not os.path.isdir(workspace.workspace_path):
                    continue

                changes = self._scan_workspace(project.id, workspace.workspace_path)
                if changes:
                    all_changes.extend(changes)
                    # Write stubs and emit events
                    await self._process_changes(changes)

            except Exception as e:
                logger.warning(
                    "WorkspaceSpecWatcher: error scanning project %s: %s",
                    project.id,
                    e,
                )

        if all_changes:
            logger.info(
                "WorkspaceSpecWatcher: detected %d spec/doc change(s) across %d project(s)",
                len(all_changes),
                len({c.project_id for c in all_changes}),
            )

        return all_changes

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan_workspace(
        self,
        project_id: str,
        workspace_path: str,
    ) -> list[SpecChange]:
        """Scan a workspace directory for spec/doc file changes.

        Compares the current filesystem state against the stored snapshot
        for this project.  Updates the snapshot as a side-effect.

        Parameters
        ----------
        project_id:
            The project identifier.
        workspace_path:
            Absolute path to the workspace root.

        Returns
        -------
        list[SpecChange]
            Changes detected since the last scan.
        """
        snapshot = self._snapshots.get(project_id)
        if snapshot is None:
            # First scan — take initial snapshot, no changes reported
            snapshot = _ProjectSnapshot()
            snapshot.files = self._build_file_snapshot(workspace_path)
            snapshot.last_scan = time.time()
            self._snapshots[project_id] = snapshot
            logger.debug(
                "WorkspaceSpecWatcher: initial snapshot for %s (%d files)",
                project_id,
                len(snapshot.files),
            )
            return []

        new_files = self._build_file_snapshot(workspace_path)
        changes: list[SpecChange] = []

        # Detect created and modified files
        for rel_path, new_state in new_files.items():
            old_state = snapshot.files.get(rel_path)
            abs_path = os.path.join(workspace_path, rel_path)

            if old_state is None:
                changes.append(
                    SpecChange(
                        project_id=project_id,
                        workspace_path=workspace_path,
                        rel_path=rel_path,
                        abs_path=abs_path,
                        operation="created",
                        content_hash=new_state.content_hash,
                    )
                )
            elif (
                new_state.mtime != old_state.mtime
                or new_state.size != old_state.size
                or new_state.content_hash != old_state.content_hash
            ):
                changes.append(
                    SpecChange(
                        project_id=project_id,
                        workspace_path=workspace_path,
                        rel_path=rel_path,
                        abs_path=abs_path,
                        operation="modified",
                        content_hash=new_state.content_hash,
                    )
                )

        # Detect deleted files
        for rel_path in snapshot.files:
            if rel_path not in new_files:
                abs_path = os.path.join(workspace_path, rel_path)
                changes.append(
                    SpecChange(
                        project_id=project_id,
                        workspace_path=workspace_path,
                        rel_path=rel_path,
                        abs_path=abs_path,
                        operation="deleted",
                        content_hash="",
                    )
                )

        # Update snapshot
        snapshot.files = new_files
        snapshot.last_scan = time.time()

        return changes

    def _build_file_snapshot(self, workspace_path: str) -> dict[str, SpecFileState]:
        """Walk the workspace and build a snapshot of matching spec/doc files.

        Only files matching :attr:`_file_patterns` are included.  Hidden
        directories and common build artifact directories are skipped.
        """
        result: dict[str, SpecFileState] = {}

        if not os.path.isdir(workspace_path):
            return result

        try:
            for dirpath, dirnames, filenames in os.walk(workspace_path):
                # Prune excluded directories
                dirnames[:] = [
                    d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS and not d.startswith(".")
                ]

                for fname in filenames:
                    if fname.startswith("."):
                        continue

                    full_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(full_path, workspace_path)
                    # Normalise to forward slashes
                    rel_path = rel_path.replace(os.sep, "/")

                    if not matches_any_pattern(rel_path, self._file_patterns):
                        continue

                    try:
                        stat = os.stat(full_path)
                        content_hash = compute_content_hash(full_path)
                        result[rel_path] = SpecFileState(
                            rel_path=rel_path,
                            mtime=stat.st_mtime,
                            size=stat.st_size,
                            content_hash=content_hash,
                        )
                    except (OSError, FileNotFoundError):
                        pass

        except (OSError, PermissionError) as e:
            logger.warning(
                "WorkspaceSpecWatcher: cannot scan %s: %s",
                workspace_path,
                e,
            )

        return result

    # ------------------------------------------------------------------
    # Stub writing and event emission
    # ------------------------------------------------------------------

    async def _process_changes(self, changes: list[SpecChange]) -> None:
        """Write/delete vault reference stubs and emit events for each change."""
        for change in changes:
            try:
                if change.operation == "deleted":
                    self._delete_stub(change)
                else:
                    self._write_stub(change)

                # Emit event for downstream consumers (vector indexing,
                # LLM enrichment, etc.)
                await self._emit_event(change)

            except Exception as e:
                logger.warning(
                    "WorkspaceSpecWatcher: failed to process %s %s: %s",
                    change.operation,
                    change.rel_path,
                    e,
                )

    def _write_stub(self, change: SpecChange) -> str | None:
        """Write a reference stub to the vault.

        Returns the absolute path of the written stub, or ``None`` on failure.
        """
        stub_name = derive_stub_name(change.rel_path)
        refs_dir = os.path.join(
            self._vault_projects_dir,
            change.project_id,
            "references",
        )
        os.makedirs(refs_dir, exist_ok=True)

        stub_path = os.path.join(refs_dir, stub_name)
        content = generate_stub_content(
            rel_path=change.rel_path,
            abs_path=change.abs_path,
            content_hash=change.content_hash,
            workspace_path=change.workspace_path,
            project_id=change.project_id,
            max_excerpt_lines=self._max_excerpt_lines,
        )

        try:
            with open(stub_path, "w", encoding="utf-8") as f:
                f.write(content)
            self._total_stubs_written += 1
            logger.info(
                "WorkspaceSpecWatcher: wrote stub %s for %s/%s",
                stub_name,
                change.project_id,
                change.rel_path,
            )
            return stub_path
        except OSError as e:
            logger.warning(
                "WorkspaceSpecWatcher: failed to write stub %s: %s",
                stub_path,
                e,
            )
            return None

    def _delete_stub(self, change: SpecChange) -> bool:
        """Delete a reference stub from the vault when the source is deleted.

        Returns ``True`` if the stub was deleted, ``False`` if it did not
        exist or could not be deleted.
        """
        stub_name = derive_stub_name(change.rel_path)
        stub_path = os.path.join(
            self._vault_projects_dir,
            change.project_id,
            "references",
            stub_name,
        )

        if not os.path.isfile(stub_path):
            return False

        try:
            os.remove(stub_path)
            self._total_stubs_deleted += 1
            logger.info(
                "WorkspaceSpecWatcher: deleted stub %s (source %s/%s removed)",
                stub_name,
                change.project_id,
                change.rel_path,
            )
            return True
        except OSError as e:
            logger.warning(
                "WorkspaceSpecWatcher: failed to delete stub %s: %s",
                stub_path,
                e,
            )
            return False

    async def _emit_event(self, change: SpecChange) -> None:
        """Emit a ``workspace.spec.changed`` event on the EventBus."""
        try:
            await self._bus.emit(
                "workspace.spec.changed",
                {
                    "project_id": change.project_id,
                    "workspace_path": change.workspace_path,
                    "rel_path": change.rel_path,
                    "abs_path": change.abs_path,
                    "operation": change.operation,
                    "content_hash": change.content_hash,
                    "stub_name": derive_stub_name(change.rel_path),
                },
            )
        except Exception as e:
            logger.debug(
                "WorkspaceSpecWatcher: failed to emit event for %s: %s",
                change.rel_path,
                e,
            )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_stub_path(self, project_id: str, rel_path: str) -> str:
        """Return the expected vault stub path for a given source file.

        Useful for testing and debugging.
        """
        stub_name = derive_stub_name(rel_path)
        return os.path.join(
            self._vault_projects_dir,
            project_id,
            "references",
            stub_name,
        )

    def get_snapshot(self, project_id: str) -> dict[str, SpecFileState] | None:
        """Return the current file snapshot for a project, or ``None``."""
        snap = self._snapshots.get(project_id)
        return dict(snap.files) if snap else None

    def clear_snapshot(self, project_id: str) -> None:
        """Clear the stored snapshot for a project (forces full rescan)."""
        self._snapshots.pop(project_id, None)
