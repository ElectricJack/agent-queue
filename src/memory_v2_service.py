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
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from memsearch import CollectionRouter, MemoryScope, collection_name
    from memsearch.embeddings import EmbeddingProvider, get_provider
    from memsearch.store import MilvusStore

    MEMSEARCH_AVAILABLE = True
except ImportError:
    MEMSEARCH_AVAILABLE = False
    CollectionRouter = None  # type: ignore[assignment,misc]
    MemoryScope = None  # type: ignore[assignment,misc]
    collection_name = None  # type: ignore[assignment]
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
    ) -> dict[str, Any]:
        """Write a KV entry to the scoped collection.

        Creates or updates the entry.  The value is stored as-is in
        ``kv_value`` (caller should JSON-encode complex values).

        Parameters
        ----------
        project_id:
            Project identifier (determines scope collection).
        namespace:
            KV namespace (e.g. ``"project"``, ``"conventions"``).
        key:
            The key to set.
        value:
            The value to store.

        Returns
        -------
        dict
            The stored entry.
        """
        if not self.available:
            raise RuntimeError("MemoryV2Service not available")

        store = self._get_store(project_id)
        return await asyncio.to_thread(
            store.set_kv,
            key,
            value,
            namespace=namespace,
            content=f"{namespace}/{key}: {value}",
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
