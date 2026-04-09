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

    def search(
        self,
        query_embedding: list[float],
        *,
        query_text: str = "",
        top_k: int = 10,
        filter_expr: str = "",
    ) -> list[dict[str, Any]]:
        """Hybrid search: dense vector + BM25 full-text with RRF reranking."""
        from pymilvus import AnnSearchRequest, RRFRanker

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
            output_fields=self._QUERY_FIELDS,
        )

        if not results or not results[0]:
            return []
        return [{**hit["entity"], "score": hit["distance"]} for hit in results[0]]

    _QUERY_FIELDS: ClassVar[list[str]] = [
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
    ]

    def query(self, *, filter_expr: str = "") -> list[dict[str, Any]]:
        """Retrieve chunks by scalar filter (no vector needed)."""
        kwargs: dict[str, Any] = {
            "collection_name": self._collection,
            "output_fields": self._QUERY_FIELDS,
            "filter": filter_expr if filter_expr else 'chunk_hash != ""',
        }
        return self._client.query(**kwargs)

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
