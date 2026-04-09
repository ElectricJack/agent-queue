"""MemoryV2Service — async service layer wrapping the memsearch fork.

Provides the backend for :class:`MemoryV2Plugin` and the ``memory_v2``
service protocol accessible via ``ctx.get_service("memory_v2")``.

All v2 memory operations flow through this service:

- **Semantic search** — multi-scope weighted search via
  :class:`CollectionRouter` with automatic embedding.
- **KV operations** — exact scalar lookups via :meth:`MilvusStore.set_kv`
  / :meth:`MilvusStore.get_kv`.
- **Temporal facts** — validity-windowed facts via
  :meth:`MilvusStore.set_temporal` / :meth:`MilvusStore.get_temporal`.
- **Cross-scope tag search** — queries all ``aq_*`` collections for
  entries with a specific tag.
- **Stats** — collection-level statistics (entry counts by type, model
  info, reindex status).

The service wraps:

- :class:`memsearch.CollectionRouter` for scope-aware collection
  management and multi-scope search.
- :class:`memsearch.embeddings.EmbeddingProvider` for computing query
  embeddings (semantic search only).
- :class:`memsearch.store.MilvusStore` for per-scope KV/temporal/scalar
  operations.

See ``docs/specs/design/memory-plugin.md`` §3 for the full architecture.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.facts_parser import extract_preamble, parse_facts_file, render_facts_file

logger = logging.getLogger(__name__)

try:
    from memsearch import (
        CollectionRouter,
        MemoryScope,
        collection_name,
        resolve_scopes,
        vault_paths,
    )
    from memsearch.embeddings import EmbeddingProvider, get_provider
    from memsearch.store import MilvusStore

    MEMSEARCH_AVAILABLE = True
except ImportError:
    MEMSEARCH_AVAILABLE = False
    CollectionRouter = None  # type: ignore[assignment,misc]
    MemoryScope = None  # type: ignore[assignment,misc]
    collection_name = None  # type: ignore[assignment]
    resolve_scopes = None  # type: ignore[assignment]
    vault_paths = None  # type: ignore[assignment]
    EmbeddingProvider = None  # type: ignore[assignment,misc]
    get_provider = None  # type: ignore[assignment]
    MilvusStore = None  # type: ignore[assignment,misc]


class MemoryV2Service:
    """Async service layer for v2 memory operations via memsearch/Milvus.

    Initialized by :class:`MemoryV2Plugin` during plugin startup.  Other
    subsystems access these operations through the plugin's tool interface
    or via ``ctx.get_service("memory_v2")``.

    Parameters
    ----------
    milvus_uri:
        Milvus connection URI.  A local ``*.db`` path uses Milvus Lite;
        ``http://host:port`` connects to a Milvus server.
    milvus_token:
        Auth token for remote Milvus server.
    embedding_provider:
        Embedding provider name (``"openai"``, ``"onnx"``, etc.).
    embedding_model:
        Override the default model for the provider.
    embedding_base_url:
        Override the API base URL (OpenAI-compatible endpoints).
    embedding_api_key:
        Override the API key for the embedding provider.
    data_dir:
        Application data directory (``~/.agent-queue``).  Used for vault
        path resolution.
    """

    def __init__(
        self,
        *,
        milvus_uri: str = "~/.agent-queue/memsearch/milvus.db",
        milvus_token: str = "",
        embedding_provider: str = "openai",
        embedding_model: str = "",
        embedding_base_url: str = "",
        embedding_api_key: str = "",
        data_dir: str = "",
    ) -> None:
        self._milvus_uri = milvus_uri
        self._milvus_token = milvus_token or None
        self._embedding_provider_name = embedding_provider
        self._embedding_model = embedding_model or None
        self._embedding_base_url = embedding_base_url or None
        self._embedding_api_key = embedding_api_key or None
        self._data_dir = data_dir

        self._embedder: Any = None  # EmbeddingProvider
        self._router: Any = None  # CollectionRouter
        self._initialized = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether the memsearch backend is available and initialized."""
        return self._initialized and self._router is not None

    @property
    def router(self) -> Any:
        """The :class:`CollectionRouter` instance, or ``None``."""
        return self._router

    @property
    def embedder(self) -> Any:
        """The :class:`EmbeddingProvider` instance, or ``None``."""
        return self._embedder

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Create the embedding provider and :class:`CollectionRouter`.

        Safe to call repeatedly — subsequent calls are no-ops.
        """
        if self._initialized:
            return

        if not MEMSEARCH_AVAILABLE:
            logger.warning("memsearch package not available — MemoryV2Service disabled")
            return

        try:
            self._embedder = get_provider(
                self._embedding_provider_name,
                model=self._embedding_model,
                base_url=self._embedding_base_url,
                api_key=self._embedding_api_key,
            )

            self._router = CollectionRouter(
                milvus_uri=self._milvus_uri,
                token=self._milvus_token,
                dimension=self._embedder.dimension,
            )

            self._initialized = True
            logger.info(
                "MemoryV2Service initialized (embedding=%s/%s, dim=%d, milvus=%s)",
                self._embedding_provider_name,
                self._embedder.model_name,
                self._embedder.dimension,
                self._milvus_uri,
            )
        except Exception:
            logger.error(
                "MemoryV2Service initialization failed",
                exc_info=True,
            )
            self._initialized = False

    async def shutdown(self) -> None:
        """Close all connections and release resources."""
        if self._router:
            self._router.close()
            self._router = None
        self._embedder = None
        self._initialized = False
        logger.info("MemoryV2Service shut down")

    # ------------------------------------------------------------------
    # Scope resolution
    # ------------------------------------------------------------------

    def _resolve_scope(
        self,
        project_id: str,
        scope: str | None = None,
    ) -> tuple[Any, str | None]:
        """Resolve a scope string to ``(MemoryScope, scope_id)``.

        When *scope* is ``None``, defaults to the project scope.

        Parameters
        ----------
        project_id:
            Fallback project identifier.
        scope:
            Optional scope string: ``"system"``, ``"orchestrator"``,
            ``"agenttype_{type}"``, ``"project_{id}"``, or ``None``.

        Returns
        -------
        tuple[MemoryScope, str | None]
        """
        if scope is None:
            return (MemoryScope.PROJECT, project_id)
        if scope == "system":
            return (MemoryScope.SYSTEM, None)
        if scope == "orchestrator":
            return (MemoryScope.ORCHESTRATOR, None)
        if scope.startswith("agenttype_"):
            agent_type = scope.removeprefix("agenttype_")
            return (MemoryScope.AGENT_TYPE, agent_type)
        if scope.startswith("project_"):
            pid = scope.removeprefix("project_")
            return (MemoryScope.PROJECT, pid)
        # Default to project scope
        return (MemoryScope.PROJECT, project_id)

    def _get_store(
        self,
        project_id: str,
        scope: str | None = None,
    ) -> Any:
        """Get the :class:`MilvusStore` for the given scope.

        Creates the collection lazily if it doesn't exist.
        """
        mem_scope, scope_id = self._resolve_scope(project_id, scope)
        return self._router.get_store(mem_scope, scope_id)

    # ------------------------------------------------------------------
    # Semantic Search
    # ------------------------------------------------------------------

    async def search(
        self,
        project_id: str,
        query: str,
        *,
        scope: str | None = None,
        topic: str | None = None,
        top_k: int = 10,
        full: bool = False,
    ) -> list[dict[str, Any]]:
        """Semantic search across scoped collection(s).

        When *scope* is ``None`` (default), performs multi-scope weighted
        search across the project collection and the system collection
        via :meth:`CollectionRouter.search`.

        When *scope* is explicitly set, searches only that single
        collection.

        Parameters
        ----------
        project_id:
            Project identifier (determines project scope).
        query:
            Natural-language search query.
        scope:
            Explicit scope.  ``None`` for multi-scope (project + system).
        topic:
            Optional topic pre-filter.
        top_k:
            Maximum results.
        full:
            When ``True``, include the ``original`` field in results.
            When ``False`` (default), only the summary ``content``
            is returned.  Per spec §9.
        """
        if not self.available:
            return []

        # Embed the query
        embeddings = await self._embedder.embed([query])
        query_embedding = embeddings[0]

        if scope is not None:
            # Explicit scope: search only that collection
            store = self._get_store(project_id, scope)
            from memsearch.store import _escape_filter_value

            filter_expr = ""
            if topic:
                escaped = _escape_filter_value(topic)
                filter_expr = f'(topic == "{escaped}" or topic == "")'

            results = await asyncio.to_thread(
                store.search,
                query_embedding,
                query_text=query,
                top_k=top_k,
                filter_expr=filter_expr,
                full=full,
            )
            # Annotate with scope info
            mem_scope, scope_id = self._resolve_scope(project_id, scope)
            coll_name = collection_name(mem_scope, scope_id)
            for r in results:
                r["_collection"] = coll_name
                r["_scope"] = mem_scope.value
                r["_scope_id"] = scope_id
            return results
        else:
            # Multi-scope search via router (project + system)
            return await self._router.search(
                query_embedding,
                query_text=query,
                project_id=project_id,
                topic=topic,
                top_k=top_k,
                full=full,
            )

    async def batch_search(
        self,
        project_id: str,
        queries: list[str],
        *,
        scope: str | None = None,
        topic: str | None = None,
        top_k: int = 10,
    ) -> dict[str, list[dict[str, Any]]]:
        """Run multiple search queries concurrently.

        Returns a dict mapping each query string to its results.
        """
        if not self.available:
            return {q: [] for q in queries}

        tasks = [self.search(project_id, q, scope=scope, topic=topic, top_k=top_k) for q in queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output: dict[str, list[dict[str, Any]]] = {}
        for i, q in enumerate(queries):
            r = results[i]
            output[q] = r if not isinstance(r, BaseException) else []
        return output

    async def search_by_tag(
        self,
        tag: str,
        *,
        entry_type: str | None = None,
        topic: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Cross-scope tag search across ALL ``aq_*`` collections.

        Per spec §7.3: queries all collections for entries matching a
        specific tag.  Use for cross-cutting discovery.
        """
        if not self.available:
            return []

        return await self._router.search_by_tag_async(
            tag,
            entry_type=entry_type,
            topic=topic,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # KV Operations
    # ------------------------------------------------------------------

    async def kv_get(
        self,
        project_id: str,
        namespace: str,
        key: str,
    ) -> dict[str, Any] | None:
        """Exact key-value lookup via scalar query.

        Pure scalar lookup — no vector computation.  Fast O(1) by
        deterministic chunk_hash.
        """
        if not self.available:
            return None

        store = self._get_store(project_id)
        return await asyncio.to_thread(store.get_kv, key, namespace=namespace)

    async def kv_set(
        self,
        project_id: str,
        namespace: str,
        key: str,
        value: str,
        *,
        scope: str | None = None,
        _from_vault: bool = False,
    ) -> dict[str, Any]:
        """Write a KV entry to the scoped collection and vault facts file.

        Creates or updates the entry in the Milvus collection for the
        resolved scope.  Also syncs the key-value pair to the vault
        ``facts.md`` file for human-readable access and L1 tier injection.

        Per spec §7, writes go to the most specific scope by default
        (project when *project_id* is set).  An explicit *scope* overrides.

        Parameters
        ----------
        project_id:
            Project identifier (determines default scope collection).
        namespace:
            KV namespace (e.g. ``"project"``, ``"conventions"``).
        key:
            The key to set.
        value:
            The value to store.
        scope:
            Explicit scope override.  One of ``"system"``,
            ``"orchestrator"``, ``"agenttype_{type}"``, or
            ``"project_{id}"``.  Defaults to the project scope.
        _from_vault:
            When ``True``, skip writing back to the vault ``facts.md``
            file.  Used by the facts-file watcher handler to avoid
            circular sync (file change -> parse -> kv_set -> file write
            -> file change ...).

        Returns
        -------
        dict
            The stored entry with ``vault_path`` indicating the synced
            facts file.
        """
        if not self.available:
            raise RuntimeError("MemoryV2Service not available")

        store = self._get_store(project_id, scope)
        entry = await asyncio.to_thread(
            store.set_kv,
            key,
            value,
            namespace=namespace,
            content=f"{namespace}/{key}: {value}",
        )

        # Sync to vault facts.md file (skip when the write originated
        # from a vault file parse to prevent circular sync loops).
        mem_scope, scope_id = self._resolve_scope(project_id, scope)
        facts_path = self._vault_facts_path(mem_scope, scope_id)
        if not _from_vault:
            await asyncio.to_thread(self._sync_facts_file, facts_path, namespace, key, value)
        entry["_vault_path"] = str(facts_path)
        entry["_scope"] = mem_scope.value
        entry["_scope_id"] = scope_id

        return entry

    # ------------------------------------------------------------------
    # Vault facts.md sync helpers
    # ------------------------------------------------------------------

    def _vault_facts_path(
        self,
        mem_scope: Any,
        scope_id: str | None,
    ) -> Path:
        """Return the absolute path to the ``facts.md`` file for a scope.

        Uses :data:`VAULT_PATHS` to locate the facts file.  The last
        entry in each scope's path list is the ``facts.md`` file.

        Parameters
        ----------
        mem_scope:
            The :class:`MemoryScope` enum value.
        scope_id:
            Scope-specific identifier (project id, agent-type name, etc.).
        """
        paths = vault_paths(mem_scope, scope_id)
        # The last entry in VAULT_PATHS for each scope is the facts.md file
        facts_rel = None
        for p in paths:
            if p.endswith("facts.md"):
                facts_rel = p
                break
        if not facts_rel:
            # Fallback: construct from scope
            facts_rel = f"vault/projects/{scope_id or 'unknown'}/facts.md"

        base = Path(self._data_dir).expanduser() if self._data_dir else Path.home() / ".agent-queue"
        return base / facts_rel

    @staticmethod
    def _parse_facts_file(text: str) -> dict[str, dict[str, str]]:
        """Parse a ``facts.md`` file into ``{namespace: {key: value}}``.

        Delegates to :func:`src.facts_parser.parse_facts_file` — the
        standalone parser that handles YAML frontmatter, bullet-prefixed
        lines, and the full spec format.

        Parameters
        ----------
        text:
            The raw content of the facts.md file.

        Returns
        -------
        dict[str, dict[str, str]]
            Mapping of namespace -> {key -> value}.
        """
        return parse_facts_file(text)

    @staticmethod
    def _render_facts_file(data: dict[str, dict[str, str]]) -> str:
        """Render a ``{namespace: {key: value}}`` dict to facts.md format.

        Delegates to :func:`src.facts_parser.render_facts_file`.

        Parameters
        ----------
        data:
            Mapping of namespace -> {key -> value}.

        Returns
        -------
        str
            Formatted markdown content for the facts.md file.
        """
        return render_facts_file(data)

    def _sync_facts_file(
        self,
        facts_path: Path,
        namespace: str,
        key: str,
        value: str,
    ) -> None:
        """Update or create the vault ``facts.md`` file with a KV entry.

        Reads the existing facts file (if any), merges the new key-value
        pair under the specified namespace heading, and writes the result
        back.  Creates parent directories as needed.

        Any **preamble** in the existing file (YAML frontmatter, ``# title``,
        introductory text before the first ``## heading``) is preserved.
        Only the structured KV sections are updated.

        Parameters
        ----------
        facts_path:
            Absolute path to the ``facts.md`` file.
        namespace:
            The namespace heading (e.g. ``"project"``).
        key:
            The key to set.
        value:
            The value to store.
        """
        # Read existing content
        existing = ""
        if facts_path.exists():
            try:
                existing = facts_path.read_text(encoding="utf-8")
            except OSError:
                logger.warning("Could not read facts file %s", facts_path)

        # Preserve any preamble (frontmatter, title, etc.) from the
        # existing file.  The preamble is everything before the first
        # ``## heading`` line and is not part of the KV data.
        preamble, _ = extract_preamble(existing)

        # Parse, merge, render
        data = self._parse_facts_file(existing)
        if namespace not in data:
            data[namespace] = {}
        data[namespace][key] = value

        rendered = self._render_facts_file(data)

        # Combine preamble with rendered structured sections
        if preamble:
            # Ensure preamble ends with a blank line separator before
            # the first ``## heading``.
            if not preamble.endswith("\n\n"):
                preamble = preamble.rstrip("\n") + "\n\n"
            output = preamble + rendered
        else:
            output = rendered

        # Write back
        facts_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            facts_path.write_text(output, encoding="utf-8")
            logger.debug(
                "Synced KV %s/%s to vault facts: %s",
                namespace,
                key,
                facts_path,
            )
        except OSError:
            logger.error(
                "Failed to write facts file %s",
                facts_path,
                exc_info=True,
            )

    async def kv_list(
        self,
        project_id: str,
        namespace: str,
    ) -> list[dict[str, Any]]:
        """List all KV entries in a namespace.

        Pure scalar query — no vector search.
        """
        if not self.available:
            return []

        store = self._get_store(project_id)
        return await asyncio.to_thread(store.list_kv, namespace=namespace)

    async def kv_recall(
        self,
        key: str,
        *,
        project_id: str | None = None,
        agent_type: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any] | None:
        """KV lookup with scope resolution.  First match wins (most specific).

        Per spec ``docs/specs/design/memory-scoping.md`` §6, scopes are
        searched in order of decreasing specificity:

        1. **project** — ``aq_project_{project_id}``
        2. **agent-type** — ``aq_agenttype_{agent_type}``
        3. **system** — ``aq_system``

        The first scope that contains a matching entry wins.  This means
        project-level overrides always take precedence over agent-type
        defaults, which in turn override system-wide values.

        Parameters
        ----------
        key:
            The key to look up.
        project_id:
            Project identifier.  When set, the project scope is searched
            first.
        agent_type:
            Agent type name (e.g. ``"coding"``).  When set, the
            agent-type scope is searched second.
        namespace:
            Optional KV namespace filter (e.g. ``"project"``,
            ``"conventions"``).

        Returns
        -------
        dict | None
            The first matching KV entry (annotated with ``_scope``,
            ``_scope_id``, and ``_collection``), or ``None`` if no scope
            contains the key.
        """
        if not self.available:
            return None

        # Use the scope resolver to build the ordered scope list,
        # consistent with CollectionRouter.recall() and .search().
        scope_entries = resolve_scopes(
            agent_type=agent_type,
            project_id=project_id,
        )

        ns = namespace or ""

        for entry in scope_entries:
            store = self._router.get_store(entry.scope, entry.scope_id)
            result = await asyncio.to_thread(store.get_kv, key, namespace=ns)
            if result is not None:
                result["_collection"] = entry.collection
                result["_scope"] = entry.scope.value
                result["_scope_id"] = entry.scope_id
                return result

        return None

    async def load_l1_facts(
        self,
        *,
        project_id: str | None = None,
        agent_type: str | None = None,
    ) -> str:
        """Load KV entries from project + agent-type scopes for L1 injection.

        Reads the vault ``facts.md`` files for the project and agent-type
        scopes, merges them with first-match-wins (project entries override
        agent-type entries for the same namespace/key), and renders a
        compact text block suitable for prompt injection.

        Per spec ``docs/specs/design/memory-scoping.md`` §2, the L1 tier
        is ~200 tokens of critical facts eagerly loaded at task start.
        No search is needed — just a file read.

        Parameters
        ----------
        project_id:
            Project identifier.  When set, the project's ``facts.md``
            is loaded first (highest priority).
        agent_type:
            Agent type name (e.g. ``"coding"``).  When set, the
            agent-type's ``facts.md`` is loaded second (lower priority).

        Returns
        -------
        str
            Formatted markdown block (``## Critical Facts`` heading with
            KV entries), or empty string if no facts are found.
        """
        from src.facts_parser import parse_facts_file

        # Collect facts from each scope.  Project entries take priority
        # over agent-type entries for the same namespace/key (first-match-wins).
        merged: dict[str, dict[str, str]] = {}  # namespace -> {key -> value}

        # Helper: read and parse a vault facts.md file
        def _read_facts(facts_path: Path) -> dict[str, dict[str, str]]:
            try:
                if facts_path.is_file():
                    text = facts_path.read_text(encoding="utf-8")
                    return parse_facts_file(text)
            except OSError:
                logger.debug("Could not read facts file: %s", facts_path)
            return {}

        # Agent-type scope (loaded first so project entries override below)
        if agent_type and MEMSEARCH_AVAILABLE:
            at_path = self._vault_facts_path(MemoryScope.AGENT_TYPE, agent_type)
            at_facts = await asyncio.to_thread(_read_facts, at_path)
            for ns, entries in at_facts.items():
                merged.setdefault(ns, {}).update(entries)

        # Project scope (loaded second — overwrites agent-type entries)
        if project_id and MEMSEARCH_AVAILABLE:
            proj_path = self._vault_facts_path(MemoryScope.PROJECT, project_id)
            proj_facts = await asyncio.to_thread(_read_facts, proj_path)
            for ns, entries in proj_facts.items():
                merged.setdefault(ns, {}).update(entries)

        if not merged:
            return ""

        # Render a compact markdown block.  Group entries by namespace
        # for readability, but keep the format dense to respect the
        # ~200 token budget.
        lines: list[str] = ["## Critical Facts"]
        for ns in sorted(merged.keys()):
            entries = merged[ns]
            if not entries:
                continue
            for key in sorted(entries.keys()):
                lines.append(f"- {key}: {entries[key]}")
        return "\n".join(lines)

    async def recall(
        self,
        query: str,
        *,
        project_id: str | None = None,
        agent_type: str | None = None,
        namespace: str | None = None,
        topic: str | None = None,
        top_k: int = 5,
        full: bool = False,
    ) -> dict[str, Any]:
        """Smart retrieval: KV exact match first, then semantic search.

        Per spec §7 (``memory_recall`` tool): agents use this when they
        are not sure whether the information is a structured fact or an
        unstructured insight.

        1. Try :meth:`kv_recall` with the *query* as the key.
        2. If a KV match is found, return it immediately.
        3. Otherwise fall back to :meth:`search` (multi-scope semantic).

        Parameters
        ----------
        query:
            The search query.  Used as the KV key for the exact-match
            attempt, and as the semantic search query for the fallback.
        project_id:
            Project identifier.
        agent_type:
            Agent type name for scope resolution.
        namespace:
            KV namespace for the exact-match attempt.
        topic:
            Topic pre-filter for semantic search fallback.
        top_k:
            Maximum semantic search results.
        full:
            When ``True``, include the ``original`` field in semantic
            search results (full content alongside the summary).
            Per spec §9: "``memory_get`` with ``full=true`` returns
            the original."

        Returns
        -------
        dict
            ``{"source": "kv"|"semantic"|"unavailable", "results": [...]}``
        """
        if not self.available:
            return {"source": "unavailable", "results": []}

        # Step 1: Try KV exact match with scope resolution
        kv_result = await self.kv_recall(
            query,
            project_id=project_id,
            agent_type=agent_type,
            namespace=namespace,
        )
        if kv_result is not None:
            return {"source": "kv", "results": [kv_result]}

        # Step 2: Fall back to semantic search
        results = await self.search(
            project_id or "",
            query,
            topic=topic,
            top_k=top_k,
            full=full,
        )
        return {"source": "semantic", "results": results}

    # ------------------------------------------------------------------
    # Temporal Facts
    # ------------------------------------------------------------------

    async def fact_get(
        self,
        project_id: str,
        key: str,
        *,
        as_of: int | None = None,
    ) -> dict[str, Any] | None:
        """Get the current (or as-of) value of a temporal fact.

        Returns the entry whose validity window covers the query time,
        or ``None`` if no matching entry exists.
        """
        if not self.available:
            return None

        store = self._get_store(project_id)
        results = await asyncio.to_thread(store.get_temporal, key, at=as_of)
        return results[0] if results else None

    async def fact_set(
        self,
        project_id: str,
        key: str,
        value: str,
    ) -> dict[str, Any]:
        """Set a temporal fact, closing the previous validity window.

        Per spec §6 (Temporal Fact Lifecycle):
        1. Current entry's ``valid_to`` is set to now
        2. New entry created with ``valid_from`` = now, ``valid_to`` = 0
        3. Both entries persist — history is preserved

        Returns
        -------
        dict
            The newly created entry.
        """
        if not self.available:
            raise RuntimeError("MemoryV2Service not available")

        store = self._get_store(project_id)
        return await asyncio.to_thread(
            store.set_temporal,
            key,
            value,
            content=f"fact/{key}: {value}",
        )

    async def fact_list(
        self,
        project_id: str,
        namespace: str = "",
        *,
        current_only: bool = True,
    ) -> list[dict[str, Any]]:
        """List all temporal fact entries in a namespace.

        Pure scalar query — no vector search.

        Parameters
        ----------
        project_id:
            Project whose collection to query.
        namespace:
            Namespace filter (exact match).  Empty string returns entries
            with no namespace.
        current_only:
            If ``True`` (default), only return currently-open entries
            (``valid_to == 0``).  If ``False``, return all entries
            including closed (superseded) ones.

        Returns
        -------
        list[dict]
            All matching temporal entries sorted by key then valid_from.
        """
        if not self.available:
            return []

        store = self._get_store(project_id)
        return await asyncio.to_thread(
            store.list_temporal, namespace=namespace, current_only=current_only
        )

    async def fact_history(
        self,
        project_id: str,
        key: str,
    ) -> list[dict[str, Any]]:
        """Retrieve the full history of a temporal fact.

        Returns all entries (open and closed) ordered by ``valid_from``
        ascending.  Useful for pattern detection.
        """
        if not self.available:
            return []

        store = self._get_store(project_id)
        return await asyncio.to_thread(store.get_temporal_history, key)

    # ------------------------------------------------------------------
    # Document Save (memory_save — spec §8)
    # ------------------------------------------------------------------

    def _vault_base_dir(
        self,
        project_id: str,
        scope: str | None = None,
    ) -> Path:
        """Return the vault memory directory for the given scope.

        Uses the first entry from :func:`vault_paths` (the ``memory/``
        directory) under the configured data dir.
        """
        mem_scope, scope_id = self._resolve_scope(project_id, scope)
        paths = vault_paths(mem_scope, scope_id)
        # First path is the memory/ directory for each scope
        rel = paths[0] if paths else f"vault/projects/{project_id}/memory/"
        base = Path(self._data_dir).expanduser() if self._data_dir else Path.home() / ".agent-queue"
        return base / rel

    @staticmethod
    def _slugify(text: str, max_len: int = 60) -> str:
        """Generate a filesystem-safe slug from text.

        Lowercases, replaces non-alphanum with hyphens, collapses runs,
        and truncates at word boundaries.
        """
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
        slug = re.sub(r"-+", "-", slug)
        if len(slug) > max_len:
            slug = slug[:max_len].rsplit("-", 1)[0]
        return slug or "insight"

    @staticmethod
    def _generate_chunk_hash(
        scope: str,
        content: str,
        topic: str | None = None,
    ) -> str:
        """Deterministic chunk hash for a document entry."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        parts = f"doc:{scope}:{topic or ''}:{content_hash}"
        return hashlib.sha256(parts.encode()).hexdigest()[:32]

    def _write_vault_file(
        self,
        vault_dir: Path,
        *,
        content: str,
        original: str | None = None,
        tags: list[str],
        topic: str | None = None,
        source_task: str | None = None,
        filename: str | None = None,
        created: str | None = None,
    ) -> Path:
        """Write a markdown file with frontmatter to the vault.

        Parameters
        ----------
        vault_dir:
            Target directory (e.g. ``vault/projects/{id}/memory/insights/``).
        content:
            Summary / main content.
        original:
            Full original content (included below summary if different).
        tags:
            Frontmatter tags.
        topic:
            Optional topic field.
        source_task:
            Optional task ID reference.
        filename:
            Override filename (without extension).  Auto-generated from
            content if not provided.
        created:
            ISO date string for ``created`` field.  Uses today if not set.

        Returns
        -------
        Path
            The path to the written file.
        """
        insights_dir = vault_dir / "insights"
        insights_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not filename:
            # Derive filename from first line or first ~60 chars
            first_line = content.split("\n", 1)[0].lstrip("# ").strip()
            slug = self._slugify(first_line or content[:60])
            # Add short hash to avoid collisions
            short_hash = hashlib.sha256(content.encode()).hexdigest()[:6]
            filename = f"{slug}-{short_hash}"

        filepath = insights_dir / f"{filename}.md"

        # Build frontmatter
        fm_lines = ["---"]
        import json as _json

        fm_lines.append(f"tags: {_json.dumps(tags)}")
        if topic:
            fm_lines.append(f"topic: {topic}")
        if source_task:
            fm_lines.append(f"source_task: {source_task}")
        fm_lines.append(f"created: {created or now}")
        fm_lines.append(f"updated: {now}")
        fm_lines.append("---")
        fm_lines.append("")

        # Build body
        body_parts = [content]
        if original and original != content:
            body_parts.append("\n\n## Original\n")
            body_parts.append(original)

        full = "\n".join(fm_lines) + "\n".join(body_parts) + "\n"
        filepath.write_text(full, encoding="utf-8")
        return filepath

    def _update_vault_file(
        self,
        filepath: Path,
        *,
        content: str | None = None,
        original: str | None = None,
        tags: list[str] | None = None,
        source_task: str | None = None,
    ) -> None:
        """Update an existing vault markdown file.

        Only modifies the ``updated`` timestamp and optionally appends a
        ``source_task`` to the frontmatter.  If *content* is provided the
        file body is replaced.  If *original* is also provided, the body
        includes both the summary content and the original text under an
        ``## Original`` heading (per spec §9).
        """
        import json as _json

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if not filepath.exists():
            return

        text = filepath.read_text(encoding="utf-8")

        # Update 'updated' timestamp in frontmatter
        text = re.sub(r"^updated:.*$", f"updated: {now}", text, count=1, flags=re.MULTILINE)

        # Add source_task if not already present
        if source_task and f"source_task: {source_task}" not in text:
            # Append source task as additional reference
            text = re.sub(
                r"^(updated:.*)$",
                rf"\1\nsource_tasks_additional: {source_task}",
                text,
                count=1,
                flags=re.MULTILINE,
            )

        # Merge tags if provided
        if tags:
            existing_tags_match = re.search(r"^tags:\s*(\[.*?\])$", text, flags=re.MULTILINE)
            if existing_tags_match:
                try:
                    existing = _json.loads(existing_tags_match.group(1))
                    merged = list(dict.fromkeys(existing + tags))
                    text = text.replace(
                        existing_tags_match.group(0),
                        f"tags: {_json.dumps(merged)}",
                    )
                except _json.JSONDecodeError:
                    pass

        # Replace body if new content provided
        if content:
            parts = text.split("---", 2)
            if len(parts) >= 3:
                body_parts = [content]
                if original and original != content:
                    body_parts.append("\n\n## Original\n")
                    body_parts.append(original)
                text = f"---{parts[1]}---\n\n" + "\n".join(body_parts) + "\n"

        filepath.write_text(text, encoding="utf-8")

    async def save_document(
        self,
        project_id: str,
        content: str,
        *,
        summary: str | None = None,
        original: str | None = None,
        tags: list[str] | None = None,
        topic: str | None = None,
        source_task: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Save a new document entry to vault + Milvus.

        Writes a markdown file to the vault and upserts the entry into
        the scoped Milvus collection.  Does NOT perform dedup checking
        — the caller (plugin layer) is responsible for checking
        similarity and deciding whether to create, merge, or deduplicate.

        Parameters
        ----------
        project_id:
            Project identifier.
        content:
            The content to save.  If *summary* is provided, this is
            used as ``original`` and *summary* as the indexed ``content``.
        summary:
            Optional summary (indexed for search).  If not provided,
            *content* is used as both summary and original.
        original:
            Explicit original text.  Overrides using *content* as original.
        tags:
            Tags for the entry.
        topic:
            Optional topic tag.
        source_task:
            Source task ID reference.
        scope:
            Memory scope (defaults to project scope).

        Returns
        -------
        dict
            Result with ``chunk_hash``, ``vault_path``, ``collection``, etc.
        """
        if not self.available:
            raise RuntimeError("MemoryV2Service not available")

        tags = tags or ["insight", "auto-generated"]
        indexed_content = summary or content
        stored_original = original or (content if summary else content)

        # Resolve scope and generate chunk hash
        mem_scope, scope_id = self._resolve_scope(project_id, scope)
        scope_str = f"{mem_scope.value}:{scope_id}" if scope_id else mem_scope.value
        chunk_hash = self._generate_chunk_hash(scope_str, stored_original, topic)
        coll_name = collection_name(mem_scope, scope_id)

        # Write vault file
        vault_dir = self._vault_base_dir(project_id, scope)
        vault_path = self._write_vault_file(
            vault_dir,
            content=indexed_content,
            original=stored_original if stored_original != indexed_content else None,
            tags=tags,
            topic=topic,
            source_task=source_task,
        )

        # Compute embedding for the summary/content
        embeddings = await self._embedder.embed([indexed_content])
        embedding = embeddings[0]

        # Build Milvus document entry
        import json as _json

        now_ts = int(time.time())
        chunk = {
            "chunk_hash": chunk_hash,
            "entry_type": "document",
            "embedding": embedding,
            "content": indexed_content,
            "original": stored_original,
            "source": str(vault_path),
            "heading": indexed_content.split("\n", 1)[0].lstrip("# ").strip()[:200],
            "heading_level": 1,
            "start_line": 0,
            "end_line": 0,
            "topic": topic or "",
            "tags": _json.dumps(tags),
            "updated_at": now_ts,
        }

        # Upsert into Milvus
        store = self._get_store(project_id, scope)
        await asyncio.to_thread(store.upsert, [chunk])

        return {
            "chunk_hash": chunk_hash,
            "vault_path": str(vault_path),
            "collection": coll_name,
            "scope": mem_scope.value,
            "scope_id": scope_id,
            "topic": topic or "",
            "tags": tags,
            "source_task": source_task or "",
            "updated_at": now_ts,
        }

    async def update_document_timestamp(
        self,
        project_id: str,
        chunk_hash: str,
        *,
        source_task: str | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Update the timestamp on an existing document (dedup case).

        Per spec §8: when similarity > 0.95, update the timestamp on the
        existing memory and append the source task reference.  No content
        change.

        Parameters
        ----------
        project_id:
            Project identifier.
        chunk_hash:
            The chunk_hash of the existing entry.
        source_task:
            Task ID to append as a reference.
        scope:
            Memory scope.

        Returns
        -------
        dict
            Updated entry info.
        """
        if not self.available:
            raise RuntimeError("MemoryV2Service not available")

        store = self._get_store(project_id, scope)
        entry = await asyncio.to_thread(store.get, chunk_hash)
        if not entry:
            raise ValueError(f"Entry not found: {chunk_hash}")

        now_ts = int(time.time())

        # Update the Milvus entry timestamp
        entry["updated_at"] = now_ts
        await asyncio.to_thread(store.upsert, [entry])

        # Update vault file if it exists
        source = entry.get("source", "")
        if source:
            vault_file = Path(source)
            if vault_file.exists():
                self._update_vault_file(vault_file, source_task=source_task)

        mem_scope, scope_id = self._resolve_scope(project_id, scope)
        coll_name = collection_name(mem_scope, scope_id)

        return {
            "chunk_hash": chunk_hash,
            "vault_path": source,
            "collection": coll_name,
            "scope": mem_scope.value,
            "scope_id": scope_id,
            "updated_at": now_ts,
        }

    async def update_document_content(
        self,
        project_id: str,
        chunk_hash: str,
        content: str,
        *,
        original: str | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Replace the content of an existing document entry (merge case).

        Per spec §8: when similarity 0.8–0.95, the plugin merges old +
        new content via LLM and calls this method with the merged result.
        The embedding is recomputed for the new content.

        Parameters
        ----------
        project_id:
            Project identifier.
        chunk_hash:
            The chunk_hash of the existing entry.
        content:
            New (merged) content to store as the indexed summary.
        original:
            Optional new original text.
        tags:
            Optional merged tag list.
        scope:
            Memory scope.

        Returns
        -------
        dict
            Updated entry info.
        """
        if not self.available:
            raise RuntimeError("MemoryV2Service not available")

        store = self._get_store(project_id, scope)
        entry = await asyncio.to_thread(store.get, chunk_hash)
        if not entry:
            raise ValueError(f"Entry not found: {chunk_hash}")

        import json as _json

        now_ts = int(time.time())

        # Recompute embedding for new content
        embeddings = await self._embedder.embed([content])
        embedding = embeddings[0]

        # Update the entry
        entry["content"] = content
        entry["embedding"] = embedding
        entry["updated_at"] = now_ts
        if original:
            entry["original"] = original
        if tags:
            entry["tags"] = _json.dumps(tags)

        await asyncio.to_thread(store.upsert, [entry])

        # Update vault file if it exists
        source = entry.get("source", "")
        if source:
            vault_file = Path(source)
            if vault_file.exists():
                self._update_vault_file(vault_file, content=content, original=original, tags=tags)

        mem_scope, scope_id = self._resolve_scope(project_id, scope)
        coll_name = collection_name(mem_scope, scope_id)

        return {
            "chunk_hash": chunk_hash,
            "vault_path": source,
            "collection": coll_name,
            "scope": mem_scope.value,
            "scope_id": scope_id,
            "updated_at": now_ts,
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def stats(
        self,
        project_id: str,
        *,
        scope: str | None = None,
    ) -> dict[str, Any]:
        """Get statistics for a scoped Milvus collection.

        Returns entry counts by type, collection name, embedding model
        info, and reindex status.
        """
        if not self.available:
            return {"error": "MemoryV2Service not available"}

        store = self._get_store(project_id, scope)
        mem_scope, scope_id = self._resolve_scope(project_id, scope)
        coll_name = collection_name(mem_scope, scope_id)

        total = await asyncio.to_thread(store.count)
        model = store.model_info

        # Count by entry type (in parallel)
        doc_task = asyncio.to_thread(store.query, filter_expr='entry_type == "document"')
        kv_task = asyncio.to_thread(store.query, filter_expr='entry_type == "kv"')
        temporal_task = asyncio.to_thread(store.query, filter_expr='entry_type == "temporal"')
        doc_results, kv_results, temporal_results = await asyncio.gather(
            doc_task, kv_task, temporal_task
        )

        return {
            "collection": coll_name,
            "scope": mem_scope.value,
            "scope_id": scope_id,
            "total_entries": total,
            "documents": len(doc_results),
            "kv_entries": len(kv_results),
            "temporal_entries": len(temporal_results),
            "embedding_model": model,
            "needs_reindex": store.needs_reindex,
        }

    # ------------------------------------------------------------------
    # Browse / List memories
    # ------------------------------------------------------------------

    async def list_memories(
        self,
        project_id: str,
        *,
        scope: str | None = None,
        topic: str | None = None,
        tag: str | None = None,
        entry_type: str = "document",
        offset: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Browse memory entries in a scope, returning metadata.

        Pure scalar query — no vector computation.  Returns entries
        sorted by ``updated_at`` descending (newest first) with
        pagination via *offset* / *limit*.

        Parameters
        ----------
        project_id:
            Project identifier (determines default scope).
        scope:
            Explicit scope.  ``None`` for the project scope.
        topic:
            Optional topic filter.
        tag:
            Optional tag filter (matches entries whose JSON tags array
            contains the specified tag).
        entry_type:
            Entry type filter.  Defaults to ``"document"`` (semantic
            memories / insights).  Use ``"kv"`` or ``"temporal"`` for
            structured entries, or ``""`` to list all types.
        offset:
            Number of entries to skip (for pagination).
        limit:
            Maximum entries to return (default 50, max 200).

        Returns
        -------
        list[dict]
            Each dict contains metadata fields: ``chunk_hash``,
            ``heading``, ``topic``, ``tags``, ``source``,
            ``retrieval_count``, ``updated_at``, ``entry_type``,
            and a truncated ``content`` preview.
        """
        if not self.available:
            return []

        store = self._get_store(project_id, scope)
        limit = min(limit, 200)

        # Build filter expression
        from memsearch.store import _escape_filter_value

        filters: list[str] = []
        if entry_type:
            escaped = _escape_filter_value(entry_type)
            filters.append(f'entry_type == "{escaped}"')
        if topic:
            escaped = _escape_filter_value(topic)
            filters.append(f'topic == "{escaped}"')
        if tag:
            escaped = _escape_filter_value(tag)
            filters.append(f'tags like "%{escaped}%"')

        filter_expr = " and ".join(filters) if filters else ""

        results = await asyncio.to_thread(store.query, filter_expr=filter_expr)

        # Sort by updated_at descending (newest first)
        results.sort(key=lambda r: r.get("updated_at", 0), reverse=True)

        # Apply pagination
        paginated = results[offset : offset + limit]

        # Annotate with scope info
        mem_scope, scope_id = self._resolve_scope(project_id, scope)
        coll_name = collection_name(mem_scope, scope_id)

        for r in paginated:
            r["_collection"] = coll_name
            r["_scope"] = mem_scope.value
            r["_scope_id"] = scope_id

        return paginated

    # ------------------------------------------------------------------
    # Collection listing
    # ------------------------------------------------------------------

    def list_collections(self) -> list[dict[str, Any]]:
        """List all ``aq_*`` collections in the Milvus instance.

        Returns a list of dicts with ``scope``, ``scope_id``, and
        ``collection`` keys.
        """
        if not self.available:
            return []

        result: list[dict[str, Any]] = []
        for scope, scope_id, name in self._router.list_collections():
            result.append(
                {
                    "scope": scope.value,
                    "scope_id": scope_id,
                    "collection": name,
                }
            )
        return result
