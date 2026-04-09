"""Milvus vector storage layer using MilvusClient API."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, ClassVar

logger = logging.getLogger(__name__)

# ---- Embedding model version metadata helpers --------------------------------

_MODEL_META_VERSION = 1


def _build_collection_meta(
    description: str,
    provider: str,
    model: str,
    dimension: int,
) -> str:
    """Build a JSON description containing embedding model version metadata.

    The metadata is stored in the Milvus collection ``description`` field so
    it survives across restarts.  A ``_memsearch`` version key acts as a
    sentinel when parsing back.
    """
    meta: dict[str, Any] = {
        "_memsearch": _MODEL_META_VERSION,
        "provider": provider,
        "model": model,
        "dimension": dimension,
    }
    if description:
        meta["description"] = description
    return json.dumps(meta, separators=(",", ":"))


def _parse_collection_meta(description: str) -> dict[str, Any] | None:
    """Parse model metadata from a collection description.

    Returns the metadata dict if the description contains valid memsearch
    metadata (has a ``_memsearch`` version key), or ``None`` for legacy /
    non-memsearch collections.
    """
    if not description:
        return None
    try:
        data = json.loads(description)
        if isinstance(data, dict) and "_memsearch" in data:
            return data
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _escape_filter_value(value: str) -> str:
    """Escape backslashes and double quotes for Milvus filter expressions."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


