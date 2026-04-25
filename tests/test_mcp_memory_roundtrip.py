"""Tests for MCP memory tool round-trips — Roadmap 2.2.16.

Spec: docs/specs/design/memory-scoping.md §7

Tests cover the eight required round-trip scenarios:
  (a) memory_save then memory_search returns saved content with high similarity
  (b) memory_kv_set then memory_fact_recall retrieves exact value
  (c) memory_list returns all memories in scope with correct metadata
  (d) memory_fact_list returns all KV entries in scope/namespace
  (e) memory_save with duplicate content does not create a second entry (dedup)
  (f) memory_search with no results returns empty list (not error)
  (g) memory_kv_set then overwrite same key then memory_fact_recall returns latest value
  (h) all tools return well-formed response dicts with `success` field

All tests exercise the plugin-layer command handlers (cmd_*) which are
the exact code paths invoked by MCP tool calls via CommandHandler.execute().
"""

from __future__ import annotations

import pytest

pytest.importorskip("aq_memory")

import json  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

# Skip entire module on Windows (Milvus Lite not supported)
if sys.platform == "win32":
    pytest.skip("Milvus Lite not supported on Windows", allow_module_level=True)

from aq_memory.service import MEMSEARCH_AVAILABLE, MemoryService
from aq_memory import MemoryPlugin

# All tests require memsearch
pytestmark = pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_embedder():
    """Create a mock embedding provider."""
    embedder = MagicMock()
    embedder.model_name = "test-model"
    embedder.dimension = 384
    embedder.embed = AsyncMock(return_value=[[0.1] * 384])
    return embedder


@pytest.fixture
def mock_store():
    """Create a mock MilvusStore with all needed methods."""
    store = MagicMock()
    store.count.return_value = 10
    store.model_info = {"provider": "test", "model": "test-model", "dimension": 384}
    store.needs_reindex = False

    # Document operations
    store.upsert.return_value = 1
    store.get.return_value = {
        "chunk_hash": "existing_hash",
        "entry_type": "document",
        "content": "Existing insight about authentication",
        "original": "Full original text",
        "source": "",
        "heading": "Existing insight",
        "topic": "auth",
        "tags": '["insight"]',
        "updated_at": 1000,
        "embedding": [0.1] * 384,
    }
    store.search.return_value = []

    # KV methods
    store.get_kv.return_value = {
        "kv_namespace": "project",
        "kv_key": "test_key",
        "kv_value": '"test_value"',
        "updated_at": 1000,
        "tags": "[]",
        "source": "",
    }
    store.set_kv.return_value = {
        "chunk_hash": "abc123",
        "kv_namespace": "project",
        "kv_key": "test_key",
        "kv_value": '"test_value"',
        "updated_at": 1000,
        "tags": "[]",
        "source": "",
    }
    store.list_kv.return_value = [
        {
            "kv_namespace": "project",
            "kv_key": "key1",
            "kv_value": '"val1"',
            "updated_at": 1000,
            "tags": "[]",
            "source": "",
        },
    ]

    # Temporal fact methods
    store.list_temporal.return_value = [
        {
            "kv_key": "deploy_branch",
            "kv_value": '"main"',
            "valid_from": 1000,
            "valid_to": 0,
            "updated_at": 1000,
            "tags": "[]",
            "source": "",
        },
    ]

    # Query (for list_memories)
    store.query.return_value = []

    return store


@pytest.fixture
def mock_router(mock_store):
    """Create a mock CollectionRouter."""
    router = MagicMock()
    router.get_store.return_value = mock_store
    router.list_collections.return_value = []
    router.search = AsyncMock(return_value=[])
    router.search_by_tag_async = AsyncMock(return_value=[])
    router.close = MagicMock()
    return router


