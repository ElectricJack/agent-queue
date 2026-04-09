"""Scope-aware collection naming, routing, and lifecycle management.

Implements spec section 7 (Milvus Backend Topology): one Milvus collection
per memory scope, with standardized naming conventions and multi-collection
routing.

Collection naming convention::

    aq_system              -- system-wide memories
    aq_orchestrator        -- orchestrator-level memories
    aq_agenttype_{type}    -- per agent-type memories
    aq_project_{id}        -- per project memories

Each scope maps to a single Milvus collection containing document entries
(with embeddings for semantic search), KV entries (scalar-only for exact
lookup), and temporal entries (validity-windowed facts).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from .store import MilvusStore, _escape_filter_value

logger = logging.getLogger(__name__)

# All agent-queue collections start with this prefix.
_PREFIX = "aq_"

# Milvus collection name constraints: letters, digits, underscores only;
# max 255 chars; must start with a letter or underscore.
_MAX_COLLECTION_NAME_LEN = 255
_COLLECTION_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,254}$")


class MemoryScope(Enum):
    """Memory scoping levels per spec section 7."""

    SYSTEM = "system"
    ORCHESTRATOR = "orchestrator"
    AGENT_TYPE = "agent_type"
    PROJECT = "project"


# Default specificity weights for multi-scope search.
# Per spec §4: project is most specific (1.0), system is broadest (0.4).
# A moderately relevant project-specific memory outranks a highly relevant
# system memory.
SCOPE_WEIGHTS: dict[MemoryScope, float] = {
    MemoryScope.PROJECT: 1.0,
    MemoryScope.AGENT_TYPE: 0.7,
    MemoryScope.ORCHESTRATOR: 0.5,
    MemoryScope.SYSTEM: 0.4,
}

# Minimum results from a topic-filtered search before falling back to
# unfiltered search.  Matches MemSearch._TOPIC_FALLBACK_THRESHOLD.
_TOPIC_FALLBACK_THRESHOLD = 3


# Vault directory templates per scope (relative to vault root).
# Used by higher-level code to resolve indexed directories.
VAULT_PATHS: ClassVar[dict[MemoryScope, list[str]]] = {
    MemoryScope.SYSTEM: [
        "vault/system/memory/",
        "vault/system/facts.md",
    ],
    MemoryScope.ORCHESTRATOR: [
        "vault/orchestrator/memory/",
        "vault/orchestrator/facts.md",
    ],
    MemoryScope.AGENT_TYPE: [
        "vault/agent-types/{id}/memory/",
        "vault/agent-types/{id}/facts.md",
    ],
    MemoryScope.PROJECT: [
        "vault/projects/{id}/memory/",
        "vault/projects/{id}/notes/",
        "vault/projects/{id}/references/",
        "vault/projects/{id}/facts.md",
    ],
}


def sanitize_id(raw_id: str) -> str:
    """Sanitize a raw identifier for use in Milvus collection names.

    Replaces non-alphanumeric characters with underscores, lowercases,
    collapses consecutive underscores, and strips leading/trailing
    underscores.

    Parameters
    ----------
    raw_id:
        The raw scope identifier (project name, agent type, etc.).

    Returns
    -------
    str
        A Milvus-safe identifier fragment.

    Raises
    ------
    ValueError
        If the result is empty after sanitization.

    Examples
    --------
    >>> sanitize_id("mech-fighters")
    'mech_fighters'
    >>> sanitize_id("My Cool Project!!!")
    'my_cool_project'
    >>> sanitize_id("agent--type")
    'agent_type'
    """
    safe = re.sub(r"[^a-zA-Z0-9]", "_", raw_id).lower().strip("_")
    safe = re.sub(r"_+", "_", safe)
    if not safe:
        raise ValueError(f"Cannot sanitize empty or non-alphanumeric id: {raw_id!r}")
    return safe


def collection_name(scope: MemoryScope, scope_id: str | None = None) -> str:
    """Generate the canonical Milvus collection name for a memory scope.

    Parameters
    ----------
    scope:
        The memory scope level.
    scope_id:
        Required for ``AGENT_TYPE`` and ``PROJECT`` scopes.  The raw
        identifier (will be sanitized for Milvus compatibility).

    Returns
    -------
    str
        Milvus-safe collection name.

    Raises
    ------
    ValueError
        If ``scope_id`` is required but not provided, or if the resulting
        name exceeds Milvus limits.

    Examples
    --------
    >>> collection_name(MemoryScope.SYSTEM)
    'aq_system'
    >>> collection_name(MemoryScope.ORCHESTRATOR)
    'aq_orchestrator'
    >>> collection_name(MemoryScope.AGENT_TYPE, "coding")
    'aq_agenttype_coding'
    >>> collection_name(MemoryScope.PROJECT, "mech-fighters")
    'aq_project_mech_fighters'
    """
    if scope == MemoryScope.SYSTEM:
        name = f"{_PREFIX}system"
    elif scope == MemoryScope.ORCHESTRATOR:
        name = f"{_PREFIX}orchestrator"
    elif scope == MemoryScope.AGENT_TYPE:
        if not scope_id:
            raise ValueError("scope_id is required for AGENT_TYPE scope")
        safe = sanitize_id(scope_id)
        name = f"{_PREFIX}agenttype_{safe}"
    elif scope == MemoryScope.PROJECT:
        if not scope_id:
            raise ValueError("scope_id is required for PROJECT scope")
        safe = sanitize_id(scope_id)
        name = f"{_PREFIX}project_{safe}"
    else:
        raise ValueError(f"Unknown scope: {scope}")

    if len(name) > _MAX_COLLECTION_NAME_LEN:
        raise ValueError(f"Collection name too long ({len(name)} > {_MAX_COLLECTION_NAME_LEN}): {name!r}")
    return name


def parse_collection_name(name: str) -> tuple[MemoryScope, str | None]:
    """Parse a collection name back into its scope and optional scope ID.

    Only recognizes names generated by :func:`collection_name`.  The
    returned ``scope_id`` is the *sanitized* form (lowercase, underscores).

    Parameters
    ----------
    name:
        A collection name matching the ``aq_*`` pattern.

    Returns
    -------
    tuple[MemoryScope, str | None]
        ``(scope, scope_id)`` where ``scope_id`` is ``None`` for
        ``SYSTEM`` and ``ORCHESTRATOR`` scopes.

    Raises
    ------
    ValueError
        If the name doesn't match the expected ``aq_*`` pattern.
    """
    if not name.startswith(_PREFIX):
        raise ValueError(f"Not an agent-queue collection: {name!r}")

    suffix = name[len(_PREFIX) :]

    if suffix == "system":
        return (MemoryScope.SYSTEM, None)
    if suffix == "orchestrator":
        return (MemoryScope.ORCHESTRATOR, None)
    if suffix.startswith("agenttype_"):
        scope_id = suffix[len("agenttype_") :]
        if not scope_id:
            raise ValueError(f"Missing agent type id in collection name: {name!r}")
        return (MemoryScope.AGENT_TYPE, scope_id)
    if suffix.startswith("project_"):
        scope_id = suffix[len("project_") :]
        if not scope_id:
            raise ValueError(f"Missing project id in collection name: {name!r}")
        return (MemoryScope.PROJECT, scope_id)

    raise ValueError(f"Unknown scope in collection name: {name!r}")


def vault_paths(scope: MemoryScope, scope_id: str | None = None) -> list[str]:
    """Return the vault directory paths for a given scope.

    Templates like ``{id}`` are replaced with the sanitized scope ID.

    Parameters
    ----------
    scope:
        The memory scope level.
    scope_id:
        Required for ``AGENT_TYPE`` and ``PROJECT`` scopes.

    Returns
    -------
    list[str]
        Relative vault paths for the scope.
    """
    templates = VAULT_PATHS.get(scope, [])
    if scope_id is not None:
        safe = sanitize_id(scope_id)
        return [t.replace("{id}", safe) for t in templates]
    return list(templates)


def merge_and_rank(
    results: list[dict[str, Any]],
    *,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Merge results from multiple collections and rank by weighted score.

    Deduplicates by ``chunk_hash`` — when the same chunk appears in multiple
    scope results (unlikely but possible with cross-indexed data), the entry
    with the highest ``weighted_score`` is kept.

    Parameters
    ----------
    results:
        Flat list of result dicts from multiple collection searches.
        Each dict should contain ``chunk_hash``, ``score``, and
        ``weighted_score`` fields (set by
        :meth:`CollectionRouter._search_collection`).
    top_k:
        Maximum number of results to return.

    Returns
    -------
    list[dict[str, Any]]
        Deduplicated results sorted by ``weighted_score`` descending,
        truncated to *top_k*.
    """
    # Deduplicate: keep the highest weighted score for each chunk
    seen: dict[str, dict[str, Any]] = {}
    for r in results:
        key = r.get("chunk_hash", "")
        if not key:
            key = str(id(r))  # fallback for entries without chunk_hash
        existing = seen.get(key)
        if existing is None or r.get("weighted_score", 0.0) > existing.get("weighted_score", 0.0):
            seen[key] = r

    # Sort by weighted score descending
    ranked = sorted(
        seen.values(),
        key=lambda x: x.get("weighted_score", 0.0),
        reverse=True,
    )
    return ranked[:top_k]


