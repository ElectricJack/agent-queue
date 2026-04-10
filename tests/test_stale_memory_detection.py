"""Tests for stale memory detection (spec §6 — Roadmap 6.5.3).

Covers:
- ``find_stale()`` returns documents not retrieved in N days
- ``find_stale()`` returns documents never retrieved (last_retrieved == 0)
- Sort orders: staleness (default), created, retrieval_count
- Pagination via offset/limit
- Recently-retrieved documents are excluded
- ``cmd_memory_stale()`` plugin handler passes args correctly
- Edge cases: empty collection, all fresh, all stale
"""

from __future__ import annotations

import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

if sys.platform == "win32":
    pytest.skip("Milvus Lite not supported on Windows", allow_module_level=True)

from src.memory_v2_service import MemoryV2Service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_embedder():
    embedder = MagicMock()
    embedder.model_name = "test-model"
    embedder.dimension = 384
    embedder.embed = AsyncMock(return_value=[[0.1] * 384])
    return embedder


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.count.return_value = 0
    store.model_info = {"provider": "test", "model": "test-model", "dimension": 384}
    store.needs_reindex = False
    store.search.return_value = []
    store.query.return_value = []
    store.upsert.return_value = 1
    return store


@pytest.fixture
def mock_router(mock_store):
    router = MagicMock()
    router.get_store.return_value = mock_store
    router.list_collections.return_value = []
    router.search = AsyncMock(return_value=[])
    router.close = MagicMock()
    return router


@pytest.fixture
def service(mock_embedder, mock_router, tmp_path):
    svc = MemoryV2Service(
        milvus_uri="/tmp/test.db",
        embedding_provider="openai",
        data_dir=str(tmp_path),
    )
    svc._embedder = mock_embedder
    svc._router = mock_router
    svc._initialized = True
    return svc


# ---------------------------------------------------------------------------
# Helper: build fake document entries as returned by store.query()
# ---------------------------------------------------------------------------

def _make_doc(
    chunk_hash: str = "abc123",
    heading: str = "Test doc",
    topic: str = "",
    tags: str = "[]",
    content: str = "Some content",
    source: str = "/vault/test.md",
    retrieval_count: int = 0,
    last_retrieved: float = 0,
    updated_at: float | None = None,
    entry_type: str = "document",
) -> dict:
    return {
        "chunk_hash": chunk_hash,
        "heading": heading,
        "topic": topic,
        "tags": tags,
        "content": content,
        "source": source,
        "retrieval_count": retrieval_count,
        "last_retrieved": last_retrieved,
        "updated_at": updated_at or time.time(),
        "entry_type": entry_type,
    }


# ---------------------------------------------------------------------------
# Tests: find_stale — basic classification
# ---------------------------------------------------------------------------


