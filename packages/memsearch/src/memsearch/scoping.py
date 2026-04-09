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
        raise ValueError(
            f"Collection name too long ({len(name)} > {_MAX_COLLECTION_NAME_LEN}): "
            f"{name!r}"
        )
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
            logger.info(
                "Opened collection %s (scope=%s, id=%s)", name, scope.value, scope_id
            )
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
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search for entries with a specific tag across collections.

        Per spec section 7.3: cross-scope tag-based discovery using scalar
        filters on the ``tags`` JSON array field.

        Parameters
        ----------
        tag:
            Tag to search for (matched as substring in the JSON array).
        scopes:
            Optional list of ``(scope, scope_id)`` tuples to restrict
            the search.  If ``None``, searches all currently-open
            (cached) collections.
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

        stores_to_search: dict[str, MilvusStore] = {}
        if scopes is not None:
            for scope, scope_id in scopes:
                name = collection_name(scope, scope_id)
                if name in self._stores:
                    stores_to_search[name] = self._stores[name]
        else:
            stores_to_search = dict(self._stores)

        results: list[dict[str, Any]] = []
        for coll_name, store in stores_to_search.items():
            hits = self._tag_search_collection(coll_name, store, filter_expr, limit)
            results.extend(hits)

        return results

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
            logger.warning(
                "Tag search failed for collection %s", coll_name, exc_info=True
            )
            return []

        for hit in hits[:limit]:
            hit["_collection"] = coll_name
            with contextlib.suppress(ValueError):
                s, sid = parse_collection_name(coll_name)
                hit["_scope"] = s.value
                hit["_scope_id"] = sid
        return hits[:limit]

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
