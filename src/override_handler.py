"""Vault watcher handler for override ``.md`` files — re-index dispatch.

Registers a glob pattern with the :class:`~src.vault_watcher.VaultWatcher` to
detect changes to per-project agent-type override files.  When an override file
is created, modified, or deleted, the handler derives the ``project_id`` and
``agent_type`` from the file path and indexes the content into the project's
Milvus collection.

Override files are indexed into the **project collection** (``aq_project_{id}``)
and tagged with ``#override`` and the agent type.  Since the project collection
has the highest scope weight (1.0), override content naturally ranks highest in
multi-scope search results.

Pattern registered (relative to vault root)::

    projects/*/overrides/*.md       — per-project agent-type overrides

Path structure:  ``projects/{project_id}/overrides/{agent_type}.md``

The ``project_id`` is extracted from the second path segment (``projects/<here>/...``),
and the ``agent_type`` is extracted from the filename stem (e.g. ``coding.md`` →
``coding``).

See ``docs/specs/design/memory-scoping.md`` Section 5 for the override model
specification.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

# Glob pattern for override markdown files (relative to vault root).
OVERRIDE_PATTERN: str = "projects/*/overrides/*.md"

# Tag used to identify override chunks in the project collection.
OVERRIDE_TAG: str = "override"

# Weight boost for override chunks — ensures they rank above normal project
# memories within the same collection.  The ``weighted_score`` is computed as
# ``score * scope_weight * entry_weight``, so a 1.5x boost means a moderately
# relevant override still outranks a highly relevant normal project memory.
OVERRIDE_ENTRY_WEIGHT: float = 1.5


@dataclass(frozen=True)
class OverrideChangeInfo:
    """Parsed change event for an override file.

    Attributes:
        file_path: Absolute filesystem path to the override file.
        change_type: One of ``"created"``, ``"modified"``, or ``"deleted"``.
        project_id: Project identifier extracted from the path
            (e.g. ``"mech-fighters"``).
        agent_type: Agent type extracted from the filename stem
            (e.g. ``"coding"`` from ``coding.md``).
    """

    file_path: str
    change_type: str
    project_id: str
    agent_type: str


def derive_override_info(rel_path: str) -> tuple[str, str]:
    """Derive project_id and agent_type from a vault-relative override path.

    Parameters
    ----------
    rel_path:
        Path relative to the vault root, e.g.
        ``projects/mech-fighters/overrides/coding.md``.

    Returns
    -------
    tuple[str, str]
        A ``(project_id, agent_type)`` pair.

    Raises
    ------
    ValueError
        If the path does not conform to the expected
        ``projects/{project_id}/overrides/{agent_type}.md`` structure.

    Examples
    --------
    >>> derive_override_info("projects/mech-fighters/overrides/coding.md")
    ('mech-fighters', 'coding')
    >>> derive_override_info("projects/my-app/overrides/review-specialist.md")
    ('my-app', 'review-specialist')
    """
    # Normalise separators
    parts = rel_path.replace("\\", "/").split("/")

    if (
        len(parts) >= 4
        and parts[0] == "projects"
        and parts[2] == "overrides"
        and parts[3].endswith(".md")
    ):
        project_id = parts[1]
        agent_type = os.path.splitext(parts[3])[0]  # strip .md extension
        return project_id, agent_type

    raise ValueError(
        f"Path does not match expected override structure "
        f"'projects/{{project_id}}/overrides/{{agent_type}}.md': {rel_path}"
    )


class OverrideIndexer:
    """Index override ``.md`` files into the project Milvus collection.

    Provides methods to index (upsert) and delete override file chunks
    in the project-scoped collection.  Tagged with ``#override`` and the
    agent type for filterability.

    Parameters
    ----------
    router:
        The :class:`~memsearch.CollectionRouter` managing scope-aware
        Milvus collections.
    embedder:
        The :class:`~memsearch.embeddings.EmbeddingProvider` for
        generating embedding vectors.
    max_chunk_size:
        Maximum character count per chunk (passed to ``chunk_markdown``).
    overlap_lines:
        Number of overlap lines between chunks.
    """

    def __init__(
        self,
        router: Any,
        embedder: Any,
        *,
        max_chunk_size: int = 1500,
        overlap_lines: int = 2,
    ) -> None:
        self._router = router
        self._embedder = embedder
        self._max_chunk_size = max_chunk_size
        self._overlap_lines = overlap_lines

    async def index_override(
        self,
        project_id: str,
        agent_type: str,
        file_path: str,
    ) -> int:
        """Index an override file into the project collection.

        Reads the file, chunks it, embeds, and upserts into the project's
        Milvus collection.  Stale chunks from a previous version of the
        same file are cleaned up automatically.

        Parameters
        ----------
        project_id:
            The project identifier (e.g. ``"mech-fighters"``).
        agent_type:
            The agent type from the filename (e.g. ``"coding"``).
        file_path:
            Absolute path to the override ``.md`` file.

        Returns
        -------
        int
            Number of chunks indexed (0 if file is empty or unreadable).
        """
        try:
            from memsearch.chunker import (
                chunk_markdown,
                clean_content_for_embedding,
                compute_chunk_id,
            )
            from memsearch.scoping import MemoryScope
        except ImportError:
            logger.warning("memsearch not available — cannot index override file")
            return 0

        # Read file content
        try:
            content = Path(file_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Cannot read override file %s: %s", file_path, exc)
            return 0

        if not content.strip():
            logger.debug("Override file %s is empty — skipping", file_path)
            return 0

        # Chunk the markdown
        source = str(Path(file_path).resolve())
        chunks = chunk_markdown(
            content,
            source=source,
            max_chunk_size=self._max_chunk_size,
            overlap_lines=self._overlap_lines,
        )

        if not chunks:
            return 0

        # Get or create the project store
        store = self._router.get_store(
            MemoryScope.PROJECT,
            project_id,
            description=f"project/{project_id}",
        )

        model = self._embedder.model_name

        # Compute new chunk IDs
        new_ids = {
            compute_chunk_id(c.source, c.start_line, c.end_line, c.content_hash, model)
            for c in chunks
        }

        # Remove stale chunks from the same source file
        old_ids = store.hashes_by_source(source)
        stale = old_ids - new_ids
        if stale:
            store.delete_by_hashes(list(stale))

        # Only embed chunks that don't already exist
        chunks_to_embed = [
            c
            for c in chunks
            if compute_chunk_id(c.source, c.start_line, c.end_line, c.content_hash, model)
            not in old_ids
        ]

        if not chunks_to_embed:
            return 0

        # Embed and upsert
        contents = [clean_content_for_embedding(c.content) for c in chunks_to_embed]
        embeddings = await self._embedder.embed(contents)

        tags = json.dumps([OVERRIDE_TAG, agent_type])
        now_ts = int(time.time())
        records: list[dict[str, Any]] = []
        for i, chunk in enumerate(chunks_to_embed):
            chunk_id = compute_chunk_id(
                chunk.source,
                chunk.start_line,
                chunk.end_line,
                chunk.content_hash,
                model,
            )
            records.append(
                {
                    "chunk_hash": chunk_id,
                    "entry_type": "document",
                    "embedding": embeddings[i],
                    "content": contents[i],
                    "original": chunk.content,
                    "source": chunk.source,
                    "heading": chunk.heading,
                    "heading_level": chunk.heading_level,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "tags": tags,
                    "updated_at": now_ts,
                }
            )

        n = store.upsert(records)
        logger.info(
            "Indexed %d override chunks for project=%s agent_type=%s from %s",
            n,
            project_id,
            agent_type,
            file_path,
        )
        return n

    async def delete_override(
        self,
        project_id: str,
        file_path: str,
    ) -> bool:
        """Remove all chunks for a deleted override file from the project collection.

        Parameters
        ----------
        project_id:
            The project identifier.
        file_path:
            Absolute path to the (now-deleted) override file.

        Returns
        -------
        bool
            ``True`` if chunks were found and deleted, ``False`` otherwise.
        """
        try:
            from memsearch.scoping import MemoryScope
        except ImportError:
            logger.warning("memsearch not available — cannot delete override chunks")
            return False

        source = str(Path(file_path).resolve())

        # Only delete if the collection already exists (don't create one just to delete)
        if not self._router.has_store(MemoryScope.PROJECT, project_id):
            logger.debug(
                "No project collection for %s — nothing to delete for override",
                project_id,
            )
            return False

        store = self._router.get_store(MemoryScope.PROJECT, project_id)
        existing = store.hashes_by_source(source)
        if not existing:
            return False

        store.delete_by_hashes(list(existing))
        logger.info(
            "Deleted %d override chunks for project=%s from %s",
            len(existing),
            project_id,
            file_path,
        )
        return True

    async def index_all_overrides(self, vault_root: str) -> int:
        """Scan and index all existing override files under the vault.

        Called during startup to ensure override files that were created
        while the daemon was stopped are indexed.

        Parameters
        ----------
        vault_root:
            Absolute path to the vault root directory
            (e.g. ``~/.agent-queue/vault``).

        Returns
        -------
        int
            Total number of chunks indexed across all override files.
        """
        import glob as _glob

        pattern = os.path.join(vault_root, "projects", "*", "overrides", "*.md")
        total = 0
        for file_path in sorted(_glob.glob(pattern)):
            rel = os.path.relpath(file_path, vault_root).replace("\\", "/")
            try:
                project_id, agent_type = derive_override_info(rel)
            except ValueError:
                logger.warning("Unexpected override file path structure: %s", rel)
                continue
            n = await self.index_override(project_id, agent_type, file_path)
            total += n
        return total


# ---------------------------------------------------------------------------
# Module-level indexer reference for the watcher handler callback
# ---------------------------------------------------------------------------

# The watcher callback is a plain async function.  To give it access to the
# indexer we store a module-level reference that is set by ``set_indexer()``
# during orchestrator startup.
_indexer: OverrideIndexer | None = None


def set_indexer(indexer: OverrideIndexer | None) -> None:
    """Set the module-level :class:`OverrideIndexer` for the watcher callback.

    Called during orchestrator startup after the ``CollectionRouter`` and
    ``EmbeddingProvider`` are initialised.  Pass ``None`` to disable
    indexing (reverts to log-only stub behaviour).
    """
    global _indexer
    _indexer = indexer


def get_indexer() -> OverrideIndexer | None:
    """Return the current module-level :class:`OverrideIndexer`, or ``None``."""
    return _indexer


async def on_override_changed(changes: list[VaultChange]) -> None:
    """Handle changes to override ``.md`` files in the vault.

    When an :class:`OverrideIndexer` is available (set via
    :func:`set_indexer`), override files are indexed into the project
    collection on create/modify and removed on delete.

    Without an indexer (e.g. memsearch unavailable), falls back to
    logging changes at INFO level.

    Parameters
    ----------
    changes:
        List of :class:`~src.vault_watcher.VaultChange` objects for files
        matching the override pattern.
    """
    for change in changes:
        try:
            project_id, agent_type = derive_override_info(change.rel_path)
        except ValueError:
            logger.warning(
                "override.md change with unexpected path structure: %s",
                change.rel_path,
            )
            continue

        info = OverrideChangeInfo(
            file_path=change.path,
            change_type=change.operation,
            project_id=project_id,
            agent_type=agent_type,
        )

        if _indexer is not None:
            try:
                if info.change_type == "deleted":
                    await _indexer.delete_override(info.project_id, info.file_path)
                    logger.info(
                        "override.md deleted — removed index for project=%s agent_type=%s: %s",
                        info.project_id,
                        info.agent_type,
                        info.file_path,
                    )
                else:
                    n = await _indexer.index_override(
                        info.project_id, info.agent_type, info.file_path
                    )
                    logger.info(
                        "override.md %s — indexed %d chunks for project=%s agent_type=%s: %s",
                        info.change_type,
                        n,
                        info.project_id,
                        info.agent_type,
                        info.file_path,
                    )
            except Exception:
                logger.exception(
                    "Failed to index override for project=%s agent_type=%s: %s",
                    info.project_id,
                    info.agent_type,
                    info.file_path,
                )
        else:
            logger.info(
                "override.md %s for project=%s agent_type=%s: %s (no indexer configured)",
                info.change_type,
                info.project_id,
                info.agent_type,
                info.file_path,
            )


def register_override_handlers(watcher: VaultWatcher) -> str:
    """Register the override-file watcher handler on the given *watcher*.

    Registers a single handler for the :data:`OVERRIDE_PATTERN`.

    Parameters
    ----------
    watcher:
        The :class:`~src.vault_watcher.VaultWatcher` instance to register
        the handler on (typically ``orchestrator.vault_watcher``).

    Returns
    -------
    str
        The handler ID assigned by the watcher.
    """
    hid = watcher.register_handler(
        OVERRIDE_PATTERN,
        on_override_changed,
        handler_id=f"override:{OVERRIDE_PATTERN}",
    )
    logger.debug("Registered override handler for pattern %r (id=%s)", OVERRIDE_PATTERN, hid)

    logger.info(
        "Registered override.md watcher handler for re-index dispatch",
    )
    return hid