class TestFindStaleClassification:
    """find_stale correctly classifies documents as stale or not."""

    async def test_never_retrieved_is_stale(self, service, mock_store):
        """Documents with last_retrieved == 0 are stale."""
        mock_store.query.return_value = [
            _make_doc(chunk_hash="never", last_retrieved=0),
        ]
        result = await service.find_stale("test-project")

        assert result["total_stale"] == 1
        assert result["never_retrieved_count"] == 1
        assert result["stale_retrieved_count"] == 0
        assert len(result["stale_documents"]) == 1
        assert result["stale_documents"][0]["reason"] == "never_retrieved"

    async def test_old_retrieval_is_stale(self, service, mock_store):
        """Documents retrieved > stale_days ago are stale."""
        old_time = time.time() - (60 * 86400)  # 60 days ago
        mock_store.query.return_value = [
            _make_doc(chunk_hash="old", last_retrieved=old_time, retrieval_count=3),
        ]
        result = await service.find_stale("test-project", stale_days=30)

        assert result["total_stale"] == 1
        assert result["never_retrieved_count"] == 0
        assert result["stale_retrieved_count"] == 1
        assert result["stale_documents"][0]["reason"] == "stale"
        assert result["stale_documents"][0]["days_since_retrieval"] >= 59

    async def test_recent_retrieval_not_stale(self, service, mock_store):
        """Documents retrieved within stale_days are NOT stale."""
        recent_time = time.time() - (5 * 86400)  # 5 days ago
        mock_store.query.return_value = [
            _make_doc(chunk_hash="fresh", last_retrieved=recent_time, retrieval_count=2),
        ]
        result = await service.find_stale("test-project", stale_days=30)

        assert result["total_stale"] == 0
        assert result["stale_documents"] == []

    async def test_mixed_stale_and_fresh(self, service, mock_store):
        """Only stale documents are returned, fresh ones excluded."""
        now = time.time()
        mock_store.query.return_value = [
            _make_doc(chunk_hash="never", last_retrieved=0),
            _make_doc(chunk_hash="old", last_retrieved=now - (45 * 86400)),
            _make_doc(chunk_hash="fresh", last_retrieved=now - (5 * 86400)),
            _make_doc(chunk_hash="barely_fresh", last_retrieved=now - (29 * 86400)),
        ]
        result = await service.find_stale("test-project", stale_days=30)

        assert result["total_stale"] == 2
        hashes = [d["chunk_hash"] for d in result["stale_documents"]]
        assert "never" in hashes
        assert "old" in hashes
        assert "fresh" not in hashes
        assert "barely_fresh" not in hashes

    async def test_custom_stale_days(self, service, mock_store):
        """Custom stale_days threshold works."""
        now = time.time()
        mock_store.query.return_value = [
            _make_doc(chunk_hash="a", last_retrieved=now - (10 * 86400)),
            _make_doc(chunk_hash="b", last_retrieved=now - (3 * 86400)),
        ]
        # With stale_days=7, 'a' (10 days) is stale but 'b' (3 days) is not
        result = await service.find_stale("test-project", stale_days=7)
        assert result["total_stale"] == 1
        assert result["stale_documents"][0]["chunk_hash"] == "a"
        assert result["threshold_days"] == 7

    async def test_null_last_retrieved_is_stale(self, service, mock_store):
        """Documents with last_retrieved == None are stale (never retrieved)."""
        mock_store.query.return_value = [
            _make_doc(chunk_hash="null_ret", last_retrieved=None),
        ]
        result = await service.find_stale("test-project")

        assert result["total_stale"] == 1
        assert result["never_retrieved_count"] == 1
        assert result["stale_documents"][0]["reason"] == "never_retrieved"
        assert result["stale_documents"][0]["last_retrieved_date"] is None
        assert result["stale_documents"][0]["days_since_retrieval"] is None


# ---------------------------------------------------------------------------
# Tests: find_stale — sort orders
# ---------------------------------------------------------------------------


class TestFindStaleSortOrders:
    """find_stale supports multiple sort orders."""

    async def test_staleness_sort_default(self, service, mock_store):
        """Default staleness sort: never-retrieved first, then oldest."""
        now = time.time()
        mock_store.query.return_value = [
            _make_doc(chunk_hash="old_60d", last_retrieved=now - (60 * 86400)),
            _make_doc(chunk_hash="never_a", last_retrieved=0),
            _make_doc(chunk_hash="old_45d", last_retrieved=now - (45 * 86400)),
            _make_doc(chunk_hash="never_b", last_retrieved=0),
        ]
        result = await service.find_stale("test-project")

        hashes = [d["chunk_hash"] for d in result["stale_documents"]]
        # Never-retrieved come first (in insertion order), then stale by oldest
        assert hashes[0] == "never_a"
        assert hashes[1] == "never_b"
        assert hashes[2] == "old_60d"  # 60 days ago is older
        assert hashes[3] == "old_45d"  # 45 days ago is more recent

    async def test_created_sort(self, service, mock_store):
        """Sort by created: oldest updated_at first."""
        now = time.time()
        mock_store.query.return_value = [
            _make_doc(
                chunk_hash="newer",
                last_retrieved=0,
                updated_at=now - (10 * 86400),
            ),
            _make_doc(
                chunk_hash="older",
                last_retrieved=0,
                updated_at=now - (30 * 86400),
            ),
        ]
        result = await service.find_stale("test-project", sort="created")

        hashes = [d["chunk_hash"] for d in result["stale_documents"]]
        assert hashes[0] == "older"
        assert hashes[1] == "newer"

    async def test_retrieval_count_sort(self, service, mock_store):
        """Sort by retrieval_count: least retrieved first."""
        now = time.time()
        mock_store.query.return_value = [
            _make_doc(
                chunk_hash="high",
                last_retrieved=now - (60 * 86400),
                retrieval_count=10,
            ),
            _make_doc(
                chunk_hash="low",
                last_retrieved=now - (60 * 86400),
                retrieval_count=1,
            ),
            _make_doc(chunk_hash="zero", last_retrieved=0, retrieval_count=0),
        ]
        result = await service.find_stale("test-project", sort="retrieval_count")

        hashes = [d["chunk_hash"] for d in result["stale_documents"]]
        assert hashes[0] == "zero"
        assert hashes[1] == "low"
        assert hashes[2] == "high"


