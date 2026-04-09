"""Semantic memory manager for agent-queue using memsearch.

Provides per-project memory indexing and retrieval. Each project gets its own
Milvus collection and indexes markdown files from the workspace's memory/ and
notes/ directories.

Features (phases from the memory improvement plan):
- **Project Profile** — A living ``profile.md`` per project that captures
  synthesized knowledge (architecture, conventions, decisions, patterns).
- **Post-Task Revision** — After each completed task, an LLM call revises
  the project profile based on what the task learned.
- **Notes Integration** — Tasks can auto-generate categorized notes, and
  notes feed back into profile revision.
- **Memory Compaction** — Age-based lifecycle management that groups
  task memories into recent/medium/old tiers, LLM-summarizes medium-age
  memories into weekly digests, and removes old individual files.
- **Enhanced Context Delivery** — Tiered, prioritized context injection:
  profile > notes > recent tasks > semantic search results.

Optional dependency — when memsearch is not installed or memory is not
configured, all operations are no-ops.  All memsearch calls are wrapped in
try/except for resilience: a memory subsystem failure never blocks task
execution.

See ``specs/memory.md`` for the full memory system specification.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import time
from typing import Any

from src.config import MemoryConfig
from src.models import MemoryContext, ProjectFactsheet

logger = logging.getLogger(__name__)

try:
    from memsearch import (
        MemSearch,
        CollectionRouter,
        MemoryScope,
        collection_name as memsearch_collection_name,
        merge_and_rank,
    )
    from memsearch.embeddings import EmbeddingProvider, get_provider as get_embedding_provider

    MEMSEARCH_AVAILABLE = True
except ImportError:
    MemSearch = None  # type: ignore[assignment,misc]
    CollectionRouter = None  # type: ignore[assignment,misc]
    MemoryScope = None  # type: ignore[assignment,misc]
    memsearch_collection_name = None  # type: ignore[assignment]
    merge_and_rank = None  # type: ignore[assignment]
    EmbeddingProvider = None  # type: ignore[assignment,misc]
    get_embedding_provider = None  # type: ignore[assignment]
    MEMSEARCH_AVAILABLE = False


class MemoryManager:
    """Manages per-project MemSearch instances and memory operations.

    Each project gets its own Milvus collection (``aq_project_{id}``)
    so memories are fully isolated between projects. Instances are created
    lazily on first access.

    Memory files are stored centrally under ``{data_dir}/memory/{project_id}/``
    to keep all persistent data under ``~/.agent-queue``.

    When memsearch is not installed or the config has ``enabled=False``,
    every public method degrades gracefully (returns empty lists, None, etc.)
    without raising exceptions.
    """

    def __init__(self, config: MemoryConfig, storage_root: str = "") -> None:
        self.config = config
        self._storage_root = os.path.expanduser(storage_root or "~/.agent-queue")
        self._instances: dict[str, Any] = {}  # project_id -> MemSearch
        self._watchers: dict[str, Any] = {}
        self._last_compact: dict[str, float] = {}  # project_id -> timestamp
        self._doc_file_mtimes: dict[str, float] = {}  # "workspace:relpath" -> mtime
        # Multi-scope search (spec §6): shared router and embedder, lazily initialised.
        self._router: Any | None = None  # CollectionRouter
        self._embedder: Any | None = None  # EmbeddingProvider
        # Override indexer (spec §5): indexes override .md files into project collections.
        self._override_indexer: Any | None = None  # OverrideIndexer

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collection_name(self, project_id: str) -> str:
        """Deterministic, Milvus-safe collection name per project.

        Uses the canonical ``aq_project_{id}`` format from memsearch's
        scoping module.  Falls back to the same format using manual
        sanitization when memsearch is not installed.
        """
        if MEMSEARCH_AVAILABLE and memsearch_collection_name is not None:
            return memsearch_collection_name(MemoryScope.PROJECT, project_id)
        # Fallback when memsearch is unavailable — mirror the canonical format
        safe_id = project_id.replace("-", "_").replace(" ", "_").lower()
        return f"aq_project_{safe_id}"

    def _project_memory_dir(self, project_id: str) -> str:
        """Central memory storage directory for a project.

        Returns ``{data_dir}/memory/{project_id}/``.
        """
        return os.path.join(self._storage_root, "memory", project_id)

    def _tasks_dir(self, project_id: str) -> str:
        """Task record storage directory for a project.

        Returns ``{data_dir}/tasks/{project_id}/``.  Task records live
        outside the memory/vault tree per vault spec §6.
        """
        return os.path.join(self._storage_root, "tasks", project_id)

    def _reindex_marker_path(self, project_id: str) -> str:
        """Path to the post-migration reindex marker for a project.

        When ``_migrate_legacy_tasks()`` moves files out of the memory tree,
        it writes this marker so that the next ``get_instance()`` call triggers
        an ``index()`` to purge stale vector-store entries.  The marker is
        removed after reindex completes (or fails — it's best-effort).

        Non-``.md`` extension ensures MemSearch won't try to index it.
        """
        return os.path.join(self._project_memory_dir(project_id), ".needs_reindex")

    def _migrate_legacy_tasks(self, project_id: str) -> bool:
        """Move task records from legacy ``memory/*/tasks/`` to ``tasks/*/``.

        The old layout stored task markdown files inside the memory tree at
        ``{data_dir}/memory/{project_id}/tasks/``.  Because MemSearch indexes
        the entire ``memory/{project_id}/`` directory recursively, those task
        files pollute semantic search results.

        This method moves any remaining files to the new canonical location
        (``{data_dir}/tasks/{project_id}/``) and removes the legacy directory
        so future MemSearch scans no longer encounter them.

        When files are actually migrated, a ``.needs_reindex`` marker is
        written so that ``get_instance()`` triggers a vector-store cleanup
        on the next startup.

        Returns ``True`` when files were migrated (and a reindex marker was
        written), ``False`` otherwise.

        Safe to call repeatedly — it's a no-op once the legacy directory is
        gone.  Per vault spec §6 (Migration Path, Phase 1).
        """
        legacy_dir = os.path.join(self._project_memory_dir(project_id), "tasks")
        if not os.path.isdir(legacy_dir):
            return False

        new_dir = self._tasks_dir(project_id)
        os.makedirs(new_dir, exist_ok=True)

        migrated = 0
        for fname in os.listdir(legacy_dir):
            src = os.path.join(legacy_dir, fname)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(new_dir, fname)
            if not os.path.exists(dst):
                shutil.move(src, dst)
                migrated += 1
            else:
                # New location already has this file — remove the stale copy
                os.remove(src)

        # Remove the now-empty legacy directory (and any leftover subdirs)
        shutil.rmtree(legacy_dir, ignore_errors=True)

        if migrated:
            logger.info(
                "Migrated %d task record(s) from legacy memory/tasks/ to tasks/ for project %s",
                migrated,
                project_id,
            )
            # Write marker so get_instance() triggers a reindex to purge
            # stale vector-store entries for the moved task files.
            try:
                marker = self._reindex_marker_path(project_id)
                with open(marker, "w") as f:
                    f.write("reindex-after-task-migration\n")
            except OSError:
                logger.debug("Could not write reindex marker for project %s", project_id)

        return migrated > 0

    def _profile_path(self, project_id: str) -> str:
        """Path to the project profile file.

        Returns ``{data_dir}/memory/{project_id}/profile.md``.
        """
        return os.path.join(self._project_memory_dir(project_id), "profile.md")

    def _notes_dir(self, project_id: str) -> str:
        """Path to the project notes directory.

        Notes live in the vault at ``vault/projects/{project_id}/notes/``.
        """
        return os.path.join(self._storage_root, "vault", "projects", project_id, "notes")

    def _factsheet_path(self, project_id: str) -> str:
        """Path to the project factsheet file.

        Returns ``{data_dir}/memory/{project_id}/factsheet.md``.
        """
        return os.path.join(self._project_memory_dir(project_id), "factsheet.md")

    def _knowledge_dir(self, project_id: str) -> str:
        """Path to the project knowledge base directory.

        Returns ``{data_dir}/memory/{project_id}/knowledge/``.
        """
        return os.path.join(self._project_memory_dir(project_id), "knowledge")

    def _memory_paths(self, project_id: str, workspace_path: str) -> list[str]:
        """Directories to index for a project.

        Always includes the central ``{data_dir}/memory/{project_id}/``
        directory (created if absent). Optionally includes the project's
        ``notes/`` directory when ``index_notes`` is enabled, and the
        workspace ``specs/`` and ``docs/`` directories when their respective
        config flags are enabled. This lets agents reference project
        specifications and documentation without duplicating files on disk —
        MemSearch indexes the originals in-place so updates are always current.
        """
        paths = [self._project_memory_dir(project_id)]
        if self.config.index_notes:
            notes_dir = self._notes_dir(project_id)
            if os.path.isdir(notes_dir):
                paths.append(notes_dir)
        # Include knowledge base directory for semantic search indexing
        if self.config.index_knowledge:
            knowledge_dir = self._knowledge_dir(project_id)
            if os.path.isdir(knowledge_dir):
                paths.append(knowledge_dir)
        # Include workspace specs/ and docs/ directories for zero-duplication
        # knowledge indexing — agents can answer questions about their own
        # architecture, configuration, and published documentation.
        if workspace_path:
            if self.config.index_specs:
                specs_dir = os.path.join(workspace_path, "specs")
                if os.path.isdir(specs_dir):
                    paths.append(specs_dir)
            if self.config.index_docs:
                docs_dir = os.path.join(workspace_path, "docs")
                if os.path.isdir(docs_dir):
                    paths.append(docs_dir)
        return paths

    async def _index_project_doc_files(self, instance: Any, workspace_path: str) -> None:
        """Index individual project documentation files (CLAUDE.md, README.md, etc.).

        These files live at the workspace root and aren't inside a directory
        that ``_memory_paths()`` covers, so they need explicit ``index_file()``
        calls.  We track modification times to avoid re-indexing unchanged files
        across multiple ``get_instance()`` calls within the same process.
        """
        if not self.config.index_project_docs or not workspace_path:
            return
        for rel_path in self.config.project_docs_files:
            full_path = os.path.join(workspace_path, rel_path)
            if not os.path.isfile(full_path):
                continue
            try:
                mtime = os.path.getmtime(full_path)
                key = f"{workspace_path}:{rel_path}"
                if self._doc_file_mtimes.get(key) == mtime:
                    continue  # unchanged since last index
                await instance.index_file(full_path)
                self._doc_file_mtimes[key] = mtime
            except Exception as e:
                logger.debug(f"Failed to index project doc {rel_path}: {e}")

    def _resolve_milvus_uri(self) -> str:
        """Expand ``~`` in Milvus Lite file paths and ensure parent dir exists."""
        uri = os.path.expanduser(self.config.milvus_uri)
        # For file-based Milvus Lite URIs, create the parent directory
        if not uri.startswith("http"):
            os.makedirs(os.path.dirname(uri), exist_ok=True)
        return uri

    # ------------------------------------------------------------------
    # Multi-scope search infrastructure (spec §6)
    # ------------------------------------------------------------------

    async def _get_embedder(self) -> Any | None:
        """Get or create the shared embedding provider.

        Returns ``None`` when memsearch is unavailable or the subsystem is
        disabled.  The embedder is created once and reused across all
        multi-scope searches.

        The provider constructor may make synchronous network calls (e.g.
        to discover the embedding dimension from Ollama), so we run it in
        a thread to avoid blocking the asyncio event loop.
        """
        if not MEMSEARCH_AVAILABLE or not self.config.enabled:
            return None
        if self._embedder is not None:
            return self._embedder
        try:
            self._embedder = await asyncio.to_thread(
                get_embedding_provider,
                self.config.embedding_provider,
                model=self.config.embedding_model or None,
                base_url=self.config.embedding_base_url or None,
                api_key=self.config.embedding_api_key or None,
            )
            logger.info(
                "Initialised shared embedding provider: %s (dim=%d)",
                self._embedder.model_name,
                self._embedder.dimension,
            )
            return self._embedder
        except Exception as e:
            logger.error("Failed to create embedding provider: %s", e)
            return None

    async def _get_router(self) -> Any | None:
        """Get or create the shared :class:`CollectionRouter`.

        The router manages lazy ``MilvusStore`` instances per scope
        (``aq_system``, ``aq_agenttype_{type}``, ``aq_project_{id}``)
        and provides parallel multi-scope search with weighted merging.

        Requires a working embedding provider to determine vector
        dimension.  Returns ``None`` when memsearch is unavailable or
        the embedding provider cannot be initialised.
        """
        if not MEMSEARCH_AVAILABLE or not self.config.enabled:
            return None
        if self._router is not None:
            return self._router
        embedder = await self._get_embedder()
        if embedder is None:
            return None
        try:
            uri = self._resolve_milvus_uri()
            self._router = CollectionRouter(
                milvus_uri=uri,
                token=self.config.milvus_token or None,
                dimension=embedder.dimension,
            )
            logger.info("Initialised CollectionRouter (uri=%s, dim=%d)", uri, embedder.dimension)
            return self._router
        except Exception as e:
            logger.error("Failed to create CollectionRouter: %s", e)
            return None

    # ------------------------------------------------------------------
    # Startup initialization
    # ------------------------------------------------------------------

    async def ensure_system_collection(self) -> bool:
        """Ensure the ``aq_system`` collection exists in Milvus.

        Called during orchestrator startup (roadmap 3.1.3) so the system-level
        memory collection is ready before any tasks run.  This makes the
        system scope available for writes and searches from the very first
        operation, rather than waiting for lazy creation on first access.

        The system collection stores cross-cutting knowledge that applies
        across all projects and agent types (e.g., global conventions,
        organizational patterns).

        Returns
        -------
        bool
            ``True`` if the collection was successfully ensured (created or
            already existed), ``False`` if memsearch is unavailable or
            initialization failed.
        """
        if not MEMSEARCH_AVAILABLE or not self.config.enabled:
            logger.debug("Memory disabled or memsearch unavailable — skipping system collection")
            return False

        router = await self._get_router()
        if router is None:
            logger.warning("Could not initialize CollectionRouter — system collection not created")
            return False

        try:
            await asyncio.to_thread(router.ensure_system_collection)
            logger.info("System-level memory collection (aq_system) ensured on startup")
            return True
        except Exception as e:
            logger.warning("Failed to ensure system collection: %s", e)
            return False

    async def ensure_orchestrator_collection(self) -> bool:
        """Ensure the ``aq_orchestrator`` collection exists in Milvus.

        Called during orchestrator startup (roadmap 3.1.4) so the
        orchestrator-level memory collection is ready before any tasks
        run.  The orchestrator collection stores operational knowledge —
        scheduling patterns, cross-project insights, and self-improvement
        notes.

        Returns
        -------
        bool
            ``True`` if the collection was successfully ensured (created or
            already existed), ``False`` if memsearch is unavailable or
            initialization failed.
        """
        if not MEMSEARCH_AVAILABLE or not self.config.enabled:
            logger.debug(
                "Memory disabled or memsearch unavailable — skipping orchestrator collection"
            )
            return False

        router = await self._get_router()
        if router is None:
            logger.warning(
                "Could not initialize CollectionRouter — orchestrator collection not created"
            )
            return False

        try:
            await asyncio.to_thread(router.ensure_orchestrator_collection)
            logger.info("Orchestrator-level memory collection (aq_orchestrator) ensured on startup")
            return True
        except Exception as e:
            logger.warning("Failed to ensure orchestrator collection: %s", e)
            return False

    async def ensure_agent_type_collection(self, agent_type: str) -> bool:
        """Ensure the ``aq_agenttype_{agent_type}`` collection exists in Milvus.

        Called when a new agent-type profile is created (roadmap 3.1.2) so
        the agent-type-scoped memory collection is ready for writes and
        searches from the agent's first task, rather than waiting for lazy
        creation on first access.

        Parameters
        ----------
        agent_type:
            The agent type identifier (e.g. ``"coding"``, ``"code-review"``).
            Will be sanitized for Milvus compatibility.

        Returns
        -------
        bool
            ``True`` if the collection was successfully ensured (created or
            already existed), ``False`` if memsearch is unavailable or
            initialization failed.
        """
        if not MEMSEARCH_AVAILABLE or not self.config.enabled:
            logger.debug(
                "Memory disabled or memsearch unavailable — skipping agent-type collection"
            )
            return False

        router = await self._get_router()
        if router is None:
            logger.warning(
                "Could not initialize CollectionRouter — agent-type collection not created"
            )
            return False

        try:
            await asyncio.to_thread(router.ensure_agent_type_collection, agent_type)
            logger.info(
                "Agent-type memory collection (aq_agenttype_%s) ensured on profile creation",
                agent_type,
            )
            return True
        except Exception as e:
            logger.warning("Failed to ensure agent-type collection for '%s': %s", agent_type, e)
            return False

    async def migrate_legacy_project_collections(self) -> list[tuple[str, str, int]]:
        """Migrate legacy ``aq_{id}_memory`` collections to ``aq_project_{id}``.

        Called during orchestrator startup (roadmap 3.1.5) to rename
        existing project collections from the old naming convention to the
        canonical ``aq_project_{id}`` format used by the scoping system.

        Since Milvus Lite does not support ``rename_collection``, this
        copies all data (including embeddings) to a new collection and
        drops the old one.  Safe to call repeatedly — only acts on
        collections matching the legacy pattern.

        Returns
        -------
        list[tuple[str, str, int]]
            List of ``(old_name, new_name, row_count)`` for each
            migrated collection.  Empty list if nothing to migrate or
            memsearch is unavailable.
        """
        if not MEMSEARCH_AVAILABLE or not self.config.enabled:
            logger.debug("Memory disabled or memsearch unavailable — skipping legacy migration")
            return []

        router = await self._get_router()
        if router is None:
            logger.warning("Could not initialize CollectionRouter — legacy migration skipped")
            return []

        try:
            migrated = await asyncio.to_thread(router.migrate_legacy_collections)
            if migrated:
                logger.info(
                    "Migrated %d legacy collection(s) to new naming convention: %s",
                    len(migrated),
                    ", ".join(f"{old}→{new} ({n} rows)" for old, new, n in migrated),
                )
            else:
                logger.debug("No legacy collections found to migrate")
            return migrated
        except Exception as e:
            logger.warning("Legacy collection migration failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # Override indexer (spec §5)
    # ------------------------------------------------------------------

    async def get_override_indexer(self) -> Any | None:
        """Get or create the :class:`~src.override_handler.OverrideIndexer`.

        Returns ``None`` when memsearch is unavailable, memory is disabled,
        or the router/embedder cannot be initialised.  The indexer is created
        once and reused for all override file operations.
        """
        if not MEMSEARCH_AVAILABLE or not self.config.enabled:
            return None
        if self._override_indexer is not None:
            return self._override_indexer

        router = await self._get_router()
        embedder = await self._get_embedder()
        if router is None or embedder is None:
            logger.debug("Cannot create OverrideIndexer — router or embedder unavailable")
            return None

        try:
            from src.override_handler import OverrideIndexer

            self._override_indexer = OverrideIndexer(
                router,
                embedder,
                max_chunk_size=self.config.max_chunk_size,
                overlap_lines=self.config.overlap_lines,
            )
            logger.info("Created OverrideIndexer for override file indexing")
            return self._override_indexer
        except Exception as e:
            logger.warning("Failed to create OverrideIndexer: %s", e)
            return None

    async def index_project_overrides(self, vault_root: str | None = None) -> int:
        """Index all existing override files in the vault.

        Scans ``vault/projects/*/overrides/*.md`` and indexes each file
        into the corresponding project collection.  Called during startup
        to catch any overrides created while the daemon was stopped.

        Parameters
        ----------
        vault_root:
            Absolute path to the vault root.  Defaults to
            ``{storage_root}/vault``.

        Returns
        -------
        int
            Total number of chunks indexed across all override files.
        """
        indexer = await self.get_override_indexer()
        if indexer is None:
            return 0

        root = vault_root or os.path.join(self._storage_root, "vault")
        if not os.path.isdir(root):
            return 0

        try:
            total = await indexer.index_all_overrides(root)
            if total > 0:
                logger.info("Indexed %d override chunks from vault at startup", total)
            return total
        except Exception as e:
            logger.warning("Failed to index override files at startup: %s", e)
            return 0

    async def setup_override_watcher(self) -> bool:
        """Wire the override indexer into the watcher handler callback.

        Sets the module-level indexer in :mod:`src.override_handler` so
        the vault watcher callback can index override files when they
        change.

        Returns
        -------
        bool
            ``True`` if the indexer was successfully configured, ``False``
            if memsearch is unavailable or initialization failed.
        """
        indexer = await self.get_override_indexer()
        if indexer is None:
            return False

        try:
            from src.override_handler import set_indexer

            set_indexer(indexer)
            logger.info("Override watcher handler connected to OverrideIndexer")
            return True
        except Exception as e:
            logger.warning("Failed to setup override watcher: %s", e)
            return False

    # ------------------------------------------------------------------
    # Instance management
    # ------------------------------------------------------------------

    async def get_instance(self, project_id: str, workspace_path: str) -> Any | None:
        """Get or create a MemSearch instance for a project.

        Returns ``None`` when memsearch is unavailable or the subsystem is
        disabled — callers should treat ``None`` as "memory not available".
        """
        if not MEMSEARCH_AVAILABLE or not self.config.enabled:
            return None

        if project_id in self._instances:
            return self._instances[project_id]

        try:
            # Ensure the task record directory exists for remember()
            os.makedirs(self._tasks_dir(project_id), exist_ok=True)

            # Migrate any leftover task files from the legacy memory/*/tasks/
            # directory to the new tasks/*/ location so MemSearch no longer
            # indexes them (vault spec §6 Phase 1).
            self._migrate_legacy_tasks(project_id)

            paths = self._memory_paths(project_id, workspace_path)

            # MemSearch.__init__ makes a synchronous HTTP call to the
            # embedding provider (e.g. Ollama embed) to discover the
            # vector dimension.  Run in a thread to avoid blocking the
            # asyncio event loop and starving the Discord gateway heartbeat.
            instance = await asyncio.to_thread(
                MemSearch,
                paths=paths,
                embedding_provider=self.config.embedding_provider,
                embedding_model=self.config.embedding_model or None,
                embedding_base_url=self.config.embedding_base_url or None,
                embedding_api_key=self.config.embedding_api_key or None,
                milvus_uri=self._resolve_milvus_uri(),
                milvus_token=self.config.milvus_token or None,
                collection=self._collection_name(project_id),
                max_chunk_size=self.config.max_chunk_size,
                overlap_lines=self.config.overlap_lines,
            )
            self._instances[project_id] = instance

            # Purge stale vector-store entries after task file migration.
            # _migrate_legacy_tasks() writes a .needs_reindex marker when
            # it moves files out of the memory tree; a non-force index()
            # scan detects those files as deleted and removes their chunks.
            # The marker survives daemon restarts so cleanup is guaranteed.
            marker = self._reindex_marker_path(project_id)
            if os.path.isfile(marker):
                try:
                    result = await instance.index()
                    count = result if isinstance(result, int) else 0
                    logger.info(
                        "Re-indexed project %s memory after task migration (%d chunks)",
                        project_id,
                        count,
                    )
                except Exception as e:
                    logger.warning(
                        "Post-migration reindex failed for project %s: %s",
                        project_id,
                        e,
                    )
                finally:
                    try:
                        os.remove(marker)
                    except OSError:
                        pass

            # Index individual root-level doc files (CLAUDE.md, README.md)
            await self._index_project_doc_files(instance, workspace_path)

            return instance
        except Exception as e:
            logger.error(f"Failed to create MemSearch instance for project {project_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Phase 1: Project Profile
    # ------------------------------------------------------------------

    async def get_profile(self, project_id: str) -> str | None:
        """Read and return the project profile content.

        Returns ``None`` if profiles are disabled or no profile exists yet.
        """
        if not self.config.profile_enabled:
            return None

        path = self._profile_path(project_id)
        if not os.path.isfile(path):
            return None

        try:
            with open(path) as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read profile for project {project_id}: {e}")
            return None

    async def update_profile(
        self, project_id: str, new_content: str, workspace_path: str = ""
    ) -> str | None:
        """Write updated profile content and re-index it.

        Truncates to ``profile_max_size`` if the content exceeds the limit.
        Returns the file path on success, ``None`` otherwise.
        """
        if not self.config.profile_enabled:
            return None

        # Enforce size limit
        max_size = self.config.profile_max_size
        if len(new_content) > max_size:
            # Truncate at last complete line within budget
            truncated = new_content[:max_size]
            last_newline = truncated.rfind("\n")
            if last_newline > 0:
                new_content = truncated[:last_newline] + "\n"
            else:
                new_content = truncated

        path = self._profile_path(project_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(new_content)
        except Exception as e:
            logger.warning(f"Failed to write profile for project {project_id}: {e}")
            return None

        # Re-index the profile file if we have an instance
        if workspace_path:
            instance = await self.get_instance(project_id, workspace_path)
            if instance:
                try:
                    await instance.index_file(path)
                except Exception as e:
                    logger.warning(f"Profile indexing failed for project {project_id}: {e}")

        return path

    # ------------------------------------------------------------------
    # Phase 1.5: Project Factsheet (Structured Metadata Layer)
    # ------------------------------------------------------------------

    def _factsheet_path(self, project_id: str) -> str:
        """Path to the project factsheet file.

        Returns ``{data_dir}/memory/{project_id}/factsheet.md``.
        """
        return os.path.join(self._project_memory_dir(project_id), "factsheet.md")

    def _parse_factsheet(self, raw: str) -> ProjectFactsheet:
        """Parse a factsheet file into a ``ProjectFactsheet`` dataclass.

        Splits on YAML frontmatter delimiters (``---``) and parses the
        YAML block.  Returns a ``ProjectFactsheet`` with empty defaults
        if parsing fails.
        """
        try:
            import yaml as _yaml
        except ImportError:
            # PyYAML not available — return raw content as body only
            logger.debug("PyYAML not installed; factsheet YAML parsing unavailable")
            return ProjectFactsheet(body_markdown=raw)

        # Split on frontmatter delimiters
        parts = raw.split("---", 2)
        if len(parts) < 3:
            # No valid frontmatter — treat entire content as markdown body
            return ProjectFactsheet(body_markdown=raw)

        yaml_text = parts[1]
        body = parts[2].strip()

        try:
            yaml_data = _yaml.safe_load(yaml_text)
            if not isinstance(yaml_data, dict):
                yaml_data = {}
        except Exception as e:
            logger.warning(f"Failed to parse factsheet YAML: {e}")
            yaml_data = {}

        return ProjectFactsheet(raw_yaml=yaml_data, body_markdown=body)

    def _serialize_factsheet(self, factsheet: ProjectFactsheet) -> str:
        """Serialize a ``ProjectFactsheet`` back to YAML-frontmatter + markdown."""
        try:
            import yaml as _yaml
        except ImportError:
            logger.debug("PyYAML not installed; cannot serialize factsheet")
            return factsheet.body_markdown

        yaml_text = _yaml.dump(
            factsheet.raw_yaml,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
        body = factsheet.body_markdown
        return f"---\n{yaml_text}---\n\n{body}\n"

    def parse_factsheet_yaml(self, content: str) -> dict:
        """Parse YAML frontmatter from a factsheet string. Returns empty dict on failure."""
        try:
            import yaml
        except ImportError:
            return {}

        if not content or not content.startswith("---"):
            return {}

        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}
        try:
            return yaml.safe_load(parts[1]) or {}
        except Exception:
            return {}

    async def read_factsheet(self, project_id: str) -> ProjectFactsheet | None:
        """Read and parse the project factsheet.

        Returns ``None`` if no factsheet exists yet.
        """
        path = self._factsheet_path(project_id)
        if not os.path.isfile(path):
            return None

        try:
            with open(path) as f:
                raw = f.read()
            return self._parse_factsheet(raw)
        except Exception as e:
            logger.warning(f"Failed to read factsheet for project {project_id}: {e}")
            return None

    async def read_factsheet_raw(self, project_id: str) -> str | None:
        """Read the raw factsheet content as a string.

        Returns ``None`` if no factsheet exists.  This is used for context
        injection where the full markdown is needed, not parsed fields.
        """
        path = self._factsheet_path(project_id)
        if not os.path.isfile(path):
            return None

        try:
            with open(path) as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read factsheet for project {project_id}: {e}")
            return None

    async def write_factsheet(
        self,
        project_id: str,
        factsheet: ProjectFactsheet,
        workspace_path: str = "",
    ) -> str | None:
        """Write a factsheet to disk and optionally re-index.

        Updates the ``last_updated`` timestamp automatically.
        Returns the file path on success, ``None`` on failure.
        """
        # Update the timestamp
        factsheet.raw_yaml["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        content = self._serialize_factsheet(factsheet)
        path = self._factsheet_path(project_id)

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.warning(f"Failed to write factsheet for project {project_id}: {e}")
            return None

        # Re-index the factsheet if we have a memsearch instance
        if workspace_path:
            instance = await self.get_instance(project_id, workspace_path)
            if instance:
                try:
                    await instance.index_file(path)
                except Exception as e:
                    logger.warning(f"Factsheet indexing failed for project {project_id}: {e}")

        logger.info(f"Factsheet written for project {project_id}")
        return path

    async def write_factsheet_raw(
        self, project_id: str, content: str, workspace_path: str = ""
    ) -> str | None:
        """Write raw factsheet content (string) and re-index it.

        Unlike ``write_factsheet`` which accepts a ``ProjectFactsheet``,
        this accepts a raw string (YAML frontmatter + markdown).
        Returns the file path on success, ``None`` otherwise.
        """
        path = self._factsheet_path(project_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.warning(f"Failed to write factsheet for project {project_id}: {e}")
            return None

        # Re-index the factsheet file if we have an instance
        if workspace_path:
            instance = await self.get_instance(project_id, workspace_path)
            if instance:
                try:
                    await instance.index_file(path)
                except Exception as e:
                    logger.warning(f"Factsheet indexing failed for project {project_id}: {e}")

        return path

    async def update_factsheet_field(
        self,
        project_id: str,
        dotted_key: str,
        value: Any,
        workspace_path: str = "",
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> str | None:
        """Update a single field in the project factsheet.

        Creates the factsheet from the seed template if it doesn't exist yet.
        Uses dot notation for nested keys (e.g. ``"urls.github"``).

        Returns the file path on success, ``None`` on failure.
        """
        factsheet = await self.read_factsheet(project_id)
        if factsheet is None:
            # Bootstrap from seed template
            factsheet = await self._seed_factsheet(
                project_id,
                project_name=project_name,
                repo_url=repo_url,
            )

        factsheet.set_field(dotted_key, value)
        return await self.write_factsheet(project_id, factsheet, workspace_path)

    async def _seed_factsheet(
        self,
        project_id: str,
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> ProjectFactsheet:
        """Create a new factsheet from the seed template.

        Auto-populates ``urls.github`` from the project's ``repo_url``
        database field when available.
        """
        from src.prompts.memory_consolidation import FACTSHEET_SEED_TEMPLATE

        # Format the github_url as a YAML value (quoted string or null)
        github_url_yaml = f'"{repo_url}"' if repo_url else "null"

        raw = FACTSHEET_SEED_TEMPLATE.format(
            last_updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            project_name=project_name or project_id,
            project_id=project_id,
            github_url=github_url_yaml,
        )

        return self._parse_factsheet(raw)

    async def ensure_factsheet(
        self,
        project_id: str,
        workspace_path: str = "",
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> ProjectFactsheet:
        """Ensure a factsheet exists for the project, creating one if needed.

        This is the primary bootstrap entry point — call it during context
        building to guarantee a factsheet exists.  If the factsheet already
        exists, returns it as-is.

        Returns the (possibly newly created) ``ProjectFactsheet``.
        """
        existing = await self.read_factsheet(project_id)
        if existing is not None:
            return existing

        # Seed a new factsheet
        factsheet = await self._seed_factsheet(
            project_id,
            project_name=project_name,
            repo_url=repo_url,
        )
        await self.write_factsheet(project_id, factsheet, workspace_path)
        return factsheet

    # ------------------------------------------------------------------
    # Phase 3b: Knowledge Base Topic Files
    # ------------------------------------------------------------------

    # NOTE: read_knowledge_topic is defined in the Phase 3.6 section below
    # with index_knowledge config checking and sanitized path helpers.

    async def list_knowledge_topics(self, project_id: str) -> list[dict]:
        """List available knowledge base topics for a project.

        Returns a list of dicts with ``topic`` name and ``has_content`` boolean
        for each configured topic. Also includes any extra topic files found
        on disk that aren't in the default config.
        """
        knowledge_dir = self._knowledge_dir(project_id)
        configured = set(self.config.knowledge_topics)

        topics: list[dict] = []
        found_on_disk: set[str] = set()

        # Check disk for existing topic files
        if os.path.isdir(knowledge_dir):
            for fname in sorted(os.listdir(knowledge_dir)):
                if fname.endswith(".md"):
                    topic_name = fname[:-3]  # strip .md
                    found_on_disk.add(topic_name)

        # Report configured topics with presence info
        for topic in self.config.knowledge_topics:
            has_content = topic in found_on_disk
            size = 0
            if has_content:
                try:
                    size = os.path.getsize(os.path.join(knowledge_dir, f"{topic}.md"))
                except OSError:
                    pass
            topics.append(
                {
                    "topic": topic,
                    "has_content": has_content,
                    "size_bytes": size,
                }
            )

        # Include any extra topics found on disk but not in config
        for topic in sorted(found_on_disk - configured):
            try:
                size = os.path.getsize(os.path.join(knowledge_dir, f"{topic}.md"))
            except OSError:
                size = 0
            topics.append(
                {
                    "topic": topic,
                    "has_content": True,
                    "size_bytes": size,
                    "extra": True,
                }
            )

        return topics

    async def search_all_project_factsheets(
        self, project_ids: list[str], query: str = "", field: str = ""
    ) -> list[dict]:
        """Search across multiple project factsheets for metadata.

        If ``field`` is provided, extracts that specific YAML field from each
        factsheet (dot-notation, e.g. ``"urls.github"``). If ``query`` is
        provided, performs a case-insensitive text match against factsheet
        content.

        Returns a list of dicts with ``project_id``, ``match``, and
        optionally ``field_value``.
        """
        results: list[dict] = []
        query_lower = query.lower() if query else ""

        for pid in project_ids:
            content = await self.read_factsheet_raw(pid)
            if content is None:
                continue

            entry: dict = {"project_id": pid}

            # Field-specific extraction
            if field:
                yaml_data = self.parse_factsheet_yaml(content)
                value = yaml_data
                for key in field.split("."):
                    if isinstance(value, dict):
                        value = value.get(key)
                    else:
                        value = None
                        break
                if value is not None:
                    entry["field"] = field
                    entry["field_value"] = value
                    results.append(entry)
                continue

            # Text search across the whole factsheet
            if query_lower and query_lower in content.lower():
                # Extract a context snippet around the match
                idx = content.lower().find(query_lower)
                start = max(0, idx - 100)
                end = min(len(content), idx + len(query_lower) + 100)
                entry["snippet"] = content[start:end]
                results.append(entry)
            elif not query:
                # No query/field — return summary from YAML
                yaml_data = self.parse_factsheet_yaml(content)
                entry["project_name"] = (
                    yaml_data.get("project", {}).get("name", pid)
                    if isinstance(yaml_data.get("project"), dict)
                    else pid
                )
                entry["has_factsheet"] = True
                results.append(entry)

        return results

    # ------------------------------------------------------------------
    # Phase 2: Post-Task Profile Revision
    # ------------------------------------------------------------------

    def _build_revision_prompt(
        self,
        current_profile: str,
        task_summary: str,
        files_changed: str,
        *,
        task_title: str = "",
        task_type: str = "unknown",
        task_status: str = "completed",
        notes_section: str = "",
    ) -> tuple[str, str]:
        """Build the system and user prompts for a profile revision LLM call.

        Accepts the current profile content, a task summary, and a formatted
        list of changed files. Returns ``(system_prompt, user_prompt)`` ready
        for the LLM provider.
        """
        from src.prompts.memory_revision import (
            REVISION_SYSTEM_PROMPT,
            REVISION_USER_PROMPT,
        )

        system_prompt = REVISION_SYSTEM_PROMPT.format(
            max_size=self.config.profile_max_size,
        )
        user_prompt = REVISION_USER_PROMPT.format(
            current_profile=current_profile,
            task_title=task_title,
            task_type=task_type,
            task_status=task_status,
            task_summary=task_summary,
            files_changed=files_changed,
            notes_section=notes_section,
        )
        return system_prompt, user_prompt

    async def revise_profile(
        self,
        project_id: str,
        task: Any,
        output: Any,
        workspace_path: str,
    ) -> str | None:
        """Revise the project profile based on a completed task.

        Reads the current profile (or seeds from a template), calls an LLM
        to produce an updated version incorporating what the task learned,
        and writes the result back. Only called for COMPLETED tasks.

        Returns the updated profile content on success, ``None`` on failure
        or if revision is disabled.
        """
        if not self.config.revision_enabled or not self.config.profile_enabled:
            return None

        from src.prompts.memory_revision import PROFILE_SEED_TEMPLATE

        # Get current profile or seed
        current_profile = await self.get_profile(project_id)
        if not current_profile:
            current_profile = PROFILE_SEED_TEMPLATE

        # Extract task metadata for the revision prompt
        task_type = (
            task.task_type.value
            if (task.task_type and hasattr(task.task_type, "value"))
            else "unknown"
        )
        status = output.result.value if hasattr(output.result, "value") else str(output.result)
        summary = output.summary or "No summary available."
        files_changed = (
            "\n".join(f"- {f}" for f in (output.files_changed or [])) or "No files changed."
        )

        # Optionally include recent notes in revision context
        notes_section = ""
        if self.config.notes_inform_profile:
            notes_content = self._read_recent_notes(project_id, max_notes=5)
            if notes_content:
                notes_section = f"## Recent Project Notes\n{notes_content}\n\n"

        system_prompt, user_prompt = self._build_revision_prompt(
            current_profile=current_profile,
            task_summary=summary,
            files_changed=files_changed,
            task_title=task.title,
            task_type=task_type,
            task_status=status,
            notes_section=notes_section,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for profile revision")
                return None

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=system_prompt,
                max_tokens=2048,
            )

            # Extract text from response
            new_profile = ""
            for block in response.content:
                if hasattr(block, "text"):
                    new_profile += block.text

            if not new_profile.strip():
                logger.warning("LLM returned empty profile revision")
                return None

            await self.update_profile(project_id, new_profile.strip(), workspace_path)
            logger.info(f"Profile revised for project {project_id} after task {task.id}")
            return new_profile.strip()

        except Exception as e:
            logger.warning(f"Profile revision failed for project {project_id}: {e}")
            return None

    def _get_revision_provider(self) -> Any | None:
        """Create a chat provider for revision/note-generation LLM calls.

        Uses revision_provider/revision_model config if set, otherwise
        falls back to the main chat_provider settings.
        """
        try:
            from src.chat_providers import create_chat_provider
            from src.config import ChatProviderConfig

            provider_name = self.config.revision_provider or "anthropic"
            model_name = self.config.revision_model or ""

            provider_config = ChatProviderConfig(
                provider=provider_name,
                model=model_name,
            )
            return create_chat_provider(provider_config)
        except Exception as e:
            logger.warning(f"Failed to create revision LLM provider: {e}")
            return None

    def _read_recent_notes(self, project_id: str, max_notes: int = 5) -> str:
        """Read the most recent notes for a project.

        Returns formatted markdown of the most recent notes, or empty string.
        """
        notes_dir = self._notes_dir(project_id)
        if not os.path.isdir(notes_dir):
            return ""

        try:
            note_files = sorted(
                glob.glob(os.path.join(notes_dir, "*.md")),
                key=os.path.getmtime,
                reverse=True,
            )[:max_notes]

            if not note_files:
                return ""

            sections = []
            for nf in note_files:
                with open(nf) as f:
                    content = f.read().strip()
                basename = os.path.basename(nf)
                sections.append(f"### {basename}\n{content}")

            return "\n\n".join(sections)
        except Exception as e:
            logger.warning(f"Failed to read notes for project {project_id}: {e}")
            return ""

    async def regenerate_profile(
        self,
        project_id: str,
        workspace_path: str,
    ) -> str | None:
        """Force-regenerate the project profile from task history.

        Reads all stored task memories and recent notes, then calls an LLM
        to synthesize a brand-new profile from scratch. This replaces the
        existing profile entirely.

        Returns the new profile content on success, ``None`` on failure.
        """
        if not self.config.profile_enabled:
            return None

        from src.prompts.memory_revision import (
            REGENERATION_SYSTEM_PROMPT,
            REGENERATION_USER_PROMPT,
        )

        # Gather all task memory files
        tasks_dir = self._tasks_dir(project_id)
        task_summaries: list[str] = []
        if os.path.isdir(tasks_dir):
            task_files = sorted(
                glob.glob(os.path.join(tasks_dir, "*.md")),
                key=os.path.getmtime,
            )
            for tf in task_files:
                try:
                    with open(tf) as f:
                        content = f.read().strip()
                    if content:
                        task_summaries.append(content)
                except Exception:
                    pass

        if not task_summaries:
            logger.info(f"No task history for project {project_id}, cannot regenerate profile")
            return None

        # Optionally include recent notes
        notes_section = ""
        if self.config.notes_inform_profile:
            notes_content = self._read_recent_notes(project_id, max_notes=10)
            if notes_content:
                notes_section = f"## Recent Project Notes\n{notes_content}\n\n"

        system_prompt = REGENERATION_SYSTEM_PROMPT.format(
            max_size=self.config.profile_max_size,
        )
        user_prompt = REGENERATION_USER_PROMPT.format(
            task_count=len(task_summaries),
            task_summaries="\n\n---\n\n".join(task_summaries),
            notes_section=notes_section,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for profile regeneration")
                return None

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=system_prompt,
                max_tokens=2048,
            )

            new_profile = ""
            for block in response.content:
                if hasattr(block, "text"):
                    new_profile += block.text

            if not new_profile.strip():
                logger.warning("LLM returned empty profile during regeneration")
                return None

            await self.update_profile(project_id, new_profile.strip(), workspace_path)
            logger.info(
                f"Profile regenerated for project {project_id} from {len(task_summaries)} tasks"
            )
            return new_profile.strip()

        except Exception as e:
            logger.warning(f"Profile regeneration failed for project {project_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Phase 3: Notes Integration
    # ------------------------------------------------------------------

    async def generate_task_notes(
        self, project_id: str, task: Any, output: Any, workspace_path: str
    ) -> list[str]:
        """Auto-generate notes from a completed task if warranted.

        Uses an LLM to assess whether the task produced noteworthy insights
        and, if so, creates categorized note files. Returns a list of note
        file paths created, or an empty list.
        """
        if not self.config.auto_generate_notes:
            return []

        from src.prompts.memory_revision import (
            NOTE_GENERATION_SYSTEM_PROMPT,
            NOTE_GENERATION_USER_PROMPT,
        )

        task_type = (
            task.task_type.value
            if (task.task_type and hasattr(task.task_type, "value"))
            else "unknown"
        )
        summary = output.summary or "No summary available."
        files_changed = (
            "\n".join(f"- {f}" for f in (output.files_changed or [])) or "No files changed."
        )

        user_prompt = NOTE_GENERATION_USER_PROMPT.format(
            task_title=task.title,
            task_type=task_type,
            project_id=project_id,
            task_summary=summary,
            files_changed=files_changed,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                return []

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=NOTE_GENERATION_SYSTEM_PROMPT,
                max_tokens=1024,
            )

            # Extract text and parse JSON
            response_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text

            # Parse the JSON array from the response
            response_text = response_text.strip()
            # Handle markdown code fences
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                # Remove first and last lines (code fences)
                lines = [l for l in lines if not l.strip().startswith("```")]
                response_text = "\n".join(lines)

            notes = json.loads(response_text)
            if not isinstance(notes, list) or not notes:
                return []

            notes_dir = self._notes_dir(project_id)
            os.makedirs(notes_dir, exist_ok=True)

            created_paths: list[str] = []
            timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())

            for note in notes:
                category = note.get("category", "general")
                slug = note.get("slug", "note")
                content = note.get("content", "")
                if not content:
                    continue

                filename = f"{category}-{slug}-{timestamp}.md"
                path = os.path.join(notes_dir, filename)

                with open(path, "w") as f:
                    f.write(content)
                created_paths.append(path)

                # Index the new note file
                instance = await self.get_instance(project_id, workspace_path)
                if instance:
                    try:
                        await instance.index_file(path)
                    except Exception as e:
                        logger.warning(f"Note indexing failed for {path}: {e}")

            if created_paths:
                logger.info(
                    f"Generated {len(created_paths)} note(s) for project {project_id} "
                    f"from task {task.id}"
                )

            return created_paths

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to parse note generation response: {e}")
            return []
        except Exception as e:
            logger.warning(f"Note generation failed for project {project_id}: {e}")
            return []

    async def promote_note(
        self,
        project_id: str,
        note_filename: str,
        note_content: str,
        workspace_path: str,
    ) -> str | None:
        """Incorporate a specific note's content into the project profile.

        Uses an LLM to integrate the note into the existing profile rather
        than simply appending. Returns the updated profile content on success,
        ``None`` on failure.
        """
        if not self.config.profile_enabled:
            return None

        from src.prompts.memory_revision import (
            NOTE_PROMOTION_SYSTEM_PROMPT,
            NOTE_PROMOTION_USER_PROMPT,
            PROFILE_SEED_TEMPLATE,
        )

        current_profile = await self.get_profile(project_id)
        if not current_profile:
            current_profile = PROFILE_SEED_TEMPLATE

        system_prompt = NOTE_PROMOTION_SYSTEM_PROMPT.format(
            max_size=self.config.profile_max_size,
        )
        user_prompt = NOTE_PROMOTION_USER_PROMPT.format(
            current_profile=current_profile,
            note_filename=note_filename,
            note_content=note_content,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for note promotion")
                return None

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=system_prompt,
                max_tokens=2048,
            )

            new_profile = ""
            for block in response.content:
                if hasattr(block, "text"):
                    new_profile += block.text

            if not new_profile.strip():
                logger.warning("LLM returned empty profile during note promotion")
                return None

            await self.update_profile(project_id, new_profile.strip(), workspace_path)
            logger.info(f"Note '{note_filename}' promoted into profile for project {project_id}")
            return new_profile.strip()

        except Exception as e:
            logger.warning(f"Note promotion failed for project {project_id}: {e}")
            return None

    # ------------------------------------------------------------------
    # Phase 3.5: Post-Task Fact Extraction (Memory Consolidation)
    # ------------------------------------------------------------------

    def _staging_dir(self, project_id: str) -> str:
        """Path to the fact-staging directory for a project.

        Returns ``{data_dir}/memory/{project_id}/staging/``.
        """
        return os.path.join(self._project_memory_dir(project_id), "staging")

    async def extract_task_facts(
        self,
        project_id: str,
        task: Any,
        output: Any,
        workspace_path: str,
    ) -> str | None:
        """Extract structured facts from a completed task into a staging file.

        Uses an LLM to identify concrete facts (URLs, tech stack, decisions,
        conventions, architecture, config, contacts) from the task output and
        writes them to ``memory/{project_id}/staging/{task_id}.json``.

        These staging files are later consumed by the daily consolidation
        process (Phase 4) to update the project factsheet and knowledge base.

        Returns the staging file path on success, ``None`` on failure or if
        fact extraction is disabled.
        """
        if not self.config.fact_extraction_enabled:
            return None

        from src.prompts.memory_consolidation import (
            FACT_EXTRACTION_SYSTEM_PROMPT,
            FACT_EXTRACTION_USER_PROMPT,
        )

        # Extract task metadata
        task_type = (
            task.task_type.value
            if (task.task_type and hasattr(task.task_type, "value"))
            else "unknown"
        )
        summary = output.summary or "No summary available."
        files_changed = (
            "\n".join(f"- {f}" for f in (output.files_changed or [])) or "No files changed."
        )

        user_prompt = FACT_EXTRACTION_USER_PROMPT.format(
            task_id=task.id,
            task_title=task.title,
            task_type=task_type,
            project_id=project_id,
            task_summary=summary,
            files_changed=files_changed,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for fact extraction")
                return None

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=FACT_EXTRACTION_SYSTEM_PROMPT,
                max_tokens=1024,
            )

            # Extract text from response
            response_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text

            # Parse the JSON array from the response
            response_text = response_text.strip()
            # Handle markdown code fences
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                lines = [line for line in lines if not line.strip().startswith("```")]
                response_text = "\n".join(lines)

            facts = json.loads(response_text)
            if not isinstance(facts, list):
                logger.warning("Fact extraction returned non-array response")
                return None

            # Validate fact structure — keep only well-formed entries
            valid_categories = {
                "url",
                "tech_stack",
                "decision",
                "convention",
                "architecture",
                "config",
                "contact",
            }
            validated_facts = []
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                category = fact.get("category", "")
                key = fact.get("key", "")
                value = fact.get("value", "")
                if category in valid_categories and key and value:
                    validated_facts.append(
                        {
                            "category": category,
                            "key": key,
                            "value": value,
                        }
                    )

            # Build staging document
            staging_doc = {
                "task_id": task.id,
                "project_id": project_id,
                "task_title": task.title,
                "task_type": task_type,
                "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "facts": validated_facts,
            }

            # Write staging file
            staging_dir = self._staging_dir(project_id)
            os.makedirs(staging_dir, exist_ok=True)
            staging_path = os.path.join(staging_dir, f"{task.id}.json")

            with open(staging_path, "w") as f:
                json.dump(staging_doc, f, indent=2)

            if validated_facts:
                logger.info(
                    "Extracted %d fact(s) from task %s for project %s",
                    len(validated_facts),
                    task.id,
                    project_id,
                )
            else:
                logger.debug(
                    "No facts extracted from task %s for project %s",
                    task.id,
                    project_id,
                )

            return staging_path

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse fact extraction response: %s", e)
            return None
        except Exception as e:
            logger.warning("Fact extraction failed for project %s: %s", project_id, e)
            return None

    # ------------------------------------------------------------------
    # Phase 3.6: Knowledge Base Topic Files
    # ------------------------------------------------------------------

    def _knowledge_dir(self, project_id: str) -> str:
        """Path to the knowledge base directory for a project.

        Returns ``{data_dir}/memory/{project_id}/knowledge/``.
        """
        return os.path.join(self._project_memory_dir(project_id), "knowledge")

    def _knowledge_topic_path(self, project_id: str, topic: str) -> str:
        """Path to a specific knowledge topic file.

        Returns ``{data_dir}/memory/{project_id}/knowledge/{topic}.md``.
        """
        # Sanitize topic to prevent directory traversal
        safe_topic = topic.replace("/", "").replace("\\", "").replace("..", "")
        return os.path.join(self._knowledge_dir(project_id), f"{safe_topic}.md")

    async def read_knowledge_topic(self, project_id: str, topic: str) -> str | None:
        """Read a knowledge topic file for a project.

        Returns the file content as a string, or ``None`` if the topic file
        doesn't exist or knowledge indexing is disabled.
        """
        if not self.config.index_knowledge:
            return None

        path = self._knowledge_topic_path(project_id, topic)
        if not os.path.isfile(path):
            return None

        try:
            with open(path) as f:
                return f.read()
        except Exception as e:
            logger.warning(
                f"Failed to read knowledge topic '{topic}' for project {project_id}: {e}"
            )
            return None

    async def write_knowledge_topic(
        self,
        project_id: str,
        topic: str,
        content: str,
        workspace_path: str = "",
    ) -> str | None:
        """Write content to a knowledge topic file and optionally re-index.

        Creates the knowledge directory if it doesn't exist.
        Returns the file path on success, ``None`` on failure.
        """
        if not self.config.index_knowledge:
            return None

        # Validate topic against configured topics
        if topic not in self.config.knowledge_topics:
            logger.warning(
                f"Topic '{topic}' not in configured knowledge_topics for project {project_id}"
            )
            return None

        path = self._knowledge_topic_path(project_id, topic)

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.warning(
                f"Failed to write knowledge topic '{topic}' for project {project_id}: {e}"
            )
            return None

        # Re-index the topic file if we have a memsearch instance
        if workspace_path:
            instance = await self.get_instance(project_id, workspace_path)
            if instance:
                try:
                    await instance.index_file(path)
                except Exception as e:
                    logger.warning(
                        f"Knowledge topic indexing failed for '{topic}' "
                        f"in project {project_id}: {e}"
                    )

        logger.info(f"Knowledge topic '{topic}' written for project {project_id}")
        return path

    async def ensure_knowledge_topic(
        self,
        project_id: str,
        topic: str,
        workspace_path: str = "",
    ) -> str | None:
        """Ensure a knowledge topic file exists, seeding from template if needed.

        Returns the file path on success, ``None`` on failure or if the
        topic is not in the configured list.
        """
        if not self.config.index_knowledge:
            return None

        if topic not in self.config.knowledge_topics:
            return None

        path = self._knowledge_topic_path(project_id, topic)
        if os.path.isfile(path):
            return path

        # Seed from template
        from src.prompts.memory_consolidation import KNOWLEDGE_TOPIC_SEED_TEMPLATES

        template = KNOWLEDGE_TOPIC_SEED_TEMPLATES.get(topic)
        if not template:
            logger.warning(f"No seed template found for knowledge topic '{topic}'")
            return None

        content = template.format(
            last_updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        return await self.write_knowledge_topic(project_id, topic, content, workspace_path)

    # NOTE: list_knowledge_topics is defined earlier (Phase 3.6 section)
    # with a richer API (has_content, size_bytes, extra topic discovery).

    # ------------------------------------------------------------------
    # Phase 4: Daily Consolidation Process
    # ------------------------------------------------------------------

    def _staging_processed_dir(self, project_id: str) -> str:
        """Path to the processed staging directory for a project.

        Returns ``{data_dir}/memory/{project_id}/staging/processed/``.
        Staging files are moved here after successful consolidation so they
        aren't re-processed, but remain available for auditing.
        """
        return os.path.join(self._staging_dir(project_id), "processed")

    def _read_staging_files(self, project_id: str) -> list[dict]:
        """Read all unprocessed staging JSON files for a project.

        Returns a list of staging documents sorted by ``extracted_at``
        (oldest first).  Malformed files are logged and skipped.
        """
        staging_dir = self._staging_dir(project_id)
        if not os.path.isdir(staging_dir):
            return []

        docs: list[dict] = []
        for filename in os.listdir(staging_dir):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(staging_dir, filename)
            if not os.path.isfile(filepath):
                continue
            try:
                with open(filepath) as f:
                    doc = json.load(f)
                if isinstance(doc, dict) and "facts" in doc:
                    doc["_filepath"] = filepath
                    docs.append(doc)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Skipping malformed staging file %s: %s", filename, e)

        # Sort by extraction time so older facts are processed first
        docs.sort(key=lambda d: d.get("extracted_at", ""))
        return docs

    def _deduplicate_facts(self, staging_docs: list[dict]) -> list[dict]:
        """Flatten and deduplicate facts across multiple staging files.

        When the same ``(category, key)`` pair appears in multiple staging
        files, the most recent one (later in the sorted list) wins.
        Also attaches ``task_id`` from the parent document to each fact
        for source attribution.

        Returns a list of unique fact dicts, each with an added ``task_id``
        and ``task_title`` field.
        """
        seen: dict[tuple[str, str], dict] = {}

        for doc in staging_docs:
            task_id = doc.get("task_id", "unknown")
            task_title = doc.get("task_title", "")
            for fact in doc.get("facts", []):
                category = fact.get("category", "")
                key = fact.get("key", "")
                if not category or not key:
                    continue
                # Later entries overwrite earlier ones (newer facts win)
                enriched = {
                    **fact,
                    "task_id": task_id,
                    "task_title": task_title,
                }
                seen[(category, key)] = enriched

        return list(seen.values())

    def _get_consolidation_provider(self) -> "Any | None":
        """Create a chat provider for consolidation LLM calls.

        Uses consolidation_provider/consolidation_model config if set,
        falls back to revision_provider/revision_model, then to defaults.
        """
        try:
            from src.chat_providers import create_chat_provider
            from src.config import ChatProviderConfig

            provider_name = (
                self.config.consolidation_provider or self.config.revision_provider or "anthropic"
            )
            model_name = self.config.consolidation_model or self.config.revision_model or ""

            provider_config = ChatProviderConfig(
                provider=provider_name,
                model=model_name,
            )
            return create_chat_provider(provider_config)
        except Exception as e:
            logger.warning("Failed to create consolidation LLM provider: %s", e)
            return None

    def _move_to_processed(self, staging_docs: list[dict]) -> int:
        """Move processed staging files to the ``staging/processed/`` directory.

        Returns the number of files successfully moved.
        """
        moved = 0
        for doc in staging_docs:
            filepath = doc.get("_filepath")
            if not filepath or not os.path.isfile(filepath):
                continue
            project_id = doc.get("project_id", "")
            if not project_id:
                continue
            processed_dir = self._staging_processed_dir(project_id)
            os.makedirs(processed_dir, exist_ok=True)
            dest = os.path.join(processed_dir, os.path.basename(filepath))
            try:
                os.rename(filepath, dest)
                moved += 1
            except OSError as e:
                logger.warning("Failed to move staging file %s: %s", filepath, e)
        return moved

    def _group_facts_by_topic(self, facts: list[dict]) -> dict[str, list[dict]]:
        """Group deduplicated facts by knowledge topic.

        Uses ``FACT_CATEGORY_TO_TOPIC`` mapping.  Facts whose category has
        no topic mapping (e.g. ``contact``) are excluded — they only go to
        the factsheet.

        Returns ``{topic_slug: [fact, ...]}`` where each fact retains its
        full metadata including ``task_id``.
        """
        from src.prompts.memory_consolidation import FACT_CATEGORY_TO_TOPIC

        grouped: dict[str, list[dict]] = {}
        for fact in facts:
            category = fact.get("category", "")
            topics = FACT_CATEGORY_TO_TOPIC.get(category, [])
            for topic in topics:
                if topic in self.config.knowledge_topics:
                    grouped.setdefault(topic, []).append(fact)
        return grouped

    async def run_daily_consolidation(
        self,
        project_id: str,
        workspace_path: str = "",
    ) -> dict:
        """Run the daily consolidation process for a project.

        Reads all unprocessed staging files, deduplicates facts, calls an LLM
        to merge them into the project factsheet and relevant knowledge topics,
        then moves processed staging files to ``staging/processed/``.

        Returns a stats dict with:
        - ``status``: ``"consolidated"``, ``"no_staging"``, ``"disabled"``, or ``"error"``
        - ``staging_files_processed``: number of staging files consumed
        - ``facts_consolidated``: number of unique facts merged
        - ``topics_updated``: list of knowledge topics that were modified
        - ``factsheet_updated``: whether the factsheet was modified
        """
        if not self.config.consolidation_enabled:
            return {
                "status": "disabled",
                "project_id": project_id,
                "staging_files_processed": 0,
                "facts_consolidated": 0,
                "topics_updated": [],
                "factsheet_updated": False,
            }

        # 1. Read unprocessed staging files
        staging_docs = self._read_staging_files(project_id)
        if not staging_docs:
            return {
                "status": "no_staging",
                "project_id": project_id,
                "staging_files_processed": 0,
                "facts_consolidated": 0,
                "topics_updated": [],
                "factsheet_updated": False,
            }

        # 2. Deduplicate facts across all staging files
        unique_facts = self._deduplicate_facts(staging_docs)
        if not unique_facts:
            # Staging files exist but contain no facts — still move to processed
            self._move_to_processed(staging_docs)
            return {
                "status": "no_facts",
                "project_id": project_id,
                "staging_files_processed": len(staging_docs),
                "facts_consolidated": 0,
                "topics_updated": [],
                "factsheet_updated": False,
            }

        # 3. Read current factsheet
        factsheet = await self.read_factsheet(project_id)
        factsheet_yaml = ""
        if factsheet:
            try:
                import yaml as _yaml

                factsheet_yaml = _yaml.dump(
                    factsheet.raw_yaml,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            except Exception:
                factsheet_yaml = str(factsheet.raw_yaml)
        else:
            factsheet_yaml = "(no factsheet exists yet)"

        # 4. Identify which topics need updating and read their current content
        facts_by_topic = self._group_facts_by_topic(unique_facts)
        knowledge_sections: list[str] = []
        for topic in sorted(facts_by_topic.keys()):
            content = await self.read_knowledge_topic(project_id, topic)
            if content:
                knowledge_sections.append(f"### {topic}\n```markdown\n{content}\n```")
            else:
                # Ensure the topic file exists (seed from template)
                await self.ensure_knowledge_topic(project_id, topic, workspace_path)
                content = await self.read_knowledge_topic(project_id, topic)
                if content:
                    knowledge_sections.append(f"### {topic}\n```markdown\n{content}\n```")

        knowledge_topics_section = (
            "\n\n".join(knowledge_sections)
            if knowledge_sections
            else "(no knowledge topics to update)"
        )

        # 5. Format facts for the LLM prompt
        facts_lines: list[str] = []
        for fact in unique_facts:
            facts_lines.append(
                f"- **[{fact['category']}]** `{fact['key']}`: {fact['value']} "
                f"(from task: {fact['task_id']})"
            )
        facts_section = "\n".join(facts_lines)

        # 6. Call LLM for consolidation
        from src.prompts.memory_consolidation import (
            DAILY_CONSOLIDATION_SYSTEM_PROMPT,
            DAILY_CONSOLIDATION_USER_PROMPT,
        )

        user_prompt = DAILY_CONSOLIDATION_USER_PROMPT.format(
            current_factsheet_yaml=factsheet_yaml,
            knowledge_topics_section=knowledge_topics_section,
            facts_section=facts_section,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        try:
            provider = self._get_consolidation_provider()
            if not provider:
                logger.warning("No LLM provider available for consolidation")
                return {
                    "status": "error",
                    "error": "no_provider",
                    "project_id": project_id,
                    "staging_files_processed": 0,
                    "facts_consolidated": 0,
                    "topics_updated": [],
                    "factsheet_updated": False,
                }

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=DAILY_CONSOLIDATION_SYSTEM_PROMPT,
                max_tokens=4096,
            )

            # Extract text from response
            response_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text

            # Parse the JSON response
            response_text = response_text.strip()
            # Handle markdown code fences
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                lines = [line for line in lines if not line.strip().startswith("```")]
                response_text = "\n".join(lines)

            result = json.loads(response_text)
            if not isinstance(result, dict):
                logger.warning("Consolidation LLM returned non-object response")
                return {
                    "status": "error",
                    "error": "invalid_response",
                    "project_id": project_id,
                    "staging_files_processed": 0,
                    "facts_consolidated": len(unique_facts),
                    "topics_updated": [],
                    "factsheet_updated": False,
                }

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse consolidation response: %s", e)
            return {
                "status": "error",
                "error": f"parse_error: {e}",
                "project_id": project_id,
                "staging_files_processed": 0,
                "facts_consolidated": len(unique_facts),
                "topics_updated": [],
                "factsheet_updated": False,
            }
        except Exception as e:
            logger.warning("Consolidation LLM call failed for project %s: %s", project_id, e)
            return {
                "status": "error",
                "error": str(e),
                "project_id": project_id,
                "staging_files_processed": 0,
                "facts_consolidated": len(unique_facts),
                "topics_updated": [],
                "factsheet_updated": False,
            }

        # 7. Apply factsheet updates
        factsheet_updated = False
        new_yaml_data = result.get("factsheet_yaml", "")
        if new_yaml_data and factsheet:
            try:
                # The LLM may return a dict (from JSON) or a YAML string
                if isinstance(new_yaml_data, dict):
                    new_yaml = new_yaml_data
                else:
                    import yaml as _yaml

                    new_yaml = _yaml.safe_load(new_yaml_data)
                if isinstance(new_yaml, dict):
                    factsheet.raw_yaml = new_yaml
                    await self.write_factsheet(project_id, factsheet, workspace_path)
                    factsheet_updated = True
            except Exception as e:
                logger.warning("Failed to apply factsheet update: %s", e)

        # 8. Apply knowledge topic updates
        topics_updated: list[str] = []
        knowledge_updates = result.get("knowledge_updates", {})
        if isinstance(knowledge_updates, dict):
            for topic, content in knowledge_updates.items():
                if not isinstance(content, str) or not content.strip():
                    continue
                if topic not in self.config.knowledge_topics:
                    logger.debug("Skipping unknown topic '%s' from LLM output", topic)
                    continue
                written = await self.write_knowledge_topic(
                    project_id, topic, content, workspace_path
                )
                if written:
                    topics_updated.append(topic)

        # 9. Move staging files to processed/
        moved = self._move_to_processed(staging_docs)

        stats = {
            "status": "consolidated",
            "project_id": project_id,
            "staging_files_processed": moved,
            "facts_consolidated": len(unique_facts),
            "topics_updated": topics_updated,
            "factsheet_updated": factsheet_updated,
        }
        logger.info(
            "Consolidation for %s: %d facts from %d staging files, %d topics updated, factsheet %s",
            project_id,
            len(unique_facts),
            moved,
            len(topics_updated),
            "updated" if factsheet_updated else "unchanged",
        )
        return stats

    # ------------------------------------------------------------------
    # Phase 6: Weekly Deep Consolidation & Bootstrap
    # ------------------------------------------------------------------

    async def run_deep_consolidation(
        self,
        project_id: str,
        workspace_path: str = "",
    ) -> dict:
        """Run a deep consolidation pass for a project.

        Unlike the daily consolidation (which merges new staging facts), deep
        consolidation reviews the *entire* knowledge base — pruning stale facts,
        resolving conflicts, and regenerating the factsheet summary sections.
        Intended to run weekly.

        Returns a stats dict with:
        - ``status``: ``"consolidated"``, ``"no_knowledge"``, ``"disabled"``, or ``"error"``
        - ``topics_reviewed``: number of knowledge topics examined
        - ``topics_updated``: list of topics that were modified
        - ``factsheet_updated``: whether the factsheet was modified
        - ``pruned_facts``: list of descriptions of pruned/removed facts
        """
        if not self.config.consolidation_enabled:
            return {
                "status": "disabled",
                "project_id": project_id,
                "topics_reviewed": 0,
                "topics_updated": [],
                "factsheet_updated": False,
                "pruned_facts": [],
            }

        # 1. Read current factsheet
        factsheet = await self.read_factsheet(project_id)
        factsheet_yaml = ""
        factsheet_body = ""
        if factsheet:
            try:
                import yaml as _yaml

                factsheet_yaml = _yaml.dump(
                    factsheet.raw_yaml,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            except Exception:
                factsheet_yaml = str(factsheet.raw_yaml)
            factsheet_body = factsheet.body_markdown
        else:
            factsheet_yaml = "(no factsheet exists yet)"
            factsheet_body = "(no factsheet body)"

        # 2. Read all knowledge topics
        knowledge_sections: list[str] = []
        topics_reviewed = 0
        for topic in self.config.knowledge_topics:
            content = await self.read_knowledge_topic(project_id, topic)
            if content:
                knowledge_sections.append(f"### {topic}\n```markdown\n{content}\n```")
                topics_reviewed += 1

        if not knowledge_sections and not factsheet:
            return {
                "status": "no_knowledge",
                "project_id": project_id,
                "topics_reviewed": 0,
                "topics_updated": [],
                "factsheet_updated": False,
                "pruned_facts": [],
            }

        knowledge_topics_section = (
            "\n\n".join(knowledge_sections)
            if knowledge_sections
            else "(no knowledge topics exist yet)"
        )

        # 3. Count processed staging files for context
        processed_dir = self._staging_processed_dir(project_id)
        processed_count = 0
        if os.path.isdir(processed_dir):
            processed_count = len([f for f in os.listdir(processed_dir) if f.endswith(".json")])

        # 4. Call LLM for deep consolidation
        from src.prompts.memory_consolidation import (
            DEEP_CONSOLIDATION_SYSTEM_PROMPT,
            DEEP_CONSOLIDATION_USER_PROMPT,
        )

        user_prompt = DEEP_CONSOLIDATION_USER_PROMPT.format(
            current_factsheet_yaml=factsheet_yaml,
            current_factsheet_body=factsheet_body,
            knowledge_topics_section=knowledge_topics_section,
            processed_count=processed_count,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        try:
            provider = self._get_consolidation_provider()
            if not provider:
                logger.warning("No LLM provider available for deep consolidation")
                return {
                    "status": "error",
                    "error": "no_provider",
                    "project_id": project_id,
                    "topics_reviewed": topics_reviewed,
                    "topics_updated": [],
                    "factsheet_updated": False,
                    "pruned_facts": [],
                }

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=DEEP_CONSOLIDATION_SYSTEM_PROMPT,
                max_tokens=8192,
            )

            # Extract text from response
            response_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text

            # Parse the JSON response
            response_text = response_text.strip()
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                lines = [line for line in lines if not line.strip().startswith("```")]
                response_text = "\n".join(lines)

            result = json.loads(response_text)
            if not isinstance(result, dict):
                logger.warning("Deep consolidation LLM returned non-object response")
                return {
                    "status": "error",
                    "error": "invalid_response",
                    "project_id": project_id,
                    "topics_reviewed": topics_reviewed,
                    "topics_updated": [],
                    "factsheet_updated": False,
                    "pruned_facts": [],
                }

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse deep consolidation response: %s", e)
            return {
                "status": "error",
                "error": f"parse_error: {e}",
                "project_id": project_id,
                "topics_reviewed": topics_reviewed,
                "topics_updated": [],
                "factsheet_updated": False,
                "pruned_facts": [],
            }
        except Exception as e:
            logger.warning("Deep consolidation LLM call failed for project %s: %s", project_id, e)
            return {
                "status": "error",
                "error": str(e),
                "project_id": project_id,
                "topics_reviewed": topics_reviewed,
                "topics_updated": [],
                "factsheet_updated": False,
                "pruned_facts": [],
            }

        # 5. Apply factsheet updates (both YAML and body)
        factsheet_updated = False
        new_yaml_data = result.get("factsheet_yaml", "")
        new_body = result.get("factsheet_body", "")

        if factsheet is None:
            # Create a new factsheet if deep consolidation produced one
            if new_yaml_data or new_body:
                factsheet = await self._seed_factsheet(project_id)

        if factsheet:
            try:
                if new_yaml_data:
                    if isinstance(new_yaml_data, dict):
                        new_yaml = new_yaml_data
                    else:
                        import yaml as _yaml

                        new_yaml = _yaml.safe_load(new_yaml_data)
                    if isinstance(new_yaml, dict):
                        factsheet.raw_yaml = new_yaml
                        factsheet_updated = True

                if new_body and isinstance(new_body, str) and new_body.strip():
                    factsheet.body_markdown = new_body.strip()
                    factsheet_updated = True

                if factsheet_updated:
                    await self.write_factsheet(project_id, factsheet, workspace_path)
            except Exception as e:
                logger.warning("Failed to apply deep consolidation factsheet update: %s", e)

        # 6. Apply knowledge topic updates
        topics_updated: list[str] = []
        knowledge_updates = result.get("knowledge_updates", {})
        if isinstance(knowledge_updates, dict):
            for topic, content in knowledge_updates.items():
                if not isinstance(content, str) or not content.strip():
                    continue
                if topic not in self.config.knowledge_topics:
                    logger.debug("Skipping unknown topic '%s' from LLM output", topic)
                    continue
                written = await self.write_knowledge_topic(
                    project_id, topic, content, workspace_path
                )
                if written:
                    topics_updated.append(topic)

        # 7. Collect pruned facts for audit logging
        pruned_facts = result.get("pruned_facts", [])
        if not isinstance(pruned_facts, list):
            pruned_facts = []

        stats = {
            "status": "consolidated",
            "project_id": project_id,
            "topics_reviewed": topics_reviewed,
            "topics_updated": topics_updated,
            "factsheet_updated": factsheet_updated,
            "pruned_facts": pruned_facts,
        }
        logger.info(
            "Deep consolidation for %s: reviewed %d topics, updated %d, "
            "factsheet %s, %d facts pruned",
            project_id,
            topics_reviewed,
            len(topics_updated),
            "updated" if factsheet_updated else "unchanged",
            len(pruned_facts),
        )
        return stats

    async def bootstrap_consolidation(
        self,
        project_id: str,
        workspace_path: str = "",
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> dict:
        """Bootstrap a project's knowledge base from existing task memories.

        Reads all existing task memory files (and profile if available),
        calls an LLM to synthesize them into an initial factsheet and
        knowledge base entries.  This is a one-time operation for projects
        that have task history but no structured knowledge base yet.

        Returns a stats dict with:
        - ``status``: ``"bootstrapped"``, ``"no_tasks"``, ``"already_exists"``, or ``"error"``
        - ``tasks_processed``: number of task memories used as input
        - ``topics_created``: list of knowledge topics that were populated
        - ``factsheet_created``: whether a new factsheet was created
        """
        # Check if a factsheet already exists — bootstrap is one-time
        existing_factsheet = await self.read_factsheet(project_id)
        existing_topics = await self.list_knowledge_topics(project_id)
        has_knowledge = any(t.get("has_content") for t in existing_topics)

        if existing_factsheet and has_knowledge:
            return {
                "status": "already_exists",
                "project_id": project_id,
                "message": (
                    "Project already has a factsheet and knowledge base. "
                    "Use daily or deep consolidation to update them."
                ),
                "tasks_processed": 0,
                "topics_created": [],
                "factsheet_created": False,
            }

        # 1. Read all task memory files
        tasks_dir = self._tasks_dir(project_id)
        task_summaries: list[str] = []
        if os.path.isdir(tasks_dir):
            task_files = sorted(
                glob.glob(os.path.join(tasks_dir, "*.md")),
                key=os.path.getmtime,
            )
            for tf in task_files:
                try:
                    with open(tf) as f:
                        content = f.read().strip()
                    if content:
                        task_summaries.append(content)
                except Exception:
                    pass

        # Also read digest files if they exist
        digests_dir = os.path.join(self._project_memory_dir(project_id), "digests")
        if os.path.isdir(digests_dir):
            digest_files = sorted(
                glob.glob(os.path.join(digests_dir, "*.md")),
                key=os.path.getmtime,
            )
            for df in digest_files:
                try:
                    with open(df) as f:
                        content = f.read().strip()
                    if content:
                        task_summaries.append(content)
                except Exception:
                    pass

        if not task_summaries:
            return {
                "status": "no_tasks",
                "project_id": project_id,
                "message": "No task memories found to bootstrap from.",
                "tasks_processed": 0,
                "topics_created": [],
                "factsheet_created": False,
            }

        # 2. Read existing profile if available
        existing_profile = await self.get_profile(project_id) or "(no profile exists)"

        # 3. Prepare seed factsheet YAML for the LLM to fill in
        seed_factsheet = await self._seed_factsheet(
            project_id,
            project_name=project_name,
            repo_url=repo_url,
        )
        try:
            import yaml as _yaml

            seed_yaml = _yaml.dump(
                seed_factsheet.raw_yaml,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
        except Exception:
            seed_yaml = str(seed_factsheet.raw_yaml)

        # 4. Format available topics
        available_topics = "\n".join(f"- `{topic}`" for topic in self.config.knowledge_topics)

        # 5. Limit task summaries to avoid exceeding context window
        # Keep most recent tasks, truncate older ones
        max_task_chars = 30000
        formatted_tasks: list[str] = []
        total_chars = 0
        for i, summary in enumerate(reversed(task_summaries)):
            if total_chars + len(summary) > max_task_chars and formatted_tasks:
                formatted_tasks.append(
                    f"... and {len(task_summaries) - i} older tasks omitted for brevity"
                )
                break
            formatted_tasks.append(f"---\n{summary}")
            total_chars += len(summary)
        formatted_tasks.reverse()

        # 6. Call LLM for bootstrap
        from src.prompts.memory_consolidation import (
            BOOTSTRAP_SYSTEM_PROMPT,
            BOOTSTRAP_USER_PROMPT,
        )

        user_prompt = BOOTSTRAP_USER_PROMPT.format(
            project_id=project_id,
            project_name=project_name or project_id,
            repo_url=repo_url or "(not configured)",
            existing_profile=existing_profile,
            seed_yaml=seed_yaml,
            available_topics=available_topics,
            task_count=len(task_summaries),
            task_summaries="\n\n".join(formatted_tasks),
        )

        try:
            provider = self._get_consolidation_provider()
            if not provider:
                logger.warning("No LLM provider available for bootstrap consolidation")
                return {
                    "status": "error",
                    "error": "no_provider",
                    "project_id": project_id,
                    "tasks_processed": len(task_summaries),
                    "topics_created": [],
                    "factsheet_created": False,
                }

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=BOOTSTRAP_SYSTEM_PROMPT,
                max_tokens=8192,
            )

            # Extract text from response
            response_text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    response_text += block.text

            # Parse the JSON response
            response_text = response_text.strip()
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                lines = [line for line in lines if not line.strip().startswith("```")]
                response_text = "\n".join(lines)

            result = json.loads(response_text)
            if not isinstance(result, dict):
                logger.warning("Bootstrap LLM returned non-object response")
                return {
                    "status": "error",
                    "error": "invalid_response",
                    "project_id": project_id,
                    "tasks_processed": len(task_summaries),
                    "topics_created": [],
                    "factsheet_created": False,
                }

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse bootstrap response: %s", e)
            return {
                "status": "error",
                "error": f"parse_error: {e}",
                "project_id": project_id,
                "tasks_processed": len(task_summaries),
                "topics_created": [],
                "factsheet_created": False,
            }
        except Exception as e:
            logger.warning("Bootstrap LLM call failed for project %s: %s", project_id, e)
            return {
                "status": "error",
                "error": str(e),
                "project_id": project_id,
                "tasks_processed": len(task_summaries),
                "topics_created": [],
                "factsheet_created": False,
            }

        # 7. Create or update the factsheet
        factsheet_created = False
        new_yaml_data = result.get("factsheet_yaml", "")
        new_body = result.get("factsheet_body", "")

        if new_yaml_data or new_body:
            fs = existing_factsheet or seed_factsheet
            try:
                if new_yaml_data:
                    if isinstance(new_yaml_data, dict):
                        fs.raw_yaml = new_yaml_data
                    else:
                        import yaml as _yaml

                        parsed_yaml = _yaml.safe_load(new_yaml_data)
                        if isinstance(parsed_yaml, dict):
                            fs.raw_yaml = parsed_yaml

                if new_body and isinstance(new_body, str) and new_body.strip():
                    fs.body_markdown = new_body.strip()

                await self.write_factsheet(project_id, fs, workspace_path)
                factsheet_created = existing_factsheet is None
            except Exception as e:
                logger.warning("Failed to write bootstrapped factsheet: %s", e)

        # 8. Create knowledge topic files
        topics_created: list[str] = []
        knowledge_updates = result.get("knowledge_updates", {})
        if isinstance(knowledge_updates, dict):
            for topic, content in knowledge_updates.items():
                if not isinstance(content, str) or not content.strip():
                    continue
                if topic not in self.config.knowledge_topics:
                    logger.debug("Skipping unknown topic '%s' from bootstrap output", topic)
                    continue
                written = await self.write_knowledge_topic(
                    project_id, topic, content, workspace_path
                )
                if written:
                    topics_created.append(topic)

        stats = {
            "status": "bootstrapped",
            "project_id": project_id,
            "tasks_processed": len(task_summaries),
            "topics_created": topics_created,
            "factsheet_created": factsheet_created,
        }
        logger.info(
            "Bootstrap consolidation for %s: %d tasks processed, factsheet %s, %d topics created",
            project_id,
            len(task_summaries),
            "created" if factsheet_created else "updated",
            len(topics_created),
        )
        return stats

    # ------------------------------------------------------------------
    # Phase 4b: Memory Compaction & Enhanced Context Delivery
    # ------------------------------------------------------------------

    async def compact(self, project_id: str, workspace_path: str) -> dict:
        """Compact old task memories into weekly digest files.

        Groups task memory files by age:
        - **Recent** (< ``compact_recent_days``): kept as-is (full detail).
        - **Medium** (``compact_recent_days`` .. ``compact_archive_days``):
          LLM-summarized into weekly digest files under ``digests/``.
        - **Old** (> ``compact_archive_days``): individual task files deleted
          after their content has been included in a digest.

        Returns a stats dict with counts of tasks inspected, digests
        created, and files removed.
        """
        tasks_dir = self._tasks_dir(project_id)
        digests_dir = os.path.join(self._project_memory_dir(project_id), "digests")

        if not os.path.isdir(tasks_dir):
            return {
                "status": "no_tasks",
                "project_id": project_id,
                "tasks_inspected": 0,
                "digests_created": 0,
                "files_removed": 0,
            }

        now = time.time()
        recent_cutoff = now - (self.config.compact_recent_days * 86400)
        archive_cutoff = now - (self.config.compact_archive_days * 86400)

        # Classify task files by age tier
        recent: list[str] = []
        medium: list[str] = []  # candidates for digesting
        old: list[str] = []  # candidates for deletion after digesting

        task_files = glob.glob(os.path.join(tasks_dir, "*.md"))
        for tf in task_files:
            try:
                mtime = os.path.getmtime(tf)
            except OSError:
                continue
            if mtime >= recent_cutoff:
                recent.append(tf)
            elif mtime >= archive_cutoff:
                medium.append(tf)
            else:
                old.append(tf)

        # Combine medium + old as candidates for digesting (old files will
        # additionally be deleted after digesting).
        to_digest = medium + old
        digests_created = 0
        files_removed = 0

        if to_digest:
            # Group by ISO week for weekly digests
            week_buckets: dict[str, list[str]] = {}
            for tf in to_digest:
                try:
                    mtime = os.path.getmtime(tf)
                    dt = time.gmtime(mtime)
                    # ISO year-week key, e.g. "2026-W11"
                    import datetime as _dt

                    d = _dt.date(dt.tm_year, dt.tm_mon, dt.tm_mday)
                    iso_year, iso_week, _ = d.isocalendar()
                    week_key = f"{iso_year}-W{iso_week:02d}"
                except (OSError, ValueError):
                    week_key = "unknown"
                week_buckets.setdefault(week_key, []).append(tf)

            os.makedirs(digests_dir, exist_ok=True)
            old_set = set(old)

            for week_key, files in sorted(week_buckets.items()):
                digest_path = os.path.join(digests_dir, f"week-{week_key}.md")

                # Skip if digest already exists for this week
                if os.path.isfile(digest_path):
                    # Still remove old files even if digest already exists
                    for tf in files:
                        if tf in old_set:
                            try:
                                os.remove(tf)
                                files_removed += 1
                            except OSError as e:
                                logger.warning(f"Failed to remove old task file {tf}: {e}")
                    continue

                # Read contents for summarization
                contents: list[str] = []
                for tf in sorted(files, key=os.path.getmtime):
                    try:
                        with open(tf) as f:
                            contents.append(f.read().strip())
                    except OSError:
                        pass

                if not contents:
                    continue

                # LLM-summarize into a digest
                digest_text = await self._summarize_batch(contents, week_key)
                if digest_text:
                    try:
                        with open(digest_path, "w") as f:
                            f.write(digest_text)
                        digests_created += 1

                        # Index the new digest file
                        instance = await self.get_instance(project_id, workspace_path)
                        if instance:
                            try:
                                await instance.index_file(digest_path)
                            except Exception as e:
                                logger.warning(f"Digest indexing failed for {digest_path}: {e}")
                    except OSError as e:
                        logger.warning(f"Failed to write digest {digest_path}: {e}")
                        continue

                # Delete old (> archive_days) individual files now that they're digested
                for tf in files:
                    if tf in old_set:
                        try:
                            os.remove(tf)
                            files_removed += 1
                        except OSError as e:
                            logger.warning(f"Failed to remove old task file {tf}: {e}")

        self._last_compact[project_id] = now

        stats = {
            "status": "compacted",
            "project_id": project_id,
            "tasks_inspected": len(task_files),
            "recent_kept": len(recent),
            "medium_digested": len(medium),
            "old_removed": len(old),
            "digests_created": digests_created,
            "files_removed": files_removed,
        }
        logger.info(
            f"Memory compaction for {project_id}: "
            f"{len(task_files)} inspected, {digests_created} digests created, "
            f"{files_removed} files removed"
        )
        return stats

    async def _summarize_batch(self, task_memories: list[str], date_range: str = "") -> str:
        """LLM-summarize a batch of task memories into a digest.

        Returns the digest markdown text, or empty string on failure.
        """
        from src.prompts.memory_revision import (
            DIGEST_SYSTEM_PROMPT,
            DIGEST_USER_PROMPT,
        )

        combined = "\n\n---\n\n".join(task_memories)
        user_prompt = DIGEST_USER_PROMPT.format(
            task_count=len(task_memories),
            date_range=date_range or "unknown period",
            task_memories=combined,
        )

        try:
            provider = self._get_revision_provider()
            if not provider:
                logger.warning("No LLM provider available for memory digest")
                return ""

            response = await provider.create_message(
                messages=[{"role": "user", "content": user_prompt}],
                system=DIGEST_SYSTEM_PROMPT,
                max_tokens=2048,
            )

            digest = ""
            for block in response.content:
                if hasattr(block, "text"):
                    digest += block.text

            return digest.strip()
        except Exception as e:
            logger.warning(f"Digest summarization failed: {e}")
            return ""

    # ------------------------------------------------------------------
    # L2 Topic Detection (spec §3)
    # ------------------------------------------------------------------

    # Default keyword aliases mapping common terms to knowledge topic names.
    # Keys are lowercase patterns that appear in task text; values are the
    # corresponding knowledge topic name.  The topic name itself is always
    # matched as a keyword too — these aliases catch synonyms and related terms.
    TOPIC_KEYWORD_ALIASES: dict[str, str] = {
        # architecture
        "architect": "architecture",
        "design pattern": "architecture",
        "module structure": "architecture",
        "system design": "architecture",
        "component": "architecture",
        "refactor": "architecture",
        "restructure": "architecture",
        # api-and-endpoints
        "api": "api-and-endpoints",
        "endpoint": "api-and-endpoints",
        "rest": "api-and-endpoints",
        "graphql": "api-and-endpoints",
        "route": "api-and-endpoints",
        "handler": "api-and-endpoints",
        "request": "api-and-endpoints",
        "response": "api-and-endpoints",
        "webhook": "api-and-endpoints",
        # deployment
        "deploy": "deployment",
        "ci/cd": "deployment",
        "ci cd": "deployment",
        "pipeline": "deployment",
        "docker": "deployment",
        "container": "deployment",
        "kubernetes": "deployment",
        "k8s": "deployment",
        "release": "deployment",
        "staging": "deployment",
        "production": "deployment",
        # dependencies
        "dependency": "dependencies",
        "package": "dependencies",
        "library": "dependencies",
        "upgrade": "dependencies",
        "version": "dependencies",
        "import": "dependencies",
        "install": "dependencies",
        "requirements": "dependencies",
        # gotchas
        "gotcha": "gotchas",
        "pitfall": "gotchas",
        "caveat": "gotchas",
        "workaround": "gotchas",
        "edge case": "gotchas",
        "known issue": "gotchas",
        "quirk": "gotchas",
        # conventions
        "convention": "conventions",
        "style": "conventions",
        "naming": "conventions",
        "format": "conventions",
        "lint": "conventions",
        "standard": "conventions",
        "best practice": "conventions",
        "code review": "conventions",
        # decisions
        "decision": "decisions",
        "adr": "decisions",
        "trade-off": "decisions",
        "tradeoff": "decisions",
        "rationale": "decisions",
        "why we": "decisions",
        "chose": "decisions",
    }

    async def detect_topics(
        self,
        project_id: str,
        text: str,
    ) -> list[str]:
        """Detect knowledge topics relevant to the given text.

        Uses keyword matching against available knowledge topics (both
        configured defaults and extra topics found on disk).  Returns topic
        names ordered by match strength (number of keyword hits), limited to
        ``topic_max_knowledge_files`` from config.

        This is a fast, deterministic operation — no LLM calls, no embedding.
        Designed for L2 pre-filtering at task start (spec §3).

        Parameters
        ----------
        project_id:
            Project whose knowledge base to check.
        text:
            Task title + description (or any text to detect topics from).

        Returns
        -------
        list[str]
            Topic names that matched, ordered by relevance (most keyword
            hits first).  Empty if topic detection is disabled or no
            topics matched.
        """
        if not self.config.topic_detection_enabled:
            return []

        if not text or not text.strip():
            return []

        # Collect all available topics (configured + on-disk extras)
        available_topics: set[str] = set()

        # Configured topics are always candidates
        available_topics.update(self.config.knowledge_topics)

        # Also include extra topics found on disk
        knowledge_dir = self._knowledge_dir(project_id)
        if os.path.isdir(knowledge_dir):
            for fname in os.listdir(knowledge_dir):
                if fname.endswith(".md"):
                    available_topics.add(fname[:-3])

        if not available_topics:
            return []

        # Normalize text for matching
        text_lower = text.lower()
        # Tokenize for word-boundary matching
        text_words = set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text_lower))

        # Score each topic by keyword hits
        topic_scores: dict[str, float] = {}

        for topic in available_topics:
            score = 0.0

            # Direct topic name match (highest signal)
            topic_lower = topic.lower()
            # Exact word match for topic name (e.g., "deployment" in text)
            if topic_lower in text_words:
                score += 3.0
            # Hyphenated topic name as substring (e.g., "api-and-endpoints")
            elif topic_lower in text_lower:
                score += 2.5
            # Individual words from hyphenated topic names
            # (e.g., "api" from "api-and-endpoints")
            else:
                topic_parts = set(topic_lower.split("-")) - {"and", "or", "the", "a", "an"}
                overlap = topic_parts & text_words
                if overlap and len(overlap) >= len(topic_parts) * 0.5:
                    score += 1.5 * (len(overlap) / max(len(topic_parts), 1))

            # Check keyword aliases
            for keyword, target_topic in self.TOPIC_KEYWORD_ALIASES.items():
                if target_topic != topic:
                    continue
                # Multi-word alias: substring match
                if " " in keyword or "-" in keyword:
                    if keyword in text_lower:
                        score += 1.0
                else:
                    # Single-word alias: word-boundary match
                    if keyword in text_words:
                        score += 1.0

            if score > 0:
                topic_scores[topic] = score

        # Sort by score descending, then alphabetically for stability
        sorted_topics = sorted(
            topic_scores.keys(),
            key=lambda t: (-topic_scores[t], t),
        )

        max_topics = self.config.topic_max_knowledge_files
        return sorted_topics[:max_topics]

    async def _load_topic_context(
        self,
        project_id: str,
        topics: list[str],
    ) -> str:
        """Load knowledge base content for the detected topics.

        Reads knowledge topic files and assembles them into a formatted
        string suitable for context injection (L2 tier).

        Parameters
        ----------
        project_id:
            Project whose knowledge base to read from.
        topics:
            Topic names to load (as returned by ``detect_topics``).

        Returns
        -------
        str
            Formatted markdown with each topic's content, or empty string
            if no topics have content on disk.
        """
        if not topics:
            return ""

        max_chars = self.config.topic_max_chars_per_file
        parts: list[str] = []

        for topic in topics:
            content = await self.read_knowledge_topic(project_id, topic)
            if not content or not content.strip():
                continue

            # Strip frontmatter if present
            stripped = content.strip()
            if stripped.startswith("---"):
                end = stripped.find("---", 3)
                if end != -1:
                    stripped = stripped[end + 3 :].strip()

            if not stripped:
                continue

            # Truncate to budget
            if len(stripped) > max_chars:
                stripped = stripped[:max_chars] + "\n\n[truncated]"

            parts.append(f"### {topic}\n{stripped}")

        return "\n\n".join(parts)

    def _parse_frontmatter_topic(self, content: str) -> str | None:
        """Extract the ``topic`` field from YAML frontmatter.

        Returns the topic string if found, ``None`` otherwise.
        Fast and safe — returns ``None`` on any parse error.
        """
        stripped = content.strip()
        if not stripped.startswith("---"):
            return None
        end = stripped.find("---", 3)
        if end == -1:
            return None
        yaml_text = stripped[3:end]
        try:
            import yaml as _yaml

            data = _yaml.safe_load(yaml_text)
            if isinstance(data, dict):
                topic = data.get("topic")
                if isinstance(topic, str) and topic.strip():
                    return topic.strip().lower()
        except Exception:
            pass
        return None

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from content, returning the body."""
        stripped = content.strip()
        if not stripped.startswith("---"):
            return stripped
        end = stripped.find("---", 3)
        if end == -1:
            return stripped
        return stripped[end + 3 :].strip()

    async def _load_topic_memories(
        self,
        project_id: str,
        topics: list[str],
    ) -> str:
        """Load memory files whose ``topic`` frontmatter matches detected topics.

        Scans notes and memory directories for ``.md`` files with YAML
        frontmatter containing a ``topic`` field that matches one of the
        given topics.  Returns formatted markdown within the configured
        ``topic_memory_budget_chars`` budget (~500 tokens).

        This implements the L2 "memories filtered by topic" tier from
        the memory-scoping spec §2, complementing the knowledge-file
        loading in ``_load_topic_context()``.

        Parameters
        ----------
        project_id:
            Project whose memory files to scan.
        topics:
            Detected topic names (lowercase) to match against.

        Returns
        -------
        str
            Formatted markdown of matching memories, or empty string
            if no matches found.
        """
        if not topics or not self.config.topic_memory_enabled:
            return ""

        topic_set = {t.lower() for t in topics}
        budget = self.config.topic_memory_budget_chars
        max_results = self.config.topic_memory_max_results

        # Collect candidate directories to scan
        scan_dirs: list[str] = []

        # Notes directory (vault/projects/{id}/notes/)
        notes_dir = self._notes_dir(project_id)
        if os.path.isdir(notes_dir):
            scan_dirs.append(notes_dir)

        # Memory staging directory (memory/{id}/staging/)
        staging_dir = os.path.join(self._project_memory_dir(project_id), "staging")
        if os.path.isdir(staging_dir):
            scan_dirs.append(staging_dir)

        # Memory root (memory/{id}/) — but only top-level .md files
        # (exclude profile.md, factsheet.md, and subdirectories which are
        # scanned separately or are knowledge files)
        memory_dir = self._project_memory_dir(project_id)

        # Gather matching files: (mtime, path, body) sorted by recency
        matches: list[tuple[float, str, str]] = []

        exclude_basenames = {"profile.md", "factsheet.md", ".needs_reindex"}

        for scan_dir in scan_dirs:
            if not os.path.isdir(scan_dir):
                continue
            try:
                for fname in os.listdir(scan_dir):
                    if not fname.endswith(".md") or fname in exclude_basenames:
                        continue
                    fpath = os.path.join(scan_dir, fname)
                    if not os.path.isfile(fpath):
                        continue
                    try:
                        with open(fpath) as f:
                            content = f.read()
                        file_topic = self._parse_frontmatter_topic(content)
                        if file_topic and file_topic in topic_set:
                            body = self._strip_frontmatter(content)
                            if body:
                                mtime = os.path.getmtime(fpath)
                                matches.append((mtime, fpath, body))
                    except Exception:
                        continue
            except Exception:
                continue

        # Also scan top-level memory dir .md files (non-recursive)
        if os.path.isdir(memory_dir):
            try:
                for fname in os.listdir(memory_dir):
                    if not fname.endswith(".md") or fname in exclude_basenames:
                        continue
                    fpath = os.path.join(memory_dir, fname)
                    if not os.path.isfile(fpath):
                        continue
                    # Skip if already scanned from another dir
                    if any(fpath == m[1] for m in matches):
                        continue
                    try:
                        with open(fpath) as f:
                            content = f.read()
                        file_topic = self._parse_frontmatter_topic(content)
                        if file_topic and file_topic in topic_set:
                            body = self._strip_frontmatter(content)
                            if body:
                                mtime = os.path.getmtime(fpath)
                                matches.append((mtime, fpath, body))
                    except Exception:
                        continue
            except Exception:
                pass

        if not matches:
            return ""

        # Sort by recency (newest first)
        matches.sort(key=lambda m: m[0], reverse=True)

        # Assemble within budget
        parts: list[str] = []
        chars_used = 0

        for _mtime, fpath, body in matches[:max_results]:
            # Truncate individual entry if needed
            remaining = budget - chars_used
            if remaining <= 0:
                break

            entry_body = body
            if len(entry_body) > remaining:
                entry_body = entry_body[:remaining] + "\n[truncated]"

            basename = os.path.basename(fpath)
            entry = f"- **{basename}:** {entry_body}"
            parts.append(entry)
            chars_used += len(entry_body)

            if chars_used >= budget:
                break

        return "\n\n".join(parts)

    async def build_context(
        self,
        project_id: str,
        task: Any,
        workspace_path: str,
        *,
        project_name: str = "",
        repo_url: str = "",
    ) -> MemoryContext:
        """Build a structured, tiered memory context for a task.

        Returns a ``MemoryContext`` with fields for each priority tier:
        0. Project factsheet (structured metadata — always included, instant lookup)
        1. Project profile (always included)
        1.5. Project documentation (CLAUDE.md — foundational context)
        2. Topic context (L2 — knowledge files pre-loaded based on detected topics)
        3. Relevant notes (semantic search matched)
        4. Recent task memories (for continuity)
        5. Semantic search results (de-duplicated against above)

        The orchestrator uses this instead of the old flat recall approach.
        """
        ctx = MemoryContext()

        # Set the memory folder path so agents know where to find more context
        memory_dir = self._project_memory_dir(project_id)
        if os.path.isdir(memory_dir):
            ctx.memory_folder = memory_dir if memory_dir.endswith("/") else memory_dir + "/"

        # Set the tasks folder path (task records live outside the memory tree)
        tasks_dir = self._tasks_dir(project_id)
        if os.path.isdir(tasks_dir):
            ctx.tasks_folder = tasks_dir if tasks_dir.endswith("/") else tasks_dir + "/"

        # Tier 0: Project Factsheet (structured metadata, always included)
        if self.config.factsheet_in_context:
            try:
                factsheet_raw = await self.read_factsheet_raw(project_id)
                if factsheet_raw:
                    ctx.factsheet = factsheet_raw
            except Exception as e:
                logger.warning(f"Factsheet load failed for project {project_id}: {e}")

        # Tier 1: Project Profile
        if self.config.profile_enabled:
            profile = await self.get_profile(project_id)
            if profile:
                ctx.profile = profile

        # Tier 1.5: Project Documentation (CLAUDE.md, README.md)
        # Always included as foundational context so every agent knows the
        # project basics — conventions, architecture overview, and workflow.
        if self.config.index_project_docs and workspace_path:
            doc_parts: list[str] = []
            for rel_path in self.config.project_docs_files:
                full_path = os.path.join(workspace_path, rel_path)
                if os.path.isfile(full_path):
                    try:
                        with open(full_path) as f:
                            content = f.read().strip()
                        # Truncate to keep context budget reasonable
                        max_chars = 3000
                        if len(content) > max_chars:
                            content = content[:max_chars] + "\n\n[truncated]"
                        doc_parts.append(f"### {rel_path}\n{content}")
                    except Exception:
                        pass
            if doc_parts:
                ctx.project_docs = "\n\n".join(doc_parts)

        # Tier 2: Topic Context (L2 — spec §2/§3)
        # Detect topics from task description, then:
        #   a) Load matching knowledge base files (§3)
        #   b) Load memories filtered by topic frontmatter (§2)
        # This is fast (keyword matching, file reads) and runs before any
        # semantic search to give the agent focused domain knowledge.
        if self.config.topic_detection_enabled:
            try:
                task_text = f"{task.title} {task.description}"
                detected = await self.detect_topics(project_id, task_text)
                if detected:
                    # L2a: Knowledge base files
                    if self.config.index_knowledge:
                        topic_content = await self._load_topic_context(project_id, detected)
                        if topic_content:
                            ctx.topic_context = topic_content

                    # L2b: Memories filtered by topic frontmatter
                    if self.config.topic_memory_enabled:
                        topic_memories = await self._load_topic_memories(project_id, detected)
                        if topic_memories:
                            ctx.topic_memories = topic_memories

                    if ctx.topic_context or ctx.topic_memories:
                        ctx.detected_topics = detected
                        logger.debug(
                            "L2 topic context loaded for task %s: topics=%s "
                            "knowledge=%s memories=%s",
                            getattr(task, "id", "?"),
                            detected,
                            bool(ctx.topic_context),
                            bool(ctx.topic_memories),
                        )
            except Exception as e:
                logger.warning("L2 topic detection failed for project %s: %s", project_id, e)

        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return ctx

        query = f"{task.title} {task.description}"
        seen_sources: set[str] = set()

        # Tier 3: Relevant Notes (search notes directory specifically)
        if self.config.index_notes:
            try:
                notes_results = await instance.search(query, top_k=3)
                notes_lines = []
                if notes_results:
                    notes_dir = self._notes_dir(project_id)
                    for mem in notes_results:
                        source = mem.get("source", "")
                        # Only include results from the notes directory
                        if notes_dir and source.startswith(notes_dir):
                            seen_sources.add(source)
                            heading = mem.get("heading", "")
                            content = mem.get("content", "")
                            score = mem.get("score", 0)
                            entry = f"**{os.path.basename(source)}**"
                            if heading:
                                entry += f" — {heading}"
                            entry += f" (relevance: {score:.2f})\n{content}"
                            notes_lines.append(entry)
                if notes_lines:
                    ctx.notes = "\n\n".join(notes_lines)
            except Exception as e:
                logger.warning(f"Notes search failed for project {project_id}: {e}")

        # Tier 4: Recent Task Memories
        recent_count = self.config.context_include_recent
        if recent_count > 0:
            try:
                tasks_dir = self._tasks_dir(project_id)
                if os.path.isdir(tasks_dir):
                    task_files = sorted(
                        glob.glob(os.path.join(tasks_dir, "*.md")),
                        key=os.path.getmtime,
                        reverse=True,
                    )[:recent_count]

                    recent_lines = []
                    for tf in task_files:
                        seen_sources.add(tf)
                        try:
                            with open(tf) as f:
                                content = f.read().strip()
                            # Only include the first ~500 chars for recent tasks
                            if len(content) > 500:
                                content = content[:500] + "..."
                            recent_lines.append(content)
                        except Exception:
                            pass
                    if recent_lines:
                        ctx.recent_tasks = "\n\n---\n\n".join(recent_lines)
            except Exception as e:
                logger.warning(f"Recent tasks read failed for project {project_id}: {e}")

        # Tier 5: Semantic Search Results (de-duplicated)
        try:
            k = self.config.recall_top_k
            results = await instance.search(query, top_k=k + len(seen_sources))
            if results:
                search_lines = []
                for mem in results:
                    source = mem.get("source", "")
                    # Skip profile and already-seen sources
                    if source in seen_sources:
                        continue
                    if source == self._profile_path(project_id):
                        continue
                    seen_sources.add(source)

                    heading = mem.get("heading", "")
                    content = mem.get("content", "")
                    score = mem.get("score", 0)
                    entry = f"*Source: {source}*"
                    if heading:
                        entry += f"\n*Section: {heading}*"
                    entry += f" (relevance: {score:.2f})\n{content}"
                    search_lines.append(entry)

                    if len(search_lines) >= k:
                        break

                if search_lines:
                    ctx.search_results = "\n\n".join(search_lines)
        except Exception as e:
            logger.warning(f"Semantic search failed for project {project_id}: {e}")

        return ctx

    # ------------------------------------------------------------------
    # Original Public API (preserved for compatibility)
    # ------------------------------------------------------------------

    async def recall(self, task: Any, workspace_path: str, top_k: int | None = None) -> list[dict]:
        """Search for memories relevant to *task*.

        Uses ``task.title + task.description`` as the hybrid search query so
        both semantic similarity and keyword overlap contribute to ranking.

        Returns an empty list on any error — memory recall failures must never
        block task execution.
        """
        if not self.config.auto_recall:
            return []

        instance = await self.get_instance(task.project_id, workspace_path)
        if not instance:
            return []

        k = top_k or self.config.recall_top_k
        query = f"{task.title} {task.description}"
        try:
            results = await instance.search(query, top_k=k)
            return results if results else []
        except Exception as e:
            logger.warning(f"Memory recall failed for task {task.id}: {e}")
            return []

    async def remember(self, task: Any, output: Any, workspace_path: str) -> str | None:
        """Save a task result as a structured markdown memory file.

        Writes the file to ``{data_dir}/tasks/{project_id}/{task_id}.md``
        and indexes it via ``memsearch.index_file()``. Returns the file path
        on success, ``None`` otherwise.
        """
        if not self.config.auto_remember:
            return None

        instance = await self.get_instance(task.project_id, workspace_path)
        if not instance:
            return None

        memory_path = os.path.join(self._tasks_dir(task.project_id), f"{task.id}.md")
        try:
            content = self._format_task_memory(task, output)
            os.makedirs(os.path.dirname(memory_path), exist_ok=True)
            with open(memory_path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.warning(f"Failed to write memory file for task {task.id}: {e}")
            return None

        # Index the new file (non-fatal)
        try:
            await instance.index_file(memory_path)
        except Exception as e:
            logger.warning(f"Memory indexing failed for task {task.id}: {e}")

        return memory_path

    async def write_memory(
        self, project_id: str, workspace_path: str, key: str, content: str
    ) -> str | None:
        """Write an arbitrary key-value memory entry for a project.

        Stores the content as a markdown file at
        ``{data_dir}/memory/{project_id}/{key}.md`` and indexes it for
        semantic search. This is the agent-facing write API — use it for
        persistent state like timestamps, counters, or any structured data
        that should be retrievable via ``memory_search`` or ``read_memory``.

        Returns the file path on success.  Raises ``OSError`` if the
        write fails (callers should catch and surface to the user).
        """
        memory_dir = self._project_memory_dir(project_id)
        # Sanitize key to be filesystem-safe
        safe_key = key.replace("/", "_").replace("\\", "_").replace("..", "_")
        if not safe_key.endswith(".md"):
            safe_key += ".md"
        memory_path = os.path.join(memory_dir, safe_key)

        try:
            os.makedirs(memory_dir, exist_ok=True)
            with open(memory_path, "w") as f:
                f.write(content)
        except Exception as e:
            logger.warning(f"Failed to write memory file {safe_key}: {e}")
            raise OSError(f"Failed to write memory file '{safe_key}' to {memory_dir}: {e}") from e

        # Index for semantic search (non-fatal)
        instance = await self.get_instance(project_id, workspace_path)
        if instance:
            try:
                await instance.index_file(memory_path)
            except Exception as e:
                logger.warning(f"Memory indexing failed for {safe_key}: {e}")

        return memory_path

    async def read_memory(self, project_id: str, key: str) -> str | None:
        """Read an arbitrary key-value memory entry for a project.

        Returns the file content, or ``None`` if the key doesn't exist.
        """
        memory_dir = self._project_memory_dir(project_id)
        safe_key = key.replace("/", "_").replace("\\", "_").replace("..", "_")
        if not safe_key.endswith(".md"):
            safe_key += ".md"
        memory_path = os.path.join(memory_dir, safe_key)

        if not os.path.isfile(memory_path):
            return None

        try:
            with open(memory_path, "r") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"Failed to read memory file {safe_key}: {e}")
            return None

    async def search(
        self, project_id: str, workspace_path: str, query: str, top_k: int = 10
    ) -> list[dict]:
        """Ad-hoc semantic search across a project's memory.

        Exposed for command-handler and hook-engine usage.
        """
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return []

        try:
            results = await instance.search(query, top_k=top_k)
            return results if results else []
        except Exception as e:
            logger.warning(f"Memory search failed for project {project_id}: {e}")
            return []

    async def batch_search(
        self,
        project_id: str,
        workspace_path: str,
        queries: list[str],
        top_k: int = 10,
    ) -> dict[str, list[dict]]:
        """Run multiple semantic searches concurrently.

        Returns a dict mapping each query string to its results list.
        Individual query failures return empty lists without blocking others.
        """
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return {q: [] for q in queries}

        async def _single(q: str) -> tuple[str, list[dict]]:
            try:
                results = await instance.search(q, top_k=top_k)
                return (q, results if results else [])
            except Exception as e:
                logger.warning("Memory batch_search query %r failed: %s", q, e)
                return (q, [])

        pairs = await asyncio.gather(*[_single(q) for q in queries])
        return dict(pairs)

    # ------------------------------------------------------------------
    # Multi-scope weighted search (spec §6)
    # ------------------------------------------------------------------

    async def scoped_search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        agent_type: str | None = None,
        topic: str | None = None,
        top_k: int = 10,
        weights: dict | None = None,
        full: bool = False,
    ) -> list[dict]:
        """Semantic search with weighted merge across scopes.

        Implements spec §6 (Multi-Scope Query): searches project,
        agent-type, and system collections in parallel and merges results
        weighted by scope specificity (project=1.0, agent-type=0.7,
        system=0.4).

        A moderately relevant project-specific memory outranks a highly
        relevant system memory because the scope weight amplifies raw
        similarity scores.

        Parameters
        ----------
        query:
            Natural-language search query.
        project_id:
            When set, includes the project collection (weight 1.0).
        agent_type:
            When set, includes the agent-type collection (weight 0.7).
        topic:
            Optional topic pre-filter.  Falls back to unfiltered search
            when fewer than 3 results match the topic.
        top_k:
            Maximum results after merging.
        weights:
            Override default scope weights.  Keys are
            :class:`memsearch.MemoryScope` enum values.
        full:
            When ``True``, include ``original`` field in results.

        Returns
        -------
        list[dict]
            Results sorted by ``weighted_score`` descending.  Each result
            includes ``_scope``, ``_scope_id``, ``_weight``, and
            ``weighted_score`` annotations.
        """
        router = await self._get_router()
        embedder = await self._get_embedder()
        if router is None or embedder is None:
            return []

        try:
            embeddings = await embedder.embed([query])
            query_embedding = embeddings[0]
        except Exception as e:
            logger.warning("Failed to embed query for scoped_search: %s", e)
            return []

        try:
            results = await router.search(
                query_embedding,
                query_text=query,
                project_id=project_id,
                agent_type=agent_type,
                topic=topic,
                top_k=top_k,
                weights=weights,
                full=full,
            )
            return results if results else []
        except Exception as e:
            logger.warning(
                "Scoped search failed (project=%s, agent_type=%s): %s",
                project_id,
                agent_type,
                e,
            )
            return []

    async def scoped_batch_search(
        self,
        queries: list[str],
        *,
        project_id: str | None = None,
        agent_type: str | None = None,
        topic: str | None = None,
        top_k: int = 10,
        weights: dict | None = None,
        full: bool = False,
    ) -> dict[str, list[dict]]:
        """Run multiple multi-scope searches concurrently.

        Like :meth:`scoped_search` but accepts a list of queries.
        Embeddings are computed in a single batch for efficiency, then
        each query is searched in parallel across all scopes.

        Returns a dict mapping each query string to its merged results.
        """
        router = await self._get_router()
        embedder = await self._get_embedder()
        if router is None or embedder is None:
            return {q: [] for q in queries}

        if not queries:
            return {}

        # Batch-embed all queries at once for efficiency
        try:
            all_embeddings = await embedder.embed(queries)
        except Exception as e:
            logger.warning("Failed to batch-embed queries for scoped_batch_search: %s", e)
            return {q: [] for q in queries}

        async def _single(q: str, emb: list[float]) -> tuple[str, list[dict]]:
            try:
                results = await router.search(
                    emb,
                    query_text=q,
                    project_id=project_id,
                    agent_type=agent_type,
                    topic=topic,
                    top_k=top_k,
                    weights=weights,
                    full=full,
                )
                return (q, results if results else [])
            except Exception as e:
                logger.warning("Scoped batch_search query %r failed: %s", q, e)
                return (q, [])

        pairs = await asyncio.gather(*[_single(q, emb) for q, emb in zip(queries, all_embeddings)])
        return dict(pairs)

    async def reindex(self, project_id: str, workspace_path: str) -> int:
        """Force a full reindex of a project's memory.

        Returns the number of chunks indexed, or 0 on failure.
        """
        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return 0

        try:
            result = await instance.index(force=True)
            return result if isinstance(result, int) else 0
        except Exception as e:
            logger.warning(f"Memory reindex failed for project {project_id}: {e}")
            return 0

    async def stats(self, project_id: str, workspace_path: str) -> dict:
        """Get memory statistics for a project.

        Returns a dict with at least ``enabled`` and ``available`` keys.
        Includes age-tier breakdown and digest count when task files exist.
        """
        if not self.config.enabled:
            return {"enabled": False, "available": MEMSEARCH_AVAILABLE}

        instance = await self.get_instance(project_id, workspace_path)
        if not instance:
            return {
                "enabled": True,
                "available": MEMSEARCH_AVAILABLE,
                "error": "Failed to initialize MemSearch instance",
            }

        profile_exists = os.path.isfile(self._profile_path(project_id))
        last_compact = self._last_compact.get(project_id)

        # Age-tier breakdown of task memory files
        tasks_dir = self._tasks_dir(project_id)
        digests_dir = os.path.join(self._project_memory_dir(project_id), "digests")

        task_count = 0
        recent_count = 0
        medium_count = 0
        old_count = 0

        if os.path.isdir(tasks_dir):
            now = time.time()
            recent_cutoff = now - (self.config.compact_recent_days * 86400)
            archive_cutoff = now - (self.config.compact_archive_days * 86400)

            for tf in glob.glob(os.path.join(tasks_dir, "*.md")):
                task_count += 1
                try:
                    mtime = os.path.getmtime(tf)
                except OSError:
                    continue
                if mtime >= recent_cutoff:
                    recent_count += 1
                elif mtime >= archive_cutoff:
                    medium_count += 1
                else:
                    old_count += 1

        digest_count = 0
        if os.path.isdir(digests_dir):
            digest_count = len(glob.glob(os.path.join(digests_dir, "*.md")))

        return {
            "enabled": True,
            "available": True,
            "collection": self._collection_name(project_id),
            "milvus_uri": self.config.milvus_uri,
            "embedding_provider": self.config.embedding_provider,
            "auto_recall": self.config.auto_recall,
            "auto_remember": self.config.auto_remember,
            "recall_top_k": self.config.recall_top_k,
            "profile_enabled": self.config.profile_enabled,
            "profile_exists": profile_exists,
            "revision_enabled": self.config.revision_enabled,
            "auto_generate_notes": self.config.auto_generate_notes,
            "compact_enabled": self.config.compact_enabled,
            "last_compact": last_compact,
            "task_memories": task_count,
            "task_memories_recent": recent_count,
            "task_memories_medium": medium_count,
            "task_memories_old": old_count,
            "digests": digest_count,
        }

    async def close(self) -> None:
        """Shutdown all MemSearch instances, file watchers, and router."""
        for watcher in self._watchers.values():
            try:
                watcher.stop()
            except Exception as e:
                logger.debug(f"Error stopping memory watcher: {e}")
        for project_id, instance in self._instances.items():
            try:
                instance.close()
            except Exception as e:
                logger.debug(f"Error closing MemSearch for project {project_id}: {e}")
        if self._router is not None:
            try:
                self._router.close()
            except Exception as e:
                logger.debug(f"Error closing CollectionRouter: {e}")
            self._router = None
        self._embedder = None
        self._instances.clear()
        self._watchers.clear()

        # Clear the module-level override indexer so the watcher handler
        # reverts to its log-only fallback after shutdown.
        if self._override_indexer is not None:
            try:
                from src.override_handler import set_indexer

                set_indexer(None)
            except Exception as e:
                logger.debug("Error clearing override indexer: %s", e)
            self._override_indexer = None

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def _format_task_memory(self, task: Any, output: Any) -> str:
        """Format a task result as a structured markdown memory file.

        The format follows the template from the design doc: a heading with
        the task ID and title, metadata block, summary, and files-changed
        section. This structure is optimised for memsearch's heading-based
        chunker.
        """
        date_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime())

        # Safely extract enum values
        status = output.result.value if hasattr(output.result, "value") else str(output.result)
        task_type = (
            task.task_type.value
            if (task.task_type and hasattr(task.task_type, "value"))
            else "unknown"
        )
        tokens = output.tokens_used if output.tokens_used else 0

        files_section = "No files changed."
        if output.files_changed:
            files_section = "\n".join(f"- {f}" for f in output.files_changed)

        summary = output.summary or "No summary available."

        return (
            f"# Task: {task.id} — {task.title}\n"
            f"\n"
            f"**Project:** {task.project_id} | **Type:** {task_type} | **Status:** {status}\n"
            f"**Date:** {date_str} | **Tokens:** {tokens:,}\n"
            f"\n"
            f"## Summary\n"
            f"{summary}\n"
            f"\n"
            f"## Files Changed\n"
            f"{files_section}\n"
        )