class CollectionRouter:
    """Manages scope-aware Milvus collections per spec section 7.

    Provides lazy creation and caching of :class:`MilvusStore` instances
    for each memory scope.  All stores share the same Milvus URI (database
    file or remote server).

    Parameters
    ----------
    milvus_uri:
        Milvus connection URI.  A local ``*.db`` path uses Milvus Lite,
        ``http://host:port`` connects to a Milvus server.
    token:
        Auth token for remote Milvus server.
    dimension:
        Embedding vector dimension.  ``None`` for read-only mode (won't
        create new collections).
    """

    def __init__(
        self,
        milvus_uri: str = "~/.memsearch/milvus.db",
        *,
        token: str | None = None,
        dimension: int | None = 1536,
    ) -> None:
        self._uri = milvus_uri
        self._token = token
        self._dimension = dimension
        self._stores: dict[str, MilvusStore] = {}

    @property
    def uri(self) -> str:
        """The Milvus connection URI."""
        return self._uri

    @property
    def dimension(self) -> int | None:
        """The embedding vector dimension."""
        return self._dimension

    # ------------------------------------------------------------------
    # Store access
    # ------------------------------------------------------------------

    def get_store(
        self,
        scope: MemoryScope,
        scope_id: str | None = None,
        *,
        description: str = "",
    ) -> MilvusStore:
        """Get or create a :class:`MilvusStore` for the given scope.

        Stores are cached by collection name.  The first call for a given
        scope creates the Milvus collection if it does not already exist.

        Parameters
        ----------
        scope:
            The memory scope level.
        scope_id:
            Required for ``AGENT_TYPE`` and ``PROJECT`` scopes.
        description:
            Optional human-readable description for the collection.
            Defaults to ``"<scope>/<scope_id>"``.

        Returns
        -------
        MilvusStore
            The (possibly cached) store for this scope.
        """
        name = collection_name(scope, scope_id)
        if name not in self._stores:
            desc = description or f"{scope.value}" + (f"/{scope_id}" if scope_id else "")
            self._stores[name] = MilvusStore(
                uri=self._uri,
                token=self._token,
                collection=name,
                dimension=self._dimension,
                description=desc,
            )
            logger.info("Opened collection %s (scope=%s, id=%s)", name, scope.value, scope_id)
        return self._stores[name]

    def has_store(self, scope: MemoryScope, scope_id: str | None = None) -> bool:
        """Check if a store for this scope is already cached (open)."""
        name = collection_name(scope, scope_id)
        return name in self._stores

    # ------------------------------------------------------------------
    # Collection listing
    # ------------------------------------------------------------------

    def list_collections(self) -> list[tuple[MemoryScope, str | None, str]]:
        """List all ``aq_*`` collections in the Milvus instance.

        Returns
        -------
        list[tuple[MemoryScope, str | None, str]]
            Each tuple is ``(scope, scope_id, raw_collection_name)``.
            Only collections matching the ``aq_*`` naming convention are
            included.  Unknown ``aq_*`` names are silently skipped.
        """
        client = self._get_admin_client()
        try:
            all_names: list[str] = client.list_collections()
        finally:
            self._release_admin_client(client)

        result: list[tuple[MemoryScope, str | None, str]] = []
        for name in sorted(all_names):
            if not name.startswith(_PREFIX):
                continue
            try:
                scope, scope_id = parse_collection_name(name)
                result.append((scope, scope_id, name))
            except ValueError:
                logger.debug("Skipping unrecognized aq_ collection: %s", name)
        return result

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def drop_collection(
        self,
        scope: MemoryScope,
        scope_id: str | None = None,
    ) -> bool:
        """Drop a specific scope's collection.

        Closes the cached store (if any) and drops the underlying Milvus
        collection.

        Parameters
        ----------
        scope:
            The memory scope level.
        scope_id:
            Required for ``AGENT_TYPE`` and ``PROJECT`` scopes.

        Returns
        -------
        bool
            ``True`` if the collection existed and was dropped.
        """
        name = collection_name(scope, scope_id)

        # If we have a cached store, use it to drop
        if name in self._stores:
            store = self._stores.pop(name)
            store.drop()
            store.close()
            logger.info("Dropped collection %s", name)
            return True

        # Not cached -- check via admin client and drop if it exists
        client = self._get_admin_client()
        try:
            if client.has_collection(name):
                client.drop_collection(name)
                logger.info("Dropped collection %s", name)
                return True
        finally:
            self._release_admin_client(client)

        return False

    def cleanup_project(self, project_id: str) -> bool:
        """Drop the collection for a specific project.

        Convenience wrapper around :meth:`drop_collection`.
        """
        return self.drop_collection(MemoryScope.PROJECT, project_id)

    def cleanup_agent_type(self, agent_type: str) -> bool:
        """Drop the collection for a specific agent type.

        Convenience wrapper around :meth:`drop_collection`.
        """
        return self.drop_collection(MemoryScope.AGENT_TYPE, agent_type)

    # ------------------------------------------------------------------
    # Cross-scope search
    # ------------------------------------------------------------------

    def search_by_tag(
        self,
        tag: str,
        *,
        scopes: list[tuple[MemoryScope, str | None]] | None = None,
        entry_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search for entries with a specific tag across collections.

        Per spec section 7.3: cross-scope tag-based discovery using scalar
        filters on the ``tags`` JSON array field.

        When *scopes* is ``None``, discovers **all** ``aq_*`` collections
        in the Milvus instance (not just currently-cached ones) and
        searches each one.  This is the cross-cutting search the spec
        describes for queries like "what do we know about SQLite across
        all projects and agent types?"

        Parameters
        ----------
        tag:
            Tag to search for (matched as substring in the JSON array).
        scopes:
            Optional list of ``(scope, scope_id)`` tuples to restrict
            the search.  If ``None``, searches **all** ``aq_*``
            collections discovered via :meth:`list_collections`.
        entry_type:
            Optional entry type filter (``"document"``, ``"kv"``, or
            ``"temporal"``).  When set, only entries of this type are
            returned.
        limit:
            Maximum results per collection.

        Returns
        -------
        list[dict[str, Any]]
            Combined results from all searched collections.  Each result
            dict is augmented with ``_collection``, ``_scope``, and
            ``_scope_id`` keys.
        """
        escaped_tag = _escape_filter_value(tag)
        filter_expr = f'tags like "%\\"{escaped_tag}\\"%"'
        if entry_type:
            escaped_type = _escape_filter_value(entry_type)
            filter_expr += f' and entry_type == "{escaped_type}"'

        stores_to_search = self._resolve_stores(scopes)

        results: list[dict[str, Any]] = []
        for coll_name, store in stores_to_search.items():
            hits = self._tag_search_collection(coll_name, store, filter_expr, limit)
            results.extend(hits)

        return results

    async def search_by_tag_async(
        self,
        tag: str,
        *,
        scopes: list[tuple[MemoryScope, str | None]] | None = None,
        entry_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Async cross-collection tag search with parallel execution.

        Behaves identically to :meth:`search_by_tag` but queries
        collections in parallel using ``asyncio.to_thread``.

        Per spec §7.3::

            async def search_by_tag(tag: str, limit: int = 10) -> list[MemoryResult]:
                \"\"\"Search across ALL collections for memories with a specific tag.\"\"\"
                # Uses Milvus scalar filter: tags LIKE '%"sqlite"%'

        Parameters
        ----------
        tag:
            Tag to search for.
        scopes:
            Restrict to specific ``(scope, scope_id)`` pairs.
            ``None`` means search **all** ``aq_*`` collections.
        entry_type:
            Optional entry type filter.
        limit:
            Maximum results per collection.

        Returns
        -------
        list[dict[str, Any]]
            Combined results annotated with ``_collection``, ``_scope``,
            ``_scope_id``.
        """
        escaped_tag = _escape_filter_value(tag)
        filter_expr = f'tags like "%\\"{escaped_tag}\\"%"'
        if entry_type:
            escaped_type = _escape_filter_value(entry_type)
            filter_expr += f' and entry_type == "{escaped_type}"'

        stores_to_search = self._resolve_stores(scopes)

        if not stores_to_search:
            return []

        # Query all collections in parallel
        tasks = [
            asyncio.to_thread(self._tag_search_collection, coll_name, store, filter_expr, limit)
            for coll_name, store in stores_to_search.items()
        ]
        scope_results = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[dict[str, Any]] = []
        for i, result in enumerate(scope_results):
            if isinstance(result, BaseException):
                coll_name = list(stores_to_search.keys())[i]
                logger.warning(
                    "Async tag search failed for collection %s: %s",
                    coll_name,
                    result,
                )
                continue
            results.extend(result)

        return results

    def _resolve_stores(
        self,
        scopes: list[tuple[MemoryScope, str | None]] | None,
    ) -> dict[str, MilvusStore]:
        """Resolve which stores to search for cross-collection queries.

        When *scopes* is ``None``, discovers all ``aq_*`` collections in
        the Milvus instance and opens each one (if not already cached).
        When *scopes* is provided, only includes those specific
        collections (opening them if they exist in Milvus).

        Returns a ``{collection_name: MilvusStore}`` mapping.
        """
        stores: dict[str, MilvusStore] = {}
        if scopes is not None:
            for scope, scope_id in scopes:
                store = self._get_store_if_exists(scope, scope_id)
                if store is not None:
                    stores[collection_name(scope, scope_id)] = store
        else:
            # Discover ALL aq_* collections in the Milvus instance
            for scope, scope_id, name in self.list_collections():
                store = self._get_store_if_exists(scope, scope_id)
                if store is not None:
                    stores[name] = store
        return stores

    def _tag_search_collection(
        self,
        coll_name: str,
        store: MilvusStore,
        filter_expr: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Search a single collection by tag filter, annotating results."""
        try:
            hits = store.query(filter_expr=filter_expr)
        except Exception:
            logger.warning("Tag search failed for collection %s", coll_name, exc_info=True)
            return []

        for hit in hits[:limit]:
            hit["_collection"] = coll_name
            with contextlib.suppress(ValueError):
                s, sid = parse_collection_name(coll_name)
                hit["_scope"] = s.value
                hit["_scope_id"] = sid
        return hits[:limit]

    # ------------------------------------------------------------------
    # Multi-scope parallel search (spec §4, §6)
    # ------------------------------------------------------------------

    async def search(
        self,
        query_embedding: list[float],
        *,
        query_text: str = "",
        project_id: str | None = None,
        agent_type: str | None = None,
        topic: str | None = None,
        top_k: int = 10,
        weights: dict[MemoryScope, float] | None = None,
    ) -> list[dict[str, Any]]:
        """Multi-collection parallel search with weighted merging.

        Queries project, agent-type, and system collections in parallel,
        then merges results weighted by scope specificity.

        Per spec §4: project=1.0, agent-type=0.7, system=0.4.  A moderately
        relevant project-specific memory outranks a highly relevant system
        memory.

        Parameters
        ----------
        query_embedding:
            Dense vector embedding of the query.
        query_text:
            Raw query text for BM25 sparse search.
        project_id:
            Project identifier.  When set, includes the project collection
            in the search.
        agent_type:
            Agent type identifier.  When set, includes the agent-type
            collection in the search.
        topic:
            Optional topic pre-filter.  When set, only chunks whose
            ``topic`` matches *or* whose ``topic`` is empty (untagged)
            are returned.  Falls back to unfiltered search if too few
            results (< ``_TOPIC_FALLBACK_THRESHOLD``).
        top_k:
            Maximum results to return after merging.
        weights:
            Override the default :data:`SCOPE_WEIGHTS`.  Keys are
            :class:`MemoryScope` values; missing scopes use the default.

        Returns
        -------
        list[dict[str, Any]]
            Merged results sorted by ``weighted_score`` descending.
            Each result is annotated with ``_collection``, ``_scope``,
            ``_scope_id``, ``_weight``, and ``weighted_score`` fields.
        """
        effective_weights = weights if weights is not None else SCOPE_WEIGHTS

        # Build list of (scope, scope_id, weight) for scopes to search.
        scopes_to_search: list[tuple[MemoryScope, str | None, float]] = []
        if project_id:
            w = effective_weights.get(MemoryScope.PROJECT, SCOPE_WEIGHTS[MemoryScope.PROJECT])
            scopes_to_search.append((MemoryScope.PROJECT, project_id, w))
        if agent_type:
            w = effective_weights.get(MemoryScope.AGENT_TYPE, SCOPE_WEIGHTS[MemoryScope.AGENT_TYPE])
            scopes_to_search.append((MemoryScope.AGENT_TYPE, agent_type, w))
        w = effective_weights.get(MemoryScope.SYSTEM, SCOPE_WEIGHTS[MemoryScope.SYSTEM])
        scopes_to_search.append((MemoryScope.SYSTEM, None, w))

        if not scopes_to_search:
            return []

        # Build topic filter expression
        topic_filter = ""
        if topic:
            escaped_topic = _escape_filter_value(topic)
            topic_filter = f'(topic == "{escaped_topic}" or topic == "")'

        # Search all scopes in parallel
        tasks = [
            self._search_collection_async(
                scope,
                scope_id,
                query_embedding,
                query_text=query_text,
                top_k=top_k,
                filter_expr=topic_filter,
                weight=weight,
            )
            for scope, scope_id, weight in scopes_to_search
        ]
        scope_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Flatten results, logging errors
        all_results: list[dict[str, Any]] = []
        for i, result in enumerate(scope_results):
            if isinstance(result, BaseException):
                scope, scope_id, _ = scopes_to_search[i]
                logger.warning(
                    "Search failed for %s/%s: %s",
                    scope.value,
                    scope_id,
                    result,
                )
                continue
            all_results.extend(result)

        # Topic fallback: if too few results, retry without topic filter
        if topic and len(all_results) < _TOPIC_FALLBACK_THRESHOLD:
            logger.debug(
                "Topic filter '%s' returned %d results (< %d), falling back to unfiltered search",
                topic,
                len(all_results),
                _TOPIC_FALLBACK_THRESHOLD,
            )
            tasks = [
                self._search_collection_async(
                    scope,
                    scope_id,
                    query_embedding,
                    query_text=query_text,
                    top_k=top_k,
                    filter_expr="",
                    weight=weight,
                )
                for scope, scope_id, weight in scopes_to_search
            ]
            scope_results = await asyncio.gather(*tasks, return_exceptions=True)
            all_results = []
            for result in scope_results:
                if isinstance(result, BaseException):
                    continue
                for r in result:
                    r["topic_fallback"] = True
                all_results.extend(result)

        return merge_and_rank(all_results, top_k=top_k)

    async def _search_collection_async(
        self,
        scope: MemoryScope,
        scope_id: str | None,
        query_embedding: list[float],
        *,
        query_text: str = "",
        top_k: int = 10,
        filter_expr: str = "",
        weight: float = 1.0,
    ) -> list[dict[str, Any]]:
        """Search a single collection, annotating results with scope metadata.

        Runs the synchronous :meth:`MilvusStore.search` in a thread to
        allow parallel execution across collections.

        If the collection does not exist or is not cached, returns an
        empty list (no collection is created).
        """
        store = self._get_store_if_exists(scope, scope_id)
        if store is None:
            return []

        # Run sync Milvus call in a thread for true parallelism
        results = await asyncio.to_thread(
            store.search,
            query_embedding,
            query_text=query_text,
            top_k=top_k,
            filter_expr=filter_expr,
        )

        # Annotate results with scope metadata and apply weight
        coll_name = collection_name(scope, scope_id)
        for r in results:
            r["_collection"] = coll_name
            r["_scope"] = scope.value
            r["_scope_id"] = scope_id
            r["_weight"] = weight
            r["weighted_score"] = r.get("score", 0.0) * weight

        return results

    async def recall(
        self,
        key: str,
        *,
        project_id: str | None = None,
        agent_type: str | None = None,
        namespace: str | None = None,
    ) -> str | None:
        """KV lookup with scope resolution.  First match wins (most specific).

        Per spec §6: searches project → agent-type → system in order,
        returning the value from the first scope that has a matching entry.

        Parameters
        ----------
        key:
            The KV key to look up.
        project_id:
            Project identifier for the project scope.
        agent_type:
            Agent type identifier for the agent-type scope.
        namespace:
            Optional KV namespace filter (e.g., ``"project"``,
            ``"conventions"``).

        Returns
        -------
        str | None
            The ``kv_value`` from the most specific scope that has the
            key, or ``None`` if not found in any scope.
        """
        scopes: list[tuple[MemoryScope, str | None]] = []
        if project_id:
            scopes.append((MemoryScope.PROJECT, project_id))
        if agent_type:
            scopes.append((MemoryScope.AGENT_TYPE, agent_type))
        scopes.append((MemoryScope.SYSTEM, None))

        escaped_key = _escape_filter_value(key)
        for scope, scope_id in scopes:
            store = self._get_store_if_exists(scope, scope_id)
            if store is None:
                continue

            filter_parts = [
                'entry_type == "kv"',
                f'kv_key == "{escaped_key}"',
            ]
            if namespace:
                escaped_ns = _escape_filter_value(namespace)
                filter_parts.append(f'kv_namespace == "{escaped_ns}"')
            filter_expr = " and ".join(filter_parts)

            try:
                results = store.query(filter_expr=filter_expr)
            except Exception:
                logger.warning(
                    "KV recall failed for %s/%s",
                    scope.value,
                    scope_id,
                    exc_info=True,
                )
                continue

            if results:
                return results[0].get("kv_value")

        return None

    def _get_store_if_exists(
        self,
        scope: MemoryScope,
        scope_id: str | None = None,
    ) -> MilvusStore | None:
        """Get a cached store, or open it if the collection exists in Milvus.

        Unlike :meth:`get_store`, this does **not** create the collection
        if it doesn't exist.  Returns ``None`` when the collection is
        absent — safe for read-only paths like search and recall.
        """
        name = collection_name(scope, scope_id)
        if name in self._stores:
            return self._stores[name]

        # Check existence without creating
        client = self._get_admin_client()
        try:
            exists = client.has_collection(name)
        finally:
            self._release_admin_client(client)

        if not exists:
            return None

        # Collection exists — open it (get_store won't re-create)
        return self.get_store(scope, scope_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close all cached stores and release resources."""
        for store in self._stores.values():
            with contextlib.suppress(Exception):
                store.close()
        self._stores.clear()

    def __enter__(self) -> CollectionRouter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_admin_client(self) -> Any:
        """Get a MilvusClient for admin operations.

        Reuses an existing store's client if one is cached, avoiding
        duplicate connections (important for Milvus Lite which uses a
        single server process per ``.db`` file).  Falls back to creating
        a temporary client.

        Returns the client.  Call :meth:`_release_admin_client` when done.
        """
        if self._stores:
            # Borrow an existing store's client (no cleanup needed)
            return next(iter(self._stores.values()))._client
        # No cached stores — create a temporary client
        return self._create_temp_client()

    def _release_admin_client(self, client: Any) -> None:
        """Release an admin client obtained from :meth:`_get_admin_client`.

        Only closes the client if it was created temporarily (not borrowed
        from an existing store).
        """
        # If the client belongs to one of our stores, don't close it
        for store in self._stores.values():
            if client is store._client:
                return
        # Temporary client — close and release Milvus Lite server
        with contextlib.suppress(Exception):
            client.close()
        self._release_lite_server()

    def _create_temp_client(self) -> Any:
        """Create a temporary MilvusClient for one-off admin operations."""
        from pymilvus import MilvusClient

        is_local = not self._uri.startswith(("http", "tcp"))
        resolved = str(Path(self._uri).expanduser()) if is_local else self._uri
        if is_local:
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        connect_kwargs: dict[str, Any] = {"uri": resolved}
        if self._token:
            connect_kwargs["token"] = self._token
        return MilvusClient(**connect_kwargs)

    def _release_lite_server(self) -> None:
        """Release Milvus Lite server process if running locally."""
        is_local = not self._uri.startswith(("http", "tcp"))
        if not is_local:
            return
        resolved = str(Path(self._uri).expanduser())
        try:
            from milvus_lite.server_manager import server_manager_instance

            server_manager_instance.release_server(resolved)
        except Exception:
            pass