# ---------------------------------------------------------------------------
# Tests: find_stale — pagination
# ---------------------------------------------------------------------------


class TestFindStalePagination:
    """find_stale supports pagination via offset/limit."""

    async def test_limit(self, service, mock_store):
        """limit caps the number of returned results."""
        mock_store.query.return_value = [
            _make_doc(chunk_hash=f"doc_{i}", last_retrieved=0)
            for i in range(10)
        ]
        result = await service.find_stale("test-project", limit=3)

        assert result["total_stale"] == 10
        assert len(result["stale_documents"]) == 3
        assert result["limit"] == 3

    async def test_offset(self, service, mock_store):
        """offset skips the first N results."""
        mock_store.query.return_value = [
            _make_doc(chunk_hash=f"doc_{i}", last_retrieved=0)
            for i in range(10)
        ]
        result = await service.find_stale("test-project", offset=7, limit=50)

        assert result["total_stale"] == 10
        assert len(result["stale_documents"]) == 3
        assert result["offset"] == 7

    async def test_max_limit_capped_at_200(self, service, mock_store):
        """limit is capped at 200."""
        mock_store.query.return_value = [
            _make_doc(chunk_hash=f"doc_{i}", last_retrieved=0)
            for i in range(5)
        ]
        result = await service.find_stale("test-project", limit=500)
        assert result["limit"] == 200


# ---------------------------------------------------------------------------
# Tests: find_stale — document fields
# ---------------------------------------------------------------------------


class TestFindStaleDocumentFields:
    """Stale documents include all archival-relevant metadata."""

    async def test_stale_doc_fields(self, service, mock_store):
        """Each stale document has all expected fields."""
        now = time.time()
        mock_store.query.return_value = [
            _make_doc(
                chunk_hash="abc123",
                heading="Test insight",
                topic="architecture",
                tags='["insight", "pattern"]',
                content="This is a test insight about architecture",
                source="/vault/projects/test/memory/insights/test.md",
                retrieval_count=5,
                last_retrieved=now - (45 * 86400),
                updated_at=now - (90 * 86400),
            ),
        ]
        result = await service.find_stale("test-project")

        doc = result["stale_documents"][0]
        assert doc["chunk_hash"] == "abc123"
        assert doc["title"] == "Test insight"
        assert doc["topic"] == "architecture"
        assert doc["tags"] == ["insight", "pattern"]
        assert doc["source"] == "/vault/projects/test/memory/insights/test.md"
        assert doc["retrieval_count"] == 5
        assert doc["last_retrieved_date"] is not None
        assert doc["days_since_retrieval"] >= 44
        assert doc["created_date"] is not None
        assert "test insight" in doc["content_preview"].lower()
        assert doc["reason"] == "stale"

    async def test_never_retrieved_doc_fields(self, service, mock_store):
        """Never-retrieved docs have None for retrieval date and days."""
        mock_store.query.return_value = [
            _make_doc(chunk_hash="never", last_retrieved=0),
        ]
        result = await service.find_stale("test-project")

        doc = result["stale_documents"][0]
        assert doc["last_retrieved_date"] is None
        assert doc["days_since_retrieval"] is None
        assert doc["reason"] == "never_retrieved"

    async def test_content_preview_truncated(self, service, mock_store):
        """Long content is truncated to 200 chars with ellipsis."""
        long_content = "A" * 300
        mock_store.query.return_value = [
            _make_doc(chunk_hash="long", content=long_content, last_retrieved=0),
        ]
        result = await service.find_stale("test-project")

        preview = result["stale_documents"][0]["content_preview"]
        assert len(preview) == 201  # 200 chars + "…"
        assert preview.endswith("…")

    async def test_title_fallback_from_content(self, service, mock_store):
        """When heading is empty, title is extracted from content."""
        mock_store.query.return_value = [
            _make_doc(
                chunk_hash="no_heading",
                heading="",
                content="# My Important Insight\nDetails here",
                last_retrieved=0,
            ),
        ]
        result = await service.find_stale("test-project")

        assert result["stale_documents"][0]["title"] == "My Important Insight"

    async def test_tags_decoded_from_json_string(self, service, mock_store):
        """Tags are decoded from JSON string format."""
        mock_store.query.return_value = [
            _make_doc(
                chunk_hash="tagged",
                tags='["insight", "stale"]',
                last_retrieved=0,
            ),
        ]
        result = await service.find_stale("test-project")
        assert result["stale_documents"][0]["tags"] == ["insight", "stale"]

    async def test_malformed_tags_default_to_empty(self, service, mock_store):
        """Malformed tags JSON defaults to empty list."""
        mock_store.query.return_value = [
            _make_doc(chunk_hash="bad_tags", tags="not valid json", last_retrieved=0),
        ]
        result = await service.find_stale("test-project")
        assert result["stale_documents"][0]["tags"] == []


