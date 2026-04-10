"""Tests for memory health command (spec §6 — Memory Health View, Roadmap 6.5.2).

Covers:
- ``MemoryV2Service.health()`` computing all six metrics:
  collection sizes, growth rate, stale count, most-retrieved, hit rate, contradictions.
- ``MemoryV2Plugin.cmd_memory_health()`` command handler.
- ``store.query(track=False)`` does not inflate retrieval stats.
- Edge cases: empty collections, all-stale, all-retrieved, no documents.
"""

from __future__ import annotations

import json
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


def _make_doc(
    chunk_hash: str,
    *,
    heading: str = "",
    topic: str = "",
    tags: list[str] | None = None,
    retrieval_count: int = 0,
    last_retrieved: int = 0,
    updated_at: int = 0,
    entry_type: str = "document",
) -> dict:
    """Helper to build a mock Milvus entry."""
    return {
        "chunk_hash": chunk_hash,
        "entry_type": entry_type,
        "heading": heading,
        "topic": topic,
        "tags": json.dumps(tags or []),
        "retrieval_count": retrieval_count,
        "last_retrieved": last_retrieved,
        "updated_at": updated_at,
        "content": f"Content for {chunk_hash}",
        "source": f"/vault/insights/{chunk_hash}.md",
    }


# ---------------------------------------------------------------------------
# MemoryV2Service.health() — collection sizes
# ---------------------------------------------------------------------------