class MilvusStore:
    """Thin wrapper around ``pymilvus.MilvusClient`` for chunk storage.

    Collections use both dense vector and BM25 sparse vector fields,
    with hybrid search (semantic + keyword, RRF reranking) by default.
    """

    DEFAULT_COLLECTION = "memsearch_chunks"

    def __init__(
        self,
        uri: str = "~/.memsearch/milvus.db",
        *,
        token: str | None = None,
        collection: str = DEFAULT_COLLECTION,
        dimension: int | None = 1536,
        description: str = "",
        embedding_provider: str = "",
        embedding_model: str = "",
    ) -> None:
        from pymilvus import MilvusClient

        is_local = not uri.startswith(("http", "tcp"))
        if is_local and sys.platform == "win32":
            raise RuntimeError(
                "milvus-lite does not support Windows (no wheels on PyPI).\n"
                "Use a remote Milvus server instead:\n"
                "  docker run -d -p 19530:19530 milvusdb/milvus:latest standalone\n"
                "  MemSearch(milvus_uri='http://localhost:19530')\n"
                "Or run memsearch inside WSL2: "
                "https://learn.microsoft.com/en-us/windows/wsl/install"
            )
        resolved = str(Path(uri).expanduser()) if is_local else uri
        if is_local:
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        connect_kwargs: dict[str, Any] = {"uri": resolved}
        if token:
            connect_kwargs["token"] = token
        self._client = MilvusClient(**connect_kwargs)
        self._is_lite = is_local
        self._resolved_uri = resolved
        self._collection = collection
        self._dimension = dimension
        self._description = description
        self._embedding_provider = embedding_provider
        self._embedding_model = embedding_model
        self._needs_reindex = False
        self._stored_model_info: dict[str, Any] | None = None
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if self._client.has_collection(self._collection):
            self._check_dimension()
            self._check_model()
            return

        if self._dimension is None:
            return  # read-only mode: don't create a new collection

        from pymilvus import DataType, Function, FunctionType

        # Build description with model version metadata when provider info
        # is available; otherwise use the plain user-supplied description.
        if self._embedding_provider and self._embedding_model and self._dimension:
            description = _build_collection_meta(
                self._description,
                self._embedding_provider,
                self._embedding_model,
                self._dimension,
            )
            # Populate model_info immediately for newly created collections
            self._stored_model_info = {
                "provider": self._embedding_provider,
                "model": self._embedding_model,
                "dimension": self._dimension,
            }
        else:
            description = self._description

        schema = self._client.create_schema(
            enable_dynamic_field=True,
            description=description,
        )
        # --- Core identity ---
        schema.add_field(
            field_name="chunk_hash",
            datatype=DataType.VARCHAR,
            max_length=64,
            is_primary=True,
        )
        schema.add_field(
            field_name="entry_type",
            datatype=DataType.VARCHAR,
            max_length=16,
        )  # "document" | "kv" | "temporal"

        # --- Vector search (documents) ---
        schema.add_field(
            field_name="embedding",
            datatype=DataType.FLOAT_VECTOR,
            dim=self._dimension,
        )
        schema.add_field(
            field_name="content",
            datatype=DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
        )  # Summary text (indexed for BM25)
        schema.add_field(
            field_name="sparse_vector",
            datatype=DataType.SPARSE_FLOAT_VECTOR,
        )
        schema.add_field(
            field_name="original",
            datatype=DataType.VARCHAR,
            max_length=65535,
        )  # Full original text (not indexed)

        # --- KV fields ---
        schema.add_field(
            field_name="kv_namespace",
            datatype=DataType.VARCHAR,
            max_length=256,
        )  # "project", "conventions", "stats"
        schema.add_field(
            field_name="kv_key",
            datatype=DataType.VARCHAR,
            max_length=512,
        )
        schema.add_field(
            field_name="kv_value",
            datatype=DataType.VARCHAR,
            max_length=65535,
        )  # JSON-encoded

        # --- Temporal validity (KV and temporal entries) ---
        schema.add_field(
            field_name="valid_from",
            datatype=DataType.INT64,
        )  # Unix timestamp, 0 = always
        schema.add_field(
            field_name="valid_to",
            datatype=DataType.INT64,
        )  # Unix timestamp, 0 = current/open

        # --- Topic filtering (documents) ---
        schema.add_field(
            field_name="topic",
            datatype=DataType.VARCHAR,
            max_length=256,
        )  # e.g., "authentication", "testing"

        # --- Metadata (all entry types) ---
        schema.add_field(
            field_name="source",
            datatype=DataType.VARCHAR,
            max_length=1024,
        )
        schema.add_field(
            field_name="tags",
            datatype=DataType.VARCHAR,
            max_length=4096,
        )  # JSON array
        schema.add_field(
            field_name="updated_at",
            datatype=DataType.INT64,
        )

        # --- Retrieval tracking (spec §6 — memory health observability) ---
        schema.add_field(
            field_name="retrieval_count",
            datatype=DataType.INT64,
        )  # Incremented on each search hit
        schema.add_field(
            field_name="last_retrieved",
            datatype=DataType.INT64,
        )  # Unix timestamp of last retrieval

        # --- Legacy document fields (retained for backward compat) ---
        schema.add_field(
            field_name="heading",
            datatype=DataType.VARCHAR,
            max_length=1024,
        )
        schema.add_field(field_name="heading_level", datatype=DataType.INT64)
        schema.add_field(field_name="start_line", datatype=DataType.INT64)
        schema.add_field(field_name="end_line", datatype=DataType.INT64)
        schema.add_function(
            Function(
                name="bm25_fn",
                function_type=FunctionType.BM25,
                input_field_names=["content"],
                output_field_names=["sparse_vector"],
            )
        )

        index_params = self._client.prepare_index_params()
        index_params.add_index(field_name="embedding", index_type="FLAT", metric_type="COSINE")
        index_params.add_index(field_name="sparse_vector", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")

        self._client.create_collection(
            collection_name=self._collection,
            schema=schema,
            index_params=index_params,
        )

    def _check_dimension(self) -> None:
        """Verify that the existing collection's embedding dimension matches."""
        if self._dimension is None:
            return  # no dimension specified — skip check (read-only mode)
        try:
            info = self._client.describe_collection(self._collection)
        except Exception:
            return  # best-effort; skip if describe is not supported
        for field in info.get("fields", []):
            if field.get("name") == "embedding":
                existing_dim = field.get("params", {}).get("dim")
                if existing_dim is not None and int(existing_dim) != self._dimension:
                    raise ValueError(
                        f"Embedding dimension mismatch: collection '{self._collection}' "
                        f"has dim={existing_dim} but the current embedding provider "
                        f"outputs dim={self._dimension}. "
                        f"Run 'memsearch reset --yes' to drop the collection and re-index, "
                        f"or use a different --milvus-uri / --collection."
                    )
                break

    def _check_model(self) -> None:
        """Read stored embedding model metadata and detect model changes.

        Always reads the collection description to populate
        :pyattr:`model_info`.  When the current embedding provider /
        model is known (i.e. not read-only mode), compares against the
        stored values and sets :pyattr:`needs_reindex` on mismatch.
        """
        try:
            info = self._client.describe_collection(self._collection)
        except Exception:
            return  # best-effort

        description = info.get("description", "")
        meta = _parse_collection_meta(description)

        if meta is None:
            # Legacy collection — no model metadata stored.
            if self._embedding_provider and self._embedding_model:
                logger.info(
                    "Collection '%s' has no embedding model metadata (legacy). "
                    "Model version tracking is unavailable for this collection.",
                    self._collection,
                )
            return

        self._stored_model_info = {
            "provider": meta.get("provider", ""),
            "model": meta.get("model", ""),
            "dimension": meta.get("dimension", 0),
        }

        # Compare only when we know the current model (not read-only mode).
        if not self._embedding_provider or not self._embedding_model:
            return

        stored_provider = meta.get("provider", "")
        stored_model = meta.get("model", "")

        if stored_provider != self._embedding_provider or stored_model != self._embedding_model:
            self._needs_reindex = True
            logger.warning(
                "Embedding model changed for collection '%s': "
                "stored=%s/%s, current=%s/%s. "
                "Existing embeddings were generated with a different model. "
                "Run 'memsearch index --force' to re-embed all documents.",
                self._collection,
                stored_provider,
                stored_model,
                self._embedding_provider,
                self._embedding_model,
            )

    # ---- Model version properties ----------------------------------------

    @property
    def model_info(self) -> dict[str, Any] | None:
        """Return the embedding model info stored in the collection metadata.

        Returns a dict with ``provider``, ``model``, ``dimension`` keys,
        or ``None`` for legacy collections without model tracking.
        """
        return self._stored_model_info

    @property
    def needs_reindex(self) -> bool:
        """True when the current embedding model differs from the stored one.

        When this is ``True``, existing embeddings were produced by a
        different model than the one currently configured.  A full
        re-index (``memsearch index --force``) is recommended.
        """
        return self._needs_reindex

    # Default values for unified schema fields.  Applied automatically by
    # ``upsert`` so callers only need to supply the fields they care about.
    _FIELD_DEFAULTS: ClassVar[dict[str, Any]] = {
        "entry_type": "document",
        "original": "",
        "kv_namespace": "",
        "kv_key": "",
        "kv_value": "",
        "valid_from": 0,
        "valid_to": 0,
        "topic": "",
        "tags": "[]",
        "updated_at": 0,
        "retrieval_count": 0,
        "last_retrieved": 0,
    }

    def upsert(self, chunks: list[dict[str, Any]]) -> int:
        """Insert or update chunks (keyed by ``chunk_hash`` primary key).

        ``sparse_vector`` is auto-generated by the BM25 Function from
        ``content`` — do NOT include it in chunk dicts.

        Unified schema fields (``entry_type``, ``original``, ``kv_namespace``,
        ``kv_key``, ``kv_value``, ``valid_from``, ``valid_to``, ``topic``,
        ``tags``, ``updated_at``) are filled with sensible defaults when
        omitted, so existing callers do not need to change.
        """
        if not chunks:
            return 0
        data = [{**self._FIELD_DEFAULTS, **chunk} for chunk in chunks]
        result = self._client.upsert(
            collection_name=self._collection,
            data=data,
        )
        return result.get("upsert_count", len(chunks)) if isinstance(result, dict) else len(chunks)

    def _update_retrieval_stats(self, chunk_hashes: list[str]) -> None:
        """Increment ``retrieval_count`` and set ``last_retrieved`` for the given entries.

        This implements the retrieval tracking requirement from spec §6:
        ``retrieval_count`` and ``last_retrieved`` are updated when search
        results are returned, powering staleness and hit-rate metrics.

        The method fetches the full records (including embeddings) so that
        the re-upsert preserves all existing field values.
        """
        if not chunk_hashes:
            return

        import time

        now = int(time.time())

        # Build a filter to fetch all matching records in one query.
        # Include embedding so re-upsert preserves the vector.
        escaped = [_escape_filter_value(h) for h in chunk_hashes]
        id_list = ", ".join(f'"{h}"' for h in escaped)
        filter_expr = f"chunk_hash in [{id_list}]"

        try:
            records = self._client.query(
                collection_name=self._collection,
                filter=filter_expr,
                output_fields=["*"],
            )
        except Exception:
            logger.debug("Failed to fetch records for retrieval tracking", exc_info=True)
            return

        if not records:
            return

        updated: list[dict[str, Any]] = []
        for rec in records:
            rec["retrieval_count"] = rec.get("retrieval_count", 0) + 1
            rec["last_retrieved"] = now
            # Remove sparse_vector — it's auto-generated by the BM25 Function
            rec.pop("sparse_vector", None)
            updated.append(rec)

        try:
            self._client.upsert(
                collection_name=self._collection,
                data=updated,
            )
        except Exception:
            logger.debug("Failed to update retrieval stats", exc_info=True)

    def search(
        self,
        query_embedding: list[float],
        *,
        query_text: str = "",
        top_k: int = 10,
        filter_expr: str = "",
        full: bool = False,
    ) -> list[dict[str, Any]]:
        """Hybrid search: dense vector + BM25 full-text with RRF reranking.

        Parameters
        ----------
        query_embedding:
            Dense vector for the query.
        query_text:
            Raw query text for BM25 sparse search.
        top_k:
            Maximum results.
        filter_expr:
            Milvus filter expression.
        full:
            When ``True``, include the ``original`` field in results
            (full content alongside the summary).  When ``False``
            (default), only the summary ``content`` field is returned.
            Per spec §9: "Search returns summary, full retrieval
            returns original."
        """
        from pymilvus import AnnSearchRequest, RRFRanker

        output_fields = self._FULL_FIELDS if full else self._SUMMARY_FIELDS

        req_kwargs: dict[str, Any] = {}
        if filter_expr:
            req_kwargs["expr"] = filter_expr

        dense_req = AnnSearchRequest(
            data=[query_embedding],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {}},
            limit=top_k,
            **req_kwargs,
        )

        bm25_req = AnnSearchRequest(
            data=[query_text] if query_text else [""],
            anns_field="sparse_vector",
            param={"metric_type": "BM25"},
            limit=top_k,
            **req_kwargs,
        )

        results = self._client.hybrid_search(
            collection_name=self._collection,
            reqs=[dense_req, bm25_req],
            ranker=RRFRanker(k=60),
            limit=top_k,
            output_fields=output_fields,
        )

        if not results or not results[0]:
            return []

        hits = [{**hit["entity"], "score": hit["distance"]} for hit in results[0]]

        # Update retrieval tracking (spec §6): increment retrieval_count
        # and set last_retrieved for all returned results.
        hit_hashes = [h["chunk_hash"] for h in hits if "chunk_hash" in h]
        self._update_retrieval_stats(hit_hashes)

        return hits

    # Fields returned by search (summary mode) — excludes ``original`` to
    # keep results compact.  Per spec §9: "Search returns summary, full
    # retrieval returns original."
    _SUMMARY_FIELDS: ClassVar[list[str]] = [
        "content",
        "source",
        "heading",
        "chunk_hash",
        "heading_level",
        "start_line",
        "end_line",
        "entry_type",
        "kv_namespace",
        "kv_key",
        "kv_value",
        "valid_from",
        "valid_to",
        "topic",
        "tags",
        "updated_at",
        "retrieval_count",
        "last_retrieved",
    ]

    # All queryable fields including ``original`` — used by ``get()`` and
    # ``search(full=True)`` when the caller needs the full original content.
    _FULL_FIELDS: ClassVar[list[str]] = [
        "content",
        "source",
        "heading",
        "chunk_hash",
        "heading_level",
        "start_line",
        "end_line",
        "entry_type",
        "original",
        "kv_namespace",
        "kv_key",
        "kv_value",
        "valid_from",
        "valid_to",
        "topic",
        "tags",
        "updated_at",
        "retrieval_count",
        "last_retrieved",
    ]

    # Backward-compatible alias — callers that referenced ``_QUERY_FIELDS``
    # directly get summary fields (the non-breaking default).
    _QUERY_FIELDS: ClassVar[list[str]] = _SUMMARY_FIELDS

    def query(self, *, filter_expr: str = "", full: bool = False) -> list[dict[str, Any]]:
        """Retrieve chunks by scalar filter (no vector needed).

        Parameters
        ----------
        filter_expr:
            Milvus filter expression.
        full:
            When ``True``, include the ``original`` field in results.
            Defaults to ``False`` (summary-only).
        """
        output_fields = self._FULL_FIELDS if full else self._SUMMARY_FIELDS
        kwargs: dict[str, Any] = {
            "collection_name": self._collection,
            "output_fields": output_fields,
            "filter": filter_expr if filter_expr else 'chunk_hash != ""',
        }
        return self._client.query(**kwargs)

    def get(self, chunk_hash: str) -> dict[str, Any] | None:
        """Retrieve a single entry by its ``chunk_hash``, including original content.

        This is the "full retrieval" path from spec §9: returns the
        ``original`` field alongside ``content`` (summary) and all metadata.

        Parameters
        ----------
        chunk_hash:
            The primary key of the entry.

        Returns
        -------
        dict | None
            The full entry including ``original``, or ``None`` if not found.
        """
        escaped = _escape_filter_value(chunk_hash)
        results = self._client.query(
            collection_name=self._collection,
            filter=f'chunk_hash == "{escaped}"',
            output_fields=self._FULL_FIELDS,
        )
        return results[0] if results else None

    # ---- KV API (spec §6 — scalar-only insert/query) ----------------------

    def set_kv(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = "",
        source: str = "",
        content: str = "",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Insert or update a KV entry (scalar-only, no vector search).

        The ``chunk_hash`` is deterministic:
        ``sha256("kv:{namespace}:{key}")[:32]``, so repeated calls with the
        same *key* / *namespace* overwrite (upsert) the existing entry.

        Parameters
        ----------
        key:
            The key to store (``kv_key``).
        value:
            The value.  Will be JSON-encoded into ``kv_value``.
        namespace:
            Optional namespace for grouping related KV pairs.
        source:
            Source identifier for provenance tracking.
        content:
            Human-readable description (indexed for BM25 keyword search).
        tags:
            Optional list of string tags.

        Returns
        -------
        dict
            The stored entry (without the ``embedding`` field).
        """
        import hashlib
        import time

        json_value = json.dumps(value)
        json_tags = json.dumps(tags or [])
        now = int(time.time())

        chunk_hash = hashlib.sha256(f"kv:{namespace}:{key}".encode()).hexdigest()[:32]

        entry: dict[str, Any] = {
            "chunk_hash": chunk_hash,
            "entry_type": "kv",
            "embedding": self._zero_embedding(),
            "content": content,
            "original": "",
            "source": source,
            "heading": "",
            "heading_level": 0,
            "start_line": 0,
            "end_line": 0,
            "kv_namespace": namespace,
            "kv_key": key,
            "kv_value": json_value,
            "valid_from": 0,
            "valid_to": 0,
            "topic": "",
            "tags": json_tags,
            "updated_at": now,
        }
        self.upsert([entry])

        # Return without embedding (matches query output format)
        return {k: v for k, v in entry.items() if k != "embedding"}

    def get_kv(
        self,
        key: str,
        *,
        namespace: str = "",
    ) -> dict[str, Any] | None:
        """Retrieve a single KV entry by exact key and namespace.

        Pure scalar lookup — no vector search.

        Parameters
        ----------
        key:
            The key to look up (``kv_key``).
        namespace:
            Namespace filter (exact match on ``kv_namespace``).

        Returns
        -------
        dict | None
            The entry if found, or ``None``.
        """
        # Use deterministic hash for direct primary-key lookup.
        import hashlib

        chunk_hash = hashlib.sha256(f"kv:{namespace}:{key}".encode()).hexdigest()[:32]
        entry = self.get(chunk_hash)
        if entry is None:
            return None
        # Verify it's actually a KV entry (guard against hash collisions).
        if entry.get("entry_type") != "kv":
            return None
        return entry

    def list_kv(
        self,
        *,
        namespace: str = "",
    ) -> list[dict[str, Any]]:
        """List all KV entries in a namespace.

        Pure scalar query — no vector search.

        Parameters
        ----------
        namespace:
            Namespace filter (exact match on ``kv_namespace``).
            Empty string returns KV entries with no namespace.

        Returns
        -------
        list[dict]
            All KV entries in the namespace, sorted by ``kv_key``.
        """
        all_kv = self.query(filter_expr='entry_type == "kv"')
        results = [e for e in all_kv if e["kv_namespace"] == namespace]
        results.sort(key=lambda e: e["kv_key"])
        return results

    def delete_kv(
        self,
        key: str,
        *,
        namespace: str = "",
    ) -> bool:
        """Delete a KV entry by key and namespace.

        Parameters
        ----------
        key:
            The key to delete (``kv_key``).
        namespace:
            Namespace filter (exact match on ``kv_namespace``).

        Returns
        -------
        bool
            ``True`` if an entry was deleted, ``False`` if it didn't exist.
        """
        import hashlib

        chunk_hash = hashlib.sha256(f"kv:{namespace}:{key}".encode()).hexdigest()[:32]
        existing = self.get(chunk_hash)
        if existing is None or existing.get("entry_type") != "kv":
            return False
        self.delete_by_hashes([chunk_hash])
        return True

    def list_kv_keys(
        self,
        *,
        namespace: str = "",
    ) -> list[str]:
        """List all unique KV keys in a namespace.

        Parameters
        ----------
        namespace:
            Namespace filter (exact match on ``kv_namespace``).

        Returns
        -------
        list[str]
            Sorted list of unique ``kv_key`` values.
        """
        all_kv = self.query(filter_expr='entry_type == "kv"')
        keys: set[str] = set()
        for entry in all_kv:
            if entry["kv_namespace"] == namespace:
                keys.add(entry["kv_key"])
        return sorted(keys)

    # ---- Temporal fact API (spec §6) --------------------------------------

    def _zero_embedding(self) -> list[float]:
        """Return a zero vector of the configured dimension.

        Used for non-document entries (KV, temporal) that don't need
        vector search but still require the ``embedding`` field.

        Raises
        ------
        RuntimeError
            If dimension is ``None`` (read-only mode).
        """
        if self._dimension is None:
            raise RuntimeError("Cannot create entries in read-only mode (dimension=None)")
        return [0.0] * self._dimension

    def set_temporal(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = "",
        source: str = "",
        content: str = "",
        tags: list[str] | None = None,
        timestamp: int | None = None,
    ) -> dict[str, Any]:
        """Insert or update a temporal fact with validity windowing.

        Implements the temporal fact lifecycle from spec §6:

        1. Closes any existing open entry for the same *key* / *namespace*
           (sets its ``valid_to`` to the current timestamp).
        2. Creates a new entry with ``valid_from`` = now, ``valid_to`` = 0
           (open / current).

        Both entries persist so the full history is preserved.

        Parameters
        ----------
        key:
            The fact key (stored in ``kv_key``).
        value:
            The fact value.  Will be JSON-encoded into ``kv_value``.
        namespace:
            Optional namespace for grouping related facts.
        source:
            Source identifier for provenance tracking.
        content:
            Human-readable description (indexed for BM25 keyword search).
        tags:
            Optional list of string tags.
        timestamp:
            Explicit Unix timestamp to use instead of ``time.time()``.
            Primarily useful for deterministic tests and data imports.

        Returns
        -------
        dict
            The newly created entry (without the ``embedding`` field).
        """
        import hashlib
        import os
        import time

        now = timestamp if timestamp is not None else int(time.time())
        json_value = json.dumps(value)
        json_tags = json.dumps(tags or [])

        # Query all temporal entries and filter by key + namespace in Python.
        # Milvus Lite only evaluates the first clause of an AND chain
        # reliably; subsequent clauses may be silently ignored.
        all_temporal = self.query(filter_expr='entry_type == "temporal"')
        open_entries = [
            e for e in all_temporal if e["kv_key"] == key and e["kv_namespace"] == namespace and e["valid_to"] == 0
        ]

        # Close each open entry by re-upserting with valid_to = now
        for entry in open_entries:
            self.upsert(
                [
                    {
                        **entry,
                        "embedding": self._zero_embedding(),
                        "valid_to": now,
                    }
                ]
            )

        # Create new entry with a unique hash.  Random entropy prevents
        # collisions when two updates land in the same integer-second.
        nonce = os.urandom(8).hex()
        chunk_hash = hashlib.sha256(f"temporal:{namespace}:{key}:{now}:{nonce}".encode()).hexdigest()[:32]

        new_entry: dict[str, Any] = {
            "chunk_hash": chunk_hash,
            "entry_type": "temporal",
            "embedding": self._zero_embedding(),
            "content": content,
            "original": "",
            "source": source,
            "heading": "",
            "heading_level": 0,
            "start_line": 0,
            "end_line": 0,
            "kv_namespace": namespace,
            "kv_key": key,
            "kv_value": json_value,
            "valid_from": now,
            "valid_to": 0,
            "topic": "",
            "tags": json_tags,
            "updated_at": now,
        }
        self.upsert([new_entry])

        # Return without embedding (matches query output format)
        return {k: v for k, v in new_entry.items() if k != "embedding"}

    def get_temporal(
        self,
        key: str,
        *,
        namespace: str = "",
        at: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query temporal facts at a point in time.

        With the default ``at=None``, returns the *current* value (entries
        whose validity window covers ``time.time()``).  When *at* is an
        explicit Unix timestamp, performs a historical "as-of" query.

        Parameters
        ----------
        key:
            The fact key to look up (``kv_key``).
        namespace:
            Namespace filter (exact match on ``kv_namespace``).
        at:
            Unix timestamp for the query.  ``None`` means *now*.

        Returns
        -------
        list[dict]
            Matching entries.  Typically one entry for a given key at any
            point in time, but multiple are possible if validity windows
            were constructed with overlaps.
        """
        import time

        ts = at if at is not None else int(time.time())

        # Single-clause Milvus filter + Python post-filtering.
        # Milvus Lite only evaluates the first AND clause reliably.
        all_temporal = self.query(filter_expr='entry_type == "temporal"')

        return [
            r
            for r in all_temporal
            if r["kv_key"] == key
            and r["kv_namespace"] == namespace
            and r["valid_from"] <= ts
            and (r["valid_to"] == 0 or r["valid_to"] > ts)
        ]

    def get_temporal_history(
        self,
        key: str,
        *,
        namespace: str = "",
    ) -> list[dict[str, Any]]:
        """Get the full version history of a temporal fact.

        Returns all entries (open and closed) for the given key, sorted
        by ``valid_from`` ascending.  Useful for pattern detection:
        *"this config was stable for 6 months then changed — investigate why."*

        Parameters
        ----------
        key:
            The fact key to look up (``kv_key``).
        namespace:
            Namespace filter (exact match on ``kv_namespace``).

        Returns
        -------
        list[dict]
            All entries for the key, ordered by ``valid_from`` ascending.
        """
        # Single-clause Milvus filter + Python post-filtering.
        all_temporal = self.query(filter_expr='entry_type == "temporal"')
        results = [r for r in all_temporal if r["kv_key"] == key and r["kv_namespace"] == namespace]
        results.sort(key=lambda r: r["valid_from"])
        return results

    def delete_temporal(
        self,
        key: str,
        *,
        namespace: str = "",
        timestamp: int | None = None,
    ) -> list[dict[str, Any]]:
        """Close the current temporal fact without creating a replacement.

        This is the "end of life" operation in the temporal fact lifecycle:
        the fact is expired (its ``valid_to`` is set to *now*) but no new
        entry is created.  The history chain is preserved — callers can
        still retrieve past values via :meth:`get_temporal` with an
        explicit ``at`` timestamp or :meth:`get_temporal_history`.

        Parameters
        ----------
        key:
            The fact key to expire (``kv_key``).
        namespace:
            Namespace filter (exact match on ``kv_namespace``).
        timestamp:
            Explicit Unix timestamp for the close time.  ``None`` means
            ``int(time.time())``.

        Returns
        -------
        list[dict]
            The entries that were closed (without the ``embedding`` field).
            Empty list if no open entry existed for the key.
        """
        import time

        now = timestamp if timestamp is not None else int(time.time())

        all_temporal = self.query(filter_expr='entry_type == "temporal"')
        open_entries = [
            e for e in all_temporal if e["kv_key"] == key and e["kv_namespace"] == namespace and e["valid_to"] == 0
        ]

        closed: list[dict[str, Any]] = []
        for entry in open_entries:
            updated = {
                **entry,
                "embedding": self._zero_embedding(),
                "valid_to": now,
            }
            self.upsert([updated])
            closed.append({k: v for k, v in updated.items() if k != "embedding"})

        return closed

    def list_temporal_keys(
        self,
        *,
        namespace: str = "",
        current_only: bool = False,
    ) -> list[str]:
        """List all unique temporal fact keys in a namespace.

        Parameters
        ----------
        namespace:
            Namespace filter (exact match on ``kv_namespace``).
        current_only:
            If ``True``, only return keys that have a currently-open entry
            (``valid_to == 0``).  If ``False`` (default), return keys that
            have *any* entry (open or closed).

        Returns
        -------
        list[str]
            Sorted list of unique ``kv_key`` values.
        """
        all_temporal = self.query(filter_expr='entry_type == "temporal"')
        keys: set[str] = set()
        for entry in all_temporal:
            if entry["kv_namespace"] != namespace:
                continue
            if current_only and entry["valid_to"] != 0:
                continue
            keys.add(entry["kv_key"])
        return sorted(keys)

    def hashes_by_source(self, source: str) -> set[str]:
        """Return all chunk_hash values for a given source file."""
        escaped = _escape_filter_value(source)
        results = self._client.query(
            collection_name=self._collection,
            filter=f'source == "{escaped}"',
            output_fields=["chunk_hash"],
        )
        return {r["chunk_hash"] for r in results}

    def indexed_sources(self) -> set[str]:
        """Return all distinct source values in the collection."""
        results = self._client.query(
            collection_name=self._collection,
            filter='chunk_hash != ""',
            output_fields=["source"],
        )
        return {r["source"] for r in results}

    def delete_by_source(self, source: str) -> None:
        """Delete all chunks from a given source file."""
        escaped = _escape_filter_value(source)
        self._client.delete(
            collection_name=self._collection,
            filter=f'source == "{escaped}"',
        )

    def delete_by_hashes(self, hashes: list[str]) -> None:
        """Delete chunks by their content hashes (primary keys)."""
        if not hashes:
            return
        self._client.delete(
            collection_name=self._collection,
            ids=hashes,
        )

    def count(self) -> int:
        """Return total number of stored chunks."""
        stats = self._client.get_collection_stats(self._collection)
        return stats.get("row_count", 0)

    def drop(self) -> None:
        """Drop the entire collection."""
        if self._client.has_collection(self._collection):
            self._client.drop_collection(self._collection)

    def close(self) -> None:
        self._client.close()
        # Milvus Lite: release the server process to free the db file lock.
        # Without this, the milvus_lite subprocess outlives the parent and
        # blocks subsequent CLI invocations from opening the same .db file.
        if self._is_lite:
            try:
                from milvus_lite.server_manager import server_manager_instance

                server_manager_instance.release_server(self._resolved_uri)
            except Exception:
                pass

    def __enter__(self) -> MilvusStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