# ---------------------------------------------------------------------------
# Tests: find_stale — result metadata
# ---------------------------------------------------------------------------


class TestFindStaleResultMetadata:
    """find_stale result includes collection and threshold metadata."""

    async def test_result_has_collection_info(self, service, mock_store):
        """Result includes collection, scope, and scope_id."""
        mock_store.query.return_value = []
        result = await service.find_stale("test-project")

        assert "collection" in result
        assert "scope" in result
        assert "scope_id" in result

    async def test_result_has_threshold_info(self, service, mock_store):
        """Result includes threshold_days and threshold_date."""
        mock_store.query.return_value = []
        result = await service.find_stale("test-project", stale_days=14)

        assert result["threshold_days"] == 14
        assert result["threshold_date"] is not None

    async def test_result_has_counts(self, service, mock_store):
        """Result includes total_stale, never_retrieved, stale_retrieved counts."""
        now = time.time()
        mock_store.query.return_value = [
            _make_doc(chunk_hash="never", last_retrieved=0),
            _make_doc(chunk_hash="old", last_retrieved=now - (60 * 86400)),
        ]
        result = await service.find_stale("test-project")

        assert result["total_stale"] == 2
        assert result["never_retrieved_count"] == 1
        assert result["stale_retrieved_count"] == 1


# ---------------------------------------------------------------------------
# Tests: find_stale — edge cases
# ---------------------------------------------------------------------------


class TestFindStaleEdgeCases:
    """Edge cases for find_stale."""

    async def test_empty_collection(self, service, mock_store):
        """Empty collection returns zero stale documents."""
        mock_store.query.return_value = []
        result = await service.find_stale("test-project")

        assert result["total_stale"] == 0
        assert result["stale_documents"] == []
        assert result["never_retrieved_count"] == 0
        assert result["stale_retrieved_count"] == 0

    async def test_all_fresh(self, service, mock_store):
        """All recently-retrieved documents → zero stale."""
        now = time.time()
        mock_store.query.return_value = [
            _make_doc(chunk_hash="a", last_retrieved=now - (1 * 86400)),
            _make_doc(chunk_hash="b", last_retrieved=now - (10 * 86400)),
        ]
        result = await service.find_stale("test-project", stale_days=30)

        assert result["total_stale"] == 0

    async def test_all_stale(self, service, mock_store):
        """All documents are stale."""
        now = time.time()
        mock_store.query.return_value = [
            _make_doc(chunk_hash="a", last_retrieved=0),
            _make_doc(chunk_hash="b", last_retrieved=now - (90 * 86400)),
        ]
        result = await service.find_stale("test-project")

        assert result["total_stale"] == 2

    async def test_service_unavailable(self, service, mock_store):
        """Returns error when service is unavailable."""
        service._initialized = False
        service._router = None
        result = await service.find_stale("test-project")
        assert "error" in result

    async def test_only_document_type_filtered(self, service, mock_store):
        """Only document-type entries are considered (not kv, temporal)."""
        mock_store.query.return_value = [
            _make_doc(chunk_hash="doc", last_retrieved=0, entry_type="document"),
        ]
        result = await service.find_stale("test-project")

        # Should only get the documents the store returns for the filter
        assert result["total_stale"] == 1

    async def test_boundary_exactly_at_threshold(self, service, mock_store):
        """Document retrieved exactly at threshold boundary is stale."""
        now = time.time()
        # Exactly 30 days ago (plus a tiny bit to ensure it's past)
        boundary = now - (30 * 86400) - 1
        mock_store.query.return_value = [
            _make_doc(chunk_hash="boundary", last_retrieved=boundary),
        ]
        result = await service.find_stale("test-project", stale_days=30)
        assert result["total_stale"] == 1