class TestHealthCollectionSizes:
    """health() should report entry counts by type."""

    async def test_empty_collection(self, service, mock_store):
        """Empty collection returns all-zero sizes."""
        mock_store.query.return_value = []

        result = await service.health("test-project")

        assert result["sizes"]["total"] == 0
        assert result["sizes"]["documents"] == 0
        assert result["sizes"]["kv_entries"] == 0
        assert result["sizes"]["temporal_entries"] == 0

    async def test_mixed_types(self, service, mock_store):
        """Correctly counts documents, kv, and temporal entries."""
        entries = [
            _make_doc("doc1", entry_type="document"),
            _make_doc("doc2", entry_type="document"),
            _make_doc("doc3", entry_type="document"),
            _make_doc("kv1", entry_type="kv"),
            _make_doc("kv2", entry_type="kv"),
            _make_doc("temp1", entry_type="temporal"),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        assert result["sizes"]["total"] == 6
        assert result["sizes"]["documents"] == 3
        assert result["sizes"]["kv_entries"] == 2
        assert result["sizes"]["temporal_entries"] == 1


# ---------------------------------------------------------------------------
# MemoryV2Service.health() — growth rate
# ---------------------------------------------------------------------------


class TestHealthGrowthRate:
    """health() should count documents created in the last 7 days."""

    async def test_no_recent_docs(self, service, mock_store):
        """No documents created in last 7 days → 0 growth."""
        old_ts = int(time.time()) - (30 * 86400)  # 30 days ago
        entries = [
            _make_doc("doc1", updated_at=old_ts),
            _make_doc("doc2", updated_at=old_ts),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        assert result["growth_rate"]["new_documents_7d"] == 0
        assert result["growth_rate"]["period_days"] == 7

    async def test_recent_docs(self, service, mock_store):
        """Documents with updated_at in last 7 days are counted."""
        now = int(time.time())
        entries = [
            _make_doc("doc1", updated_at=now - 3600),        # 1 hour ago
            _make_doc("doc2", updated_at=now - 86400),       # 1 day ago
            _make_doc("doc3", updated_at=now - (8 * 86400)), # 8 days ago — excluded
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        assert result["growth_rate"]["new_documents_7d"] == 2

    async def test_kv_entries_excluded_from_growth(self, service, mock_store):
        """Only documents count toward growth rate, not KV/temporal."""
        now = int(time.time())
        entries = [
            _make_doc("doc1", updated_at=now - 3600),
            _make_doc("kv1", entry_type="kv", updated_at=now - 3600),
            _make_doc("temp1", entry_type="temporal", updated_at=now - 3600),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        assert result["growth_rate"]["new_documents_7d"] == 1


# ---------------------------------------------------------------------------
# MemoryV2Service.health() — stale count
# ---------------------------------------------------------------------------


class TestHealthStaleCount:
    """health() should count documents not retrieved in stale_days."""

    async def test_never_retrieved_is_stale(self, service, mock_store):
        """Documents with last_retrieved=0 are stale."""
        entries = [
            _make_doc("doc1", last_retrieved=0),
            _make_doc("doc2", last_retrieved=0),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        assert result["stale_count"] == 2

    async def test_recently_retrieved_not_stale(self, service, mock_store):
        """Documents retrieved within stale_days are not stale."""
        now = int(time.time())
        entries = [
            _make_doc("doc1", last_retrieved=now - 3600),  # 1 hour ago
            _make_doc("doc2", last_retrieved=now - (2 * 86400)),  # 2 days ago
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project", stale_days=30)

        assert result["stale_count"] == 0

    async def test_old_retrieval_is_stale(self, service, mock_store):
        """Documents last retrieved more than stale_days ago are stale."""
        now = int(time.time())
        entries = [
            _make_doc("doc1", last_retrieved=now - (31 * 86400)),  # 31 days ago
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project", stale_days=30)

        assert result["stale_count"] == 1

    async def test_custom_stale_days(self, service, mock_store):
        """Custom stale_days threshold is respected."""
        now = int(time.time())
        entries = [
            _make_doc("doc1", last_retrieved=now - (8 * 86400)),  # 8 days ago
        ]
        mock_store.query.return_value = entries

        # 7-day threshold → stale
        result = await service.health("test-project", stale_days=7)
        assert result["stale_count"] == 1
        assert result["stale_days_threshold"] == 7

        # 14-day threshold → not stale
        result = await service.health("test-project", stale_days=14)
        assert result["stale_count"] == 0


# ---------------------------------------------------------------------------
# MemoryV2Service.health() — most-retrieved
# ---------------------------------------------------------------------------


class TestHealthMostRetrieved:
    """health() should return top N documents by retrieval_count."""

    async def test_top_n_ordering(self, service, mock_store):
        """Most-retrieved ordered by retrieval_count descending."""
        entries = [
            _make_doc("doc1", retrieval_count=5, heading="Low"),
            _make_doc("doc2", retrieval_count=20, heading="High"),
            _make_doc("doc3", retrieval_count=10, heading="Mid"),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        mr = result["most_retrieved"]
        assert len(mr) == 3
        assert mr[0]["heading"] == "High"
        assert mr[0]["retrieval_count"] == 20
        assert mr[1]["heading"] == "Mid"
        assert mr[1]["retrieval_count"] == 10
        assert mr[2]["heading"] == "Low"
        assert mr[2]["retrieval_count"] == 5

    async def test_top_n_limit(self, service, mock_store):
        """top_n limits the number of returned most-retrieved."""
        entries = [
            _make_doc(f"doc{i}", retrieval_count=i, heading=f"Doc {i}")
            for i in range(1, 6)
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project", top_n=2)

        mr = result["most_retrieved"]
        assert len(mr) == 2
        assert mr[0]["retrieval_count"] == 5
        assert mr[1]["retrieval_count"] == 4

    async def test_zero_retrievals_excluded(self, service, mock_store):
        """Documents with retrieval_count=0 are excluded from most_retrieved."""
        entries = [
            _make_doc("doc1", retrieval_count=0),
            _make_doc("doc2", retrieval_count=3),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        mr = result["most_retrieved"]
        assert len(mr) == 1
        assert mr[0]["chunk_hash"] == "doc2"

    async def test_includes_metadata(self, service, mock_store):
        """Most-retrieved entries include heading, topic, last_retrieved."""
        now = int(time.time())
        entries = [
            _make_doc(
                "doc1",
                retrieval_count=7,
                heading="Auth patterns",
                topic="authentication",
                last_retrieved=now,
            ),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        mr = result["most_retrieved"]
        assert len(mr) == 1
        assert mr[0]["chunk_hash"] == "doc1"
        assert mr[0]["heading"] == "Auth patterns"
        assert mr[0]["topic"] == "authentication"
        assert mr[0]["last_retrieved"] == now


# ---------------------------------------------------------------------------
# MemoryV2Service.health() — hit rate
# ---------------------------------------------------------------------------


class TestHealthHitRate:
    """health() should compute retrieval hit rate."""

    async def test_no_documents(self, service, mock_store):
        """Empty collection → 0.0 hit rate."""
        mock_store.query.return_value = []

        result = await service.health("test-project")

        assert result["hit_rate"] == 0.0
        assert result["hit_rate_pct"] == "0.0%"

    async def test_all_retrieved(self, service, mock_store):
        """All documents retrieved → 1.0 hit rate."""
        entries = [
            _make_doc("doc1", retrieval_count=3),
            _make_doc("doc2", retrieval_count=1),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        assert result["hit_rate"] == 1.0
        assert result["hit_rate_pct"] == "100.0%"
        assert result["documents_retrieved"] == 2
        assert result["documents_never_retrieved"] == 0

    async def test_partial_retrieval(self, service, mock_store):
        """Some documents retrieved → fractional hit rate."""
        entries = [
            _make_doc("doc1", retrieval_count=5),
            _make_doc("doc2", retrieval_count=0),
            _make_doc("doc3", retrieval_count=0),
            _make_doc("doc4", retrieval_count=2),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        assert result["hit_rate"] == 0.5
        assert result["hit_rate_pct"] == "50.0%"
        assert result["documents_retrieved"] == 2
        assert result["documents_never_retrieved"] == 2

    async def test_kv_entries_excluded_from_hit_rate(self, service, mock_store):
        """Only documents count toward hit rate, not KV/temporal."""
        entries = [
            _make_doc("doc1", retrieval_count=1),
            _make_doc("kv1", entry_type="kv", retrieval_count=0),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        # 1 doc retrieved out of 1 doc total = 100%
        assert result["hit_rate"] == 1.0


# ---------------------------------------------------------------------------
# MemoryV2Service.health() — contradictions
# ---------------------------------------------------------------------------


class TestHealthContradictions:
    """health() should detect documents tagged #contested."""

    async def test_no_contested(self, service, mock_store):
        """No contested tags → empty contradictions."""
        entries = [
            _make_doc("doc1", tags=["insight", "auto-generated"]),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        assert result["contradictions"] == []
        assert result["contradiction_count"] == 0

    async def test_contested_detected(self, service, mock_store):
        """Documents with 'contested' tag appear in contradictions."""
        entries = [
            _make_doc("doc1", tags=["insight", "contested"]),
            _make_doc("doc2", tags=["auto-generated"]),
            _make_doc("doc3", tags=["contested", "needs-review"]),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        assert result["contradiction_count"] == 2
        hashes = {c["chunk_hash"] for c in result["contradictions"]}
        assert hashes == {"doc1", "doc3"}

    async def test_contested_includes_metadata(self, service, mock_store):
        """Contradiction entries include heading, topic, tags."""
        entries = [
            _make_doc(
                "doc1",
                tags=["contested", "insight"],
                heading="Conflicting auth approach",
                topic="authentication",
            ),
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        assert len(result["contradictions"]) == 1
        c = result["contradictions"][0]
        assert c["heading"] == "Conflicting auth approach"
        assert c["topic"] == "authentication"
        assert "contested" in c["tags"]

    async def test_malformed_tags_handled(self, service, mock_store):
        """Malformed tags JSON doesn't crash."""
        entries = [
            {
                "chunk_hash": "doc1",
                "entry_type": "document",
                "heading": "",
                "topic": "",
                "tags": "not valid json{",
                "retrieval_count": 0,
                "last_retrieved": 0,
                "updated_at": 0,
                "content": "test",
                "source": "/vault/doc1.md",
            },
        ]
        mock_store.query.return_value = entries

        result = await service.health("test-project")

        # Should not crash, just skip the malformed entry
        assert result["contradiction_count"] == 0


# ---------------------------------------------------------------------------
# MemoryV2Service.health() — scope & metadata
# ---------------------------------------------------------------------------


class TestHealthMetadata:
    """health() should include collection/scope info."""

    async def test_returns_collection_info(self, service, mock_store):
        """Result includes collection name, scope, scope_id."""
        mock_store.query.return_value = []

        result = await service.health("test-project")

        assert "collection" in result
        assert "scope" in result
        assert "scope_id" in result

    async def test_unavailable_service(self, service, mock_store):
        """Returns error when service is not available."""
        service._initialized = False

        result = await service.health("test-project")

        assert "error" in result


# ---------------------------------------------------------------------------
# MemoryV2Service.health() — query uses track=False
# ---------------------------------------------------------------------------


class TestHealthNoTracking:
    """health() queries should NOT inflate retrieval counts."""

    async def test_query_called_with_track_false(self, service, mock_store):
        """store.query() is called with track=False."""
        mock_store.query.return_value = []

        await service.health("test-project")

        mock_store.query.assert_called_once()
        call_kwargs = mock_store.query.call_args
        assert call_kwargs.kwargs.get("track") is False


# ---------------------------------------------------------------------------
# MemoryV2Service.stats() — now uses track=False
# ---------------------------------------------------------------------------


class TestStatsNoTracking:
    """stats() should not inflate retrieval counts."""

    async def test_stats_queries_use_track_false(self, service, mock_store):
        """stats() calls store.query() with track=False."""
        mock_store.query.return_value = []

        await service.stats("test-project")

        # stats() makes 3 queries (doc, kv, temporal)
        assert mock_store.query.call_count == 3
        for call in mock_store.query.call_args_list:
            assert call.kwargs.get("track") is False


# ---------------------------------------------------------------------------
# Plugin: cmd_memory_health
# ---------------------------------------------------------------------------


class TestPluginCmdMemoryHealth:
    """cmd_memory_health handler validation and delegation."""

    @pytest.fixture
    def plugin(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        p = MemoryV2Plugin()
        p._log = MagicMock()
        p._service = MagicMock()
        p._service.available = True
        p._service.health = AsyncMock(
            return_value={
                "collection": "aq_project_test",
                "scope": "project",
                "scope_id": "test",
                "sizes": {"total": 10, "documents": 8, "kv_entries": 1, "temporal_entries": 1},
                "growth_rate": {"new_documents_7d": 3, "period_days": 7},
                "stale_count": 2,
                "stale_days_threshold": 30,
                "most_retrieved": [],
                "hit_rate": 0.75,
                "hit_rate_pct": "75.0%",
                "documents_retrieved": 6,
                "documents_never_retrieved": 2,
                "contradictions": [],
                "contradiction_count": 0,
            }
        )
        return p

    async def test_missing_project_id(self, plugin):
        """Returns error when project_id is missing."""
        result = await plugin.cmd_memory_health({})
        assert "error" in result
        assert "project_id" in result["error"]

    async def test_service_unavailable(self, plugin):
        """Returns error when service is unavailable."""
        plugin._service.available = False
        result = await plugin.cmd_memory_health({"project_id": "test"})
        assert "error" in result

    async def test_success(self, plugin):
        """Successful health check returns all metrics."""
        result = await plugin.cmd_memory_health({"project_id": "test"})

        assert result["success"] is True
        assert result["sizes"]["total"] == 10
        assert result["growth_rate"]["new_documents_7d"] == 3
        assert result["stale_count"] == 2
        assert result["hit_rate"] == 0.75
        assert result["contradiction_count"] == 0

    async def test_custom_args_passed(self, plugin):
        """Custom stale_days and top_n are forwarded to service."""
        await plugin.cmd_memory_health({
            "project_id": "test",
            "stale_days": 14,
            "top_n": 5,
        })

        plugin._service.health.assert_called_once_with(
            "test",
            scope=None,
            stale_days=14,
            top_n=5,
        )

    async def test_scope_forwarded(self, plugin):
        """Scope argument is forwarded to service."""
        await plugin.cmd_memory_health({
            "project_id": "test",
            "scope": "system",
        })

        plugin._service.health.assert_called_once_with(
            "test",
            scope="system",
            stale_days=30,
            top_n=10,
        )

    async def test_service_error(self, plugin):
        """Service exceptions are caught and returned as error dict."""
        plugin._service.health = AsyncMock(side_effect=RuntimeError("connection lost"))

        result = await plugin.cmd_memory_health({"project_id": "test"})

        assert "error" in result
        assert "connection lost" in result["error"]

    async def test_service_returns_error(self, plugin):
        """Service returning error dict passes through without success."""
        plugin._service.health = AsyncMock(
            return_value={"error": "MemoryV2Service not available"}
        )

        result = await plugin.cmd_memory_health({"project_id": "test"})

        assert "error" in result
        assert "success" not in result


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


class TestHealthToolRegistration:
    """memory_health should be in V2_ONLY_TOOLS and TOOL_DEFINITIONS."""

    def test_in_v2_only_tools(self):
        from src.plugins.internal.memory_v2 import V2_ONLY_TOOLS

        assert "memory_health" in V2_ONLY_TOOLS

    def test_in_tool_definitions(self):
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "memory_health" in names

    def test_tool_schema_has_required_fields(self):
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "memory_health")
        schema = tool["input_schema"]
        assert "project_id" in schema["properties"]
        assert "stale_days" in schema["properties"]
        assert "top_n" in schema["properties"]
        assert "scope" in schema["properties"]
        assert schema["required"] == ["project_id"]