@pytest.fixture
def tmp_data_dir():
    """Create a temporary directory for vault files."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def service(mock_embedder, mock_router, tmp_data_dir):
    """Create a MemoryService with mocked dependencies and temp vault."""
    svc = MemoryService(
        milvus_uri="/tmp/test.db",
        embedding_provider="openai",
        data_dir=tmp_data_dir,
    )
    svc._embedder = mock_embedder
    svc._router = mock_router
    svc._initialized = True
    return svc


@pytest.fixture
def plugin():
    """Create a fresh MemoryPlugin instance."""
    return MemoryPlugin()


@pytest.fixture
def wired_plugin(plugin, service):
    """Plugin with a wired-up service and mock context."""
    plugin._service = service
    plugin._log = MagicMock()
    plugin._ctx = MagicMock()
    plugin._ctx.invoke_llm = AsyncMock(return_value="LLM generated summary")
    return plugin


# ---------------------------------------------------------------------------
# (a) memory_save → memory_search returns saved content
# ---------------------------------------------------------------------------


class TestSaveThenSearch:
    """(a) memory_save then memory_search returns the saved content."""

    @pytest.mark.asyncio
    async def test_save_then_search_returns_content(self, wired_plugin, mock_router, mock_store):
        """Save an insight then search — the saved content should appear."""
        # 1. Save — no duplicates exist
        mock_router.search = AsyncMock(return_value=[])
        save_result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "OAuth tokens require explicit scope re-request on refresh.",
                "tags": ["insight", "auth"],
                "topic": "authentication",
            }
        )
        assert save_result.get("success") is True
        assert save_result.get("action") == "created"
        saved_hash = save_result.get("chunk_hash")
        assert saved_hash  # non-empty

        # 2. Search — configure router to return the saved content
        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": "OAuth tokens require explicit scope re-request on refresh.",
                    "heading": "OAuth tokens",
                    "source": "",
                    "score": 0.92,
                    "weighted_score": 0.92,
                    "entry_type": "document",
                    "topic": "authentication",
                    "tags": '["insight", "auth"]',
                    "chunk_hash": saved_hash,
                    "_scope": "project",
                    "_scope_id": "test-project",
                    "_collection": "aq_project_test-project",
                }
            ]
        )

        search_result = await wired_plugin.cmd_memory_search(
            {
                "project_id": "test-project",
                "query": "OAuth token refresh scoping",
            }
        )
        assert search_result.get("success") is True
        assert search_result.get("count", 0) > 0
        first = search_result["results"][0]
        assert "OAuth tokens" in first["content"]
        assert first["score"] > 0.8
        assert first["chunk_hash"] == saved_hash

    @pytest.mark.asyncio
    async def test_save_then_search_preserves_topic(self, wired_plugin, mock_router):
        """Topic set at save time should appear in search results."""
        mock_router.search = AsyncMock(return_value=[])
        save_result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "proj-a",
                "content": "Use structlog for all async logging.",
                "topic": "logging",
            }
        )
        assert save_result.get("success") is True

        # Search returns entry with topic
        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": "Use structlog for all async logging.",
                    "heading": "",
                    "source": "",
                    "score": 0.95,
                    "weighted_score": 0.95,
                    "entry_type": "document",
                    "topic": "logging",
                    "tags": '["insight", "auto-generated"]',
                    "chunk_hash": "hash123",
                    "_scope": "project",
                    "_scope_id": "proj-a",
                    "_collection": "aq_project_proj-a",
                }
            ]
        )

        search_result = await wired_plugin.cmd_memory_search(
            {
                "project_id": "proj-a",
                "query": "logging best practices",
                "topic": "logging",
            }
        )
        assert search_result.get("success") is True
        assert search_result["results"][0]["topic"] == "logging"


# ---------------------------------------------------------------------------
# (b) memory_kv_set → memory_fact_recall retrieves exact value
# ---------------------------------------------------------------------------


class TestStoreThenRecall:
    """(b) memory_kv_set then memory_fact_recall retrieves exact value."""

    @pytest.mark.asyncio
    async def test_kv_set_then_fact_recall_returns_value(self, wired_plugin, mock_store):
        """Store a KV pair then recall it by key — exact value returned."""
        # 1. Store
        mock_store.set_kv.return_value = {
            "chunk_hash": "kv_hash_1",
            "kv_namespace": "project",
            "kv_key": "deploy_branch",
            "kv_value": '"main"',
            "updated_at": 2000,
            "tags": "[]",
            "source": "",
        }
        set_result = await wired_plugin.cmd_memory_kv_set(
            {
                "project_id": "test-project",
                "namespace": "project",
                "key": "deploy_branch",
                "value": "main",
            }
        )
        assert set_result.get("success") is True
        assert set_result["key"] == "deploy_branch"
        assert set_result["value"] == "main"

        # 2. Recall — service.kv_recall finds it in project scope
        mock_store.get_kv.return_value = {
            "kv_namespace": "project",
            "kv_key": "deploy_branch",
            "kv_value": '"main"',
            "updated_at": 2000,
            "tags": "[]",
            "source": "",
            "_scope": "project",
            "_scope_id": "test-project",
            "_collection": "aq_project_test-project",
        }
        recall_result = await wired_plugin.cmd_memory_fact_recall(
            {
                "key": "deploy_branch",
                "project_id": "test-project",
            }
        )
        assert recall_result.get("success") is True
        assert recall_result.get("found") is True
        assert recall_result["key"] == "deploy_branch"
        assert recall_result["value"] == "main"
        assert recall_result.get("resolved_scope") == "project"

    @pytest.mark.asyncio
    async def test_kv_set_then_recall_returns_structured_value(self, wired_plugin, mock_store):
        """KV values are JSON-encoded — recall should decode them properly."""
        mock_store.set_kv.return_value = {
            "chunk_hash": "kv_hash_2",
            "kv_namespace": "conventions",
            "kv_key": "line_length",
            "kv_value": "100",
            "updated_at": 3000,
            "tags": "[]",
            "source": "",
        }
        set_result = await wired_plugin.cmd_memory_kv_set(
            {
                "project_id": "test-project",
                "namespace": "conventions",
                "key": "line_length",
                "value": "100",
            }
        )
        assert set_result.get("success") is True

        mock_store.get_kv.return_value = {
            "kv_namespace": "conventions",
            "kv_key": "line_length",
            "kv_value": "100",
            "updated_at": 3000,
            "tags": "[]",
            "source": "",
            "_scope": "project",
            "_scope_id": "test-project",
            "_collection": "aq_project_test-project",
        }
        recall_result = await wired_plugin.cmd_memory_fact_recall(
            {
                "key": "line_length",
                "project_id": "test-project",
                "namespace": "conventions",
            }
        )
        assert recall_result.get("success") is True
        assert recall_result.get("found") is True
        assert recall_result["value"] == 100  # Decoded from JSON string "100"


# ---------------------------------------------------------------------------
# (c) memory_list returns all memories in scope with correct metadata
# ---------------------------------------------------------------------------


class TestListMemories:
    """(c) memory_list returns all memories in scope with correct metadata."""

    @pytest.mark.asyncio
    async def test_list_returns_entries_with_metadata(self, wired_plugin, mock_store):
        """Listing memories returns entries with expected metadata fields."""
        mock_store.query.return_value = [
            {
                "chunk_hash": "doc_hash_1",
                "entry_type": "document",
                "content": "# Authentication patterns\nUse JWT for stateless auth.",
                "heading": "Authentication patterns",
                "topic": "authentication",
                "tags": '["insight", "auth"]',
                "source": "task-42",
                "retrieval_count": 3,
                "updated_at": 5000,
            },
            {
                "chunk_hash": "doc_hash_2",
                "entry_type": "document",
                "content": "SQLite needs WAL mode for concurrent writes.",
                "heading": "SQLite WAL mode",
                "topic": "database",
                "tags": '["insight", "database"]',
                "source": "task-99",
                "retrieval_count": 1,
                "updated_at": 4000,
            },
        ]

        result = await wired_plugin.cmd_memory_list({"project_id": "test-project"})
        assert result.get("success") is True
        assert result["count"] == 2
        assert result["project_id"] == "test-project"
        assert result["scope"] == "project_test-project"

        # Check metadata fields on first entry
        e1 = result["entries"][0]
        assert e1["chunk_hash"] == "doc_hash_1"
        assert e1["title"] == "Authentication patterns"
        assert e1["topic"] == "authentication"
        assert e1["tags"] == ["insight", "auth"]
        assert e1["source"] == "task-42"
        assert e1["retrieval_count"] == 3
        assert e1["updated_at"] == 5000
        assert e1["entry_type"] == "document"
        assert "content_preview" in e1

    @pytest.mark.asyncio
    async def test_list_with_topic_filter(self, wired_plugin, mock_store):
        """Filtering by topic should be passed through to the service."""
        mock_store.query.return_value = [
            {
                "chunk_hash": "doc_hash_3",
                "entry_type": "document",
                "content": "Always use async database sessions.",
                "heading": "",
                "topic": "database",
                "tags": '["insight"]',
                "source": "",
                "retrieval_count": 0,
                "updated_at": 6000,
            },
        ]

        result = await wired_plugin.cmd_memory_list(
            {"project_id": "test-project", "topic": "database"}
        )
        assert result.get("success") is True
        assert result["count"] == 1
        assert result["filters"]["topic"] == "database"
        assert result["entries"][0]["topic"] == "database"

    @pytest.mark.asyncio
    async def test_list_empty_scope(self, wired_plugin, mock_store):
        """Listing an empty scope returns zero entries, not an error."""
        mock_store.query.return_value = []

        result = await wired_plugin.cmd_memory_list({"project_id": "empty-project"})
        assert result.get("success") is True
        assert result["count"] == 0
        assert result["entries"] == []

    @pytest.mark.asyncio
    async def test_list_pagination(self, wired_plugin, mock_store):
        """Offset and limit are forwarded and reflected in the response."""
        mock_store.query.return_value = []

        result = await wired_plugin.cmd_memory_list(
            {
                "project_id": "test-project",
                "offset": 10,
                "limit": 25,
            }
        )
        assert result.get("success") is True
        assert result["offset"] == 10
        assert result["limit"] == 25


# ---------------------------------------------------------------------------
# (d) memory_fact_list returns all temporal fact entries in scope/namespace
# ---------------------------------------------------------------------------


class TestListFacts:
    """(d) memory_fact_list returns all temporal fact entries."""

    @pytest.mark.asyncio
    async def test_fact_list_returns_entries(self, wired_plugin, mock_store):
        """Listing temporal facts returns properly formatted entries."""
        mock_store.list_temporal.return_value = [
            {
                "kv_key": "deploy_branch",
                "kv_value": '"main"',
                "valid_from": 1000,
                "valid_to": 0,
                "updated_at": 1000,
                "tags": "[]",
                "source": "",
            },
            {
                "kv_key": "python_version",
                "kv_value": '"3.12"',
                "valid_from": 2000,
                "valid_to": 0,
                "updated_at": 2000,
                "tags": "[]",
                "source": "",
            },
        ]

        result = await wired_plugin.cmd_memory_fact_list({"project_id": "test-project"})
        assert result.get("success") is True
        assert result["count"] == 2
        assert result["project_id"] == "test-project"
        assert result["current_only"] is True

        e1 = result["entries"][0]
        assert e1["key"] == "deploy_branch"
        assert e1["value"] == "main"
        assert e1["valid_from"] == 1000
        assert e1["valid_to"] == 0

        e2 = result["entries"][1]
        assert e2["key"] == "python_version"
        assert e2["value"] == "3.12"

    @pytest.mark.asyncio
    async def test_fact_list_with_namespace(self, wired_plugin, mock_store):
        """Namespace filter is forwarded in the request."""
        mock_store.list_temporal.return_value = []

        result = await wired_plugin.cmd_memory_fact_list(
            {"project_id": "test-project", "namespace": "settings"}
        )
        assert result.get("success") is True
        assert result["namespace"] == "settings"
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_fact_list_include_superseded(self, wired_plugin, mock_store):
        """Setting current_only=false includes superseded entries."""
        mock_store.list_temporal.return_value = [
            {
                "kv_key": "deploy_branch",
                "kv_value": '"main"',
                "valid_from": 2000,
                "valid_to": 0,
                "updated_at": 2000,
                "tags": "[]",
                "source": "",
            },
            {
                "kv_key": "deploy_branch",
                "kv_value": '"develop"',
                "valid_from": 1000,
                "valid_to": 2000,
                "updated_at": 1000,
                "tags": "[]",
                "source": "",
            },
        ]

        result = await wired_plugin.cmd_memory_fact_list(
            {"project_id": "test-project", "current_only": False}
        )
        assert result.get("success") is True
        assert result["current_only"] is False
        assert result["count"] == 2
        # Should include both current and superseded
        keys = [e["key"] for e in result["entries"]]
        assert keys.count("deploy_branch") == 2


# ---------------------------------------------------------------------------
# (e) memory_save with duplicate content ⇒ dedup (no second entry)
# ---------------------------------------------------------------------------


class TestSaveDedup:
    """(e) memory_save with duplicate content does not create a second entry."""

    @pytest.mark.asyncio
    async def test_save_near_identical_deduplicates(self, wired_plugin, mock_router, mock_store):
        """Saving content with > 0.95 similarity triggers dedup (timestamp update)."""
        # First save — no duplicates
        mock_store.search.return_value = []
        first_result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Always use async database sessions for SQLite.",
                "topic": "database",
            }
        )
        assert first_result.get("success") is True
        assert first_result.get("action") == "created"

        # Second save — store returns the first entry as near-identical
        mock_store.search.return_value = [
            {
                "content": "Always use async database sessions for SQLite.",
                "score": 0.98,
                "chunk_hash": first_result.get("chunk_hash", "hash_1"),
                "entry_type": "document",
                "topic": "database",
                "tags": '["insight", "auto-generated"]',
                "_scope": "project",
                "_scope_id": "test-project",
                "_collection": "aq_project_test-project",
            }
        ]
        second_result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Always use async database sessions for SQLite.",
                "topic": "database",
            }
        )
        assert second_result.get("success") is True
        assert second_result.get("action") == "deduplicated"
        assert second_result.get("similarity_score") == 0.98

    @pytest.mark.asyncio
    async def test_save_merge_related_content(self, wired_plugin, mock_router, mock_store):
        """Saving content with 0.8–0.95 similarity triggers merge."""
        mock_store.search.return_value = [
            {
                "content": "OAuth needs scope on refresh.",
                "score": 0.88,
                "chunk_hash": "existing_hash",
                "entry_type": "document",
                "topic": "authentication",
                "tags": '["insight"]',
                "_scope": "project",
                "_scope_id": "test-project",
                "_collection": "aq_project_test-project",
            }
        ]
        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "OAuth also needs to handle revoked tokens gracefully.",
                "tags": ["insight", "tokens"],
            }
        )
        assert result.get("success") is True
        assert result.get("action") == "merged"
        assert result.get("merged_with") == "existing_hash"

    @pytest.mark.asyncio
    async def test_dedup_filters_non_document_entries(self, wired_plugin, mock_router, mock_store):
        """Dedup check should only consider document entries, not KV or temporal."""
        # Store returns a high-similarity KV entry — should be ignored
        mock_store.search.return_value = [
            {
                "content": "deploy_branch=main",
                "score": 0.99,
                "chunk_hash": "kv_hash",
                "entry_type": "kv",
                "topic": "",
                "tags": "[]",
                "_scope": "project",
                "_scope_id": "test-project",
                "_collection": "aq_project_test-project",
            }
        ]
        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "deploy_branch=main is the default convention.",
            }
        )
        assert result.get("success") is True
        # Should create, not dedup, because the match is a KV not a document
        assert result.get("action") == "created"


# ---------------------------------------------------------------------------
# (f) memory_search with no results returns empty list (not error)
# ---------------------------------------------------------------------------


class TestSearchNoResults:
    """(f) memory_search with no results returns empty list, not error."""

    @pytest.mark.asyncio
    async def test_search_empty_results(self, wired_plugin, mock_router):
        """Searching with no matches returns success with empty results list."""
        mock_router.search = AsyncMock(return_value=[])

        result = await wired_plugin.cmd_memory_search(
            {
                "project_id": "test-project",
                "query": "quantum computing best practices",
            }
        )
        assert result.get("success") is True
        assert result["count"] == 0
        assert result["results"] == []
        # Must NOT have "error" key
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_search_empty_with_topic_filter(self, wired_plugin, mock_router):
        """Empty results when filtering by topic — still success, not error."""
        mock_router.search = AsyncMock(return_value=[])

        result = await wired_plugin.cmd_memory_search(
            {
                "project_id": "test-project",
                "query": "deployment strategy",
                "topic": "nonexistent-topic",
            }
        )
        assert result.get("success") is True
        assert result["count"] == 0
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_recall_no_kv_no_semantic(self, wired_plugin, mock_store, mock_router):
        """memory_recall with no KV hit and no semantic results returns success."""
        mock_store.get_kv.return_value = None
        mock_router.search = AsyncMock(return_value=[])

        result = await wired_plugin.cmd_memory_recall(
            {
                "query": "nonexistent_key",
                "project_id": "test-project",
            }
        )
        assert result.get("success") is True
        assert result["source"] == "semantic"
        assert result["count"] == 0
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_memory_get_no_results(self, wired_plugin, mock_store, mock_router):
        """memory_get with no matches returns success with empty results."""
        mock_store.get_kv.return_value = None
        mock_router.search = AsyncMock(return_value=[])

        result = await wired_plugin.cmd_memory_get(
            {
                "query": "nonexistent_key",
                "project_id": "test-project",
            }
        )
        assert result.get("success") is True
        assert result["count"] == 0
        assert result["results"] == []


# ---------------------------------------------------------------------------
# (g) memory_kv_set then overwrite same key then memory_fact_recall returns
#     latest value
# ---------------------------------------------------------------------------


class TestKVOverwrite:
    """(g) Store → overwrite → recall returns the latest value."""

    @pytest.mark.asyncio
    async def test_overwrite_returns_latest(self, wired_plugin, mock_store):
        """After overwriting a key, fact_recall should return the new value."""
        # 1. Initial set
        mock_store.set_kv.return_value = {
            "chunk_hash": "kv_v1",
            "kv_namespace": "project",
            "kv_key": "deploy_branch",
            "kv_value": '"main"',
            "updated_at": 1000,
            "tags": "[]",
            "source": "",
        }
        r1 = await wired_plugin.cmd_memory_kv_set(
            {
                "project_id": "test-project",
                "namespace": "project",
                "key": "deploy_branch",
                "value": "main",
            }
        )
        assert r1.get("success") is True
        assert r1["value"] == "main"

        # 2. Overwrite with new value
        mock_store.set_kv.return_value = {
            "chunk_hash": "kv_v2",
            "kv_namespace": "project",
            "kv_key": "deploy_branch",
            "kv_value": '"staging"',
            "updated_at": 2000,
            "tags": "[]",
            "source": "",
        }
        r2 = await wired_plugin.cmd_memory_kv_set(
            {
                "project_id": "test-project",
                "namespace": "project",
                "key": "deploy_branch",
                "value": "staging",
            }
        )
        assert r2.get("success") is True
        assert r2["value"] == "staging"

        # 3. Recall — should return the overwritten value
        mock_store.get_kv.return_value = {
            "kv_namespace": "project",
            "kv_key": "deploy_branch",
            "kv_value": '"staging"',
            "updated_at": 2000,
            "tags": "[]",
            "source": "",
            "_scope": "project",
            "_scope_id": "test-project",
            "_collection": "aq_project_test-project",
        }
        recall = await wired_plugin.cmd_memory_fact_recall(
            {
                "key": "deploy_branch",
                "project_id": "test-project",
                "namespace": "project",
            }
        )
        assert recall.get("success") is True
        assert recall.get("found") is True
        assert recall["value"] == "staging"  # Latest, not "main"

    @pytest.mark.asyncio
    async def test_overwrite_timestamp_advances(self, wired_plugin, mock_store):
        """After overwriting, the timestamp should be more recent."""
        # 1. Initial set
        mock_store.set_kv.return_value = {
            "chunk_hash": "kv_old",
            "kv_namespace": "conventions",
            "kv_key": "indent_style",
            "kv_value": '"spaces"',
            "updated_at": 1000,
            "tags": "[]",
            "source": "",
        }
        r1 = await wired_plugin.cmd_memory_kv_set(
            {
                "project_id": "test-project",
                "namespace": "conventions",
                "key": "indent_style",
                "value": "spaces",
            }
        )
        assert r1.get("success") is True
        ts1 = r1.get("updated_at", 0)

        # 2. Overwrite
        mock_store.set_kv.return_value = {
            "chunk_hash": "kv_new",
            "kv_namespace": "conventions",
            "kv_key": "indent_style",
            "kv_value": '"tabs"',
            "updated_at": 5000,
            "tags": "[]",
            "source": "",
        }
        r2 = await wired_plugin.cmd_memory_kv_set(
            {
                "project_id": "test-project",
                "namespace": "conventions",
                "key": "indent_style",
                "value": "tabs",
            }
        )
        assert r2.get("success") is True
        ts2 = r2.get("updated_at", 0)
        assert ts2 > ts1


# ---------------------------------------------------------------------------
# (h) All tools return well-formed response dicts with `success` field
# ---------------------------------------------------------------------------


class TestWellFormedResponses:
    """(h) All tools return dicts with `success` field on happy path."""

    @pytest.mark.asyncio
    async def test_memory_save_success_field(self, wired_plugin, mock_router):
        mock_router.search = AsyncMock(return_value=[])
        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Insight for success field check.",
            }
        )
        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_search_success_field(self, wired_plugin, mock_router):
        mock_router.search = AsyncMock(return_value=[])
        result = await wired_plugin.cmd_memory_search(
            {
                "project_id": "test-project",
                "query": "any query",
            }
        )
        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_list_success_field(self, wired_plugin, mock_store):
        mock_store.query.return_value = []
        result = await wired_plugin.cmd_memory_list({"project_id": "test-project"})
        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_kv_get_success_field(self, wired_plugin, mock_store):
        mock_store.get_kv.return_value = {
            "kv_namespace": "ns",
            "kv_key": "k",
            "kv_value": '"v"',
            "updated_at": 100,
            "tags": "[]",
            "source": "",
        }
        result = await wired_plugin.cmd_memory_kv_get(
            {
                "project_id": "test-project",
                "namespace": "ns",
                "key": "k",
            }
        )
        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_kv_set_success_field(self, wired_plugin, mock_store):
        mock_store.set_kv.return_value = {
            "chunk_hash": "h",
            "kv_namespace": "ns",
            "kv_key": "k",
            "kv_value": '"v"',
            "updated_at": 100,
            "tags": "[]",
            "source": "",
        }
        result = await wired_plugin.cmd_memory_kv_set(
            {
                "project_id": "test-project",
                "namespace": "ns",
                "key": "k",
                "value": "v",
            }
        )
        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_kv_list_success_field(self, wired_plugin, mock_store):
        mock_store.list_kv.return_value = []
        result = await wired_plugin.cmd_memory_kv_list(
            {
                "project_id": "test-project",
                "namespace": "project",
            }
        )
        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_fact_recall_success_field(self, wired_plugin, mock_store):
        mock_store.get_kv.return_value = None
        result = await wired_plugin.cmd_memory_fact_recall(
            {"key": "some_key", "project_id": "test-project"}
        )
        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_recall_success_field(self, wired_plugin, mock_store, mock_router):
        mock_store.get_kv.return_value = None
        mock_router.search = AsyncMock(return_value=[])
        result = await wired_plugin.cmd_memory_recall(
            {"query": "anything", "project_id": "test-project"}
        )
        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_get_success_field(self, wired_plugin, mock_store, mock_router):
        mock_store.get_kv.return_value = None
        mock_router.search = AsyncMock(return_value=[])
        result = await wired_plugin.cmd_memory_get(
            {"query": "anything", "project_id": "test-project"}
        )
        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_fact_list_success_field(self, wired_plugin, mock_store):
        mock_store.list_temporal.return_value = []
        result = await wired_plugin.cmd_memory_fact_list({"project_id": "test-project"})
        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_kv_get_not_found_still_success(self, wired_plugin, mock_store):
        """KV get returning no match should still have success=True, found=False."""
        mock_store.get_kv.return_value = None
        result = await wired_plugin.cmd_memory_kv_get(
            {
                "project_id": "test-project",
                "namespace": "ns",
                "key": "nonexistent",
            }
        )
        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["found"] is False


# ---------------------------------------------------------------------------
# Error handling — missing args and unavailable service
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Error paths return dicts with 'error' key (no exceptions leak)."""

    @pytest.mark.asyncio
    async def test_save_missing_project_id(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_save({"content": "test"})
        assert isinstance(result, dict)
        assert "error" in result
        assert "project_id" in result["error"]

    @pytest.mark.asyncio
    async def test_save_missing_content(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_save({"project_id": "proj"})
        assert isinstance(result, dict)
        assert "error" in result
        assert "content" in result["error"]

    @pytest.mark.asyncio
    async def test_search_missing_project_id(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_search({"query": "test"})
        assert isinstance(result, dict)
        assert "error" in result
        assert "project_id" in result["error"]

    @pytest.mark.asyncio
    async def test_search_missing_query(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_search({"project_id": "proj"})
        assert isinstance(result, dict)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_kv_set_missing_required_fields(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()

        # Missing namespace
        result = await plugin.cmd_memory_kv_set({"project_id": "p", "key": "k", "value": "v"})
        assert "error" in result
        assert "namespace" in result["error"]

        # Missing key
        result = await plugin.cmd_memory_kv_set(
            {"project_id": "p", "namespace": "ns", "value": "v"}
        )
        assert "error" in result
        assert "key" in result["error"]

        # Missing value
        result = await plugin.cmd_memory_kv_set({"project_id": "p", "namespace": "ns", "key": "k"})
        assert "error" in result
        assert "value" in result["error"]

    @pytest.mark.asyncio
    async def test_fact_recall_missing_key(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_fact_recall({"project_id": "proj"})
        assert isinstance(result, dict)
        assert "error" in result
        assert "key" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_missing_query(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_recall({"project_id": "proj"})
        assert isinstance(result, dict)
        assert "error" in result
        assert "query" in result["error"]

    @pytest.mark.asyncio
    async def test_get_missing_query(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_get({"project_id": "proj"})
        assert isinstance(result, dict)
        assert "error" in result
        assert "query" in result["error"]

    @pytest.mark.asyncio
    async def test_list_missing_project_id(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_list({})
        assert isinstance(result, dict)
        assert "error" in result
        assert "project_id" in result["error"]

    @pytest.mark.asyncio
    async def test_fact_list_missing_project_id(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_fact_list({})
        assert isinstance(result, dict)
        assert "error" in result
        assert "project_id" in result["error"]

    # -- Service unavailable ---

    @pytest.mark.asyncio
    async def test_save_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_save({"project_id": "proj", "content": "test"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_search_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_search({"project_id": "proj", "query": "test"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_kv_get_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_kv_get(
            {"project_id": "proj", "namespace": "ns", "key": "k"}
        )
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_kv_set_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_kv_set(
            {"project_id": "p", "namespace": "ns", "key": "k", "value": "v"}
        )
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_fact_recall_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_fact_recall({"key": "k", "project_id": "proj"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_recall({"query": "test", "project_id": "proj"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_get_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_get({"query": "test", "project_id": "proj"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_list_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_list({"project_id": "proj"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_fact_list_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_fact_list({"project_id": "proj"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_service_exception_returns_error_dict(self, wired_plugin, mock_router):
        """When the service raises an exception, the handler catches it
        and returns an error dict instead of propagating."""
        mock_router.search = AsyncMock(side_effect=RuntimeError("Milvus connection lost"))
        result = await wired_plugin.cmd_memory_search(
            {
                "project_id": "test-project",
                "query": "anything",
            }
        )
        assert isinstance(result, dict)
        assert "error" in result
        assert "success" not in result or result.get("success") is not True


# ---------------------------------------------------------------------------
# Cross-tool round-trip: memory_get auto-routing
# ---------------------------------------------------------------------------


class TestMemoryGetAutoRouting:
    """memory_get should auto-route between KV and semantic search."""

    @pytest.mark.asyncio
    async def test_get_kv_hit(self, wired_plugin, mock_store, mock_router):
        """When query matches a KV key, memory_get returns KV result."""
        mock_store.get_kv.return_value = {
            "kv_namespace": "project",
            "kv_key": "deploy_branch",
            "kv_value": '"main"',
            "updated_at": 1000,
            "tags": "[]",
            "source": "",
            "_scope": "project",
            "_scope_id": "test-project",
            "_collection": "aq_project_test-project",
        }
        result = await wired_plugin.cmd_memory_get(
            {
                "query": "deploy_branch",
                "project_id": "test-project",
            }
        )
        assert result.get("success") is True
        assert result["source"] == "kv"
        assert result["count"] >= 1
        assert result["results"][0]["value"] == "main"

    @pytest.mark.asyncio
    async def test_get_semantic_fallback(self, wired_plugin, mock_store, mock_router):
        """When no KV match, memory_get falls back to semantic search."""
        mock_store.get_kv.return_value = None
        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": "Use JWT tokens for stateless authentication.",
                    "heading": "",
                    "source": "",
                    "score": 0.85,
                    "weighted_score": 0.85,
                    "entry_type": "document",
                    "topic": "authentication",
                    "tags": '["insight"]',
                    "_scope": "project",
                    "_scope_id": "test-project",
                    "_collection": "aq_project_test-project",
                }
            ]
        )
        result = await wired_plugin.cmd_memory_get(
            {
                "query": "how does authentication work?",
                "project_id": "test-project",
            }
        )
        assert result.get("success") is True
        assert result["source"] == "semantic"
        assert result["count"] >= 1
        assert "JWT" in result["results"][0]["content"]


# ---------------------------------------------------------------------------
# Cross-tool round-trip: KV list after multiple sets
# ---------------------------------------------------------------------------


class TestKVListAfterSets:
    """KV list should reflect entries written via kv_set."""

    @pytest.mark.asyncio
    async def test_kv_list_returns_stored_entries(self, wired_plugin, mock_store):
        """After storing KV pairs, listing the namespace shows them."""
        # Store two entries
        for key, val in [("branch", "main"), ("format", "ruff")]:
            mock_store.set_kv.return_value = {
                "chunk_hash": f"h_{key}",
                "kv_namespace": "project",
                "kv_key": key,
                "kv_value": json.dumps(val),
                "updated_at": 1000,
                "tags": "[]",
                "source": "",
            }
            r = await wired_plugin.cmd_memory_kv_set(
                {
                    "project_id": "test-project",
                    "namespace": "project",
                    "key": key,
                    "value": val,
                }
            )
            assert r.get("success") is True

        # List — configure mock to return both
        mock_store.list_kv.return_value = [
            {
                "kv_namespace": "project",
                "kv_key": "branch",
                "kv_value": '"main"',
                "updated_at": 1000,
                "tags": "[]",
                "source": "",
            },
            {
                "kv_namespace": "project",
                "kv_key": "format",
                "kv_value": '"ruff"',
                "updated_at": 1000,
                "tags": "[]",
                "source": "",
            },
        ]
        result = await wired_plugin.cmd_memory_kv_list(
            {
                "project_id": "test-project",
                "namespace": "project",
            }
        )
        assert result.get("success") is True
        assert result["count"] == 2
        keys = {e["key"] for e in result["entries"]}
        assert "branch" in keys
        assert "format" in keys