# ---------------------------------------------------------------------------
# Tests: plugin cmd_memory_stale handler
# ---------------------------------------------------------------------------


class TestCmdMemoryStale:
    """Tests for the plugin's cmd_memory_stale command handler."""

    @pytest.fixture
    def plugin(self, service):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        ctx = MagicMock()
        ctx.get_service = MagicMock(return_value=service)
        plugin = MemoryV2Plugin.__new__(MemoryV2Plugin)
        plugin._service = service
        plugin._log = MagicMock()
        return plugin

    async def test_requires_project_id(self, plugin):
        """Returns error when project_id is missing."""
        result = await plugin.cmd_memory_stale({})
        assert "error" in result
        assert "project_id" in result["error"]

    async def test_passes_args_to_service(self, plugin, mock_store):
        """All args are forwarded to find_stale."""
        mock_store.query.return_value = []
        result = await plugin.cmd_memory_stale({
            "project_id": "test-proj",
            "stale_days": 14,
            "sort": "created",
            "offset": 10,
            "limit": 25,
        })
        assert result["success"] is True
        assert result["threshold_days"] == 14

    async def test_returns_success_with_stale_docs(self, plugin, mock_store):
        """Returns success with stale document data."""
        mock_store.query.return_value = [
            _make_doc(chunk_hash="stale_one", last_retrieved=0),
        ]
        result = await plugin.cmd_memory_stale({"project_id": "test-proj"})

        assert result["success"] is True
        assert result["total_stale"] == 1
        assert len(result["stale_documents"]) == 1

    async def test_service_unavailable_returns_error(self, plugin):
        """Returns error when service is unavailable."""
        plugin._service = None
        result = await plugin.cmd_memory_stale({"project_id": "test-proj"})
        assert "error" in result

    async def test_default_args(self, plugin, mock_store):
        """Uses default stale_days=30, sort=staleness, offset=0, limit=50."""
        mock_store.query.return_value = []
        result = await plugin.cmd_memory_stale({"project_id": "test-proj"})

        assert result["success"] is True
        assert result["threshold_days"] == 30
        assert result["offset"] == 0
        assert result["limit"] == 50


# ---------------------------------------------------------------------------
# Tests: memory_stale tool registration
# ---------------------------------------------------------------------------


class TestMemoryStaleToolRegistration:
    """memory_stale is properly registered as a v2 tool."""

    def test_in_v2_only_tools(self):
        """memory_stale is in the V2_ONLY_TOOLS set."""
        from src.plugins.internal.memory_v2 import V2_ONLY_TOOLS

        assert "memory_stale" in V2_ONLY_TOOLS

    def test_tool_definition_exists(self):
        """memory_stale has a tool definition in TOOL_DEFINITIONS."""
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        stale_tools = [t for t in TOOL_DEFINITIONS if t["name"] == "memory_stale"]
        assert len(stale_tools) == 1

        tool = stale_tools[0]
        assert "input_schema" in tool
        props = tool["input_schema"]["properties"]
        assert "project_id" in props
        assert "stale_days" in props
        assert "sort" in props
        assert "offset" in props
        assert "limit" in props
        assert tool["input_schema"]["required"] == ["project_id"]
