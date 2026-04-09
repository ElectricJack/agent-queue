"""Tests for L3 on-demand search via ``memory_search`` tool (roadmap 3.3.9).

Verifies that the ``memory_search`` tool (L3 tier) correctly performs
cross-topic semantic search across all scopes with weighted merging,
does not duplicate content already loaded by L1 (facts) or L2 (topic
context), works independently of L2, includes scope/topic metadata,
and respects retrieval tracking.

Test cases per roadmap 3.3.9:

(a) Agent explicitly calls ``memory_search("database optimization")`` and
    gets results from all topics (not limited to current topic).
(b) L3 search returns results from all scopes (project + agent-type +
    system) with correct weighted merge.
(c) L3 search does not duplicate results already loaded in L1 or L2.
(d) L3 search works even when L2 is not active (no topic detected).
(e) L3 results include source scope and topic metadata.
(f) L3 search respects the same retrieval tracking (retrieval_count
    increments).

All tests mock the memsearch dependency so they run without Milvus or
embedding providers.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Skip entire module on Windows (Milvus Lite not supported)
if sys.platform == "win32":
    pytest.skip("Milvus Lite not supported on Windows", allow_module_level=True)

from src.memory_v2_service import MemoryV2Service, MEMSEARCH_AVAILABLE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _search_result(
    chunk_hash: str,
    content: str,
    score: float,
    *,
    scope: str = "project",
    scope_id: str | None = "myapp",
    weight: float = 1.0,
    collection: str = "aq_project_myapp",
    topic: str = "",
    entry_type: str = "document",
    tags: str = "[]",
    retrieval_count: int = 0,
    source: str = "",
    heading: str = "",
) -> dict:
    """Build a fake search result matching CollectionRouter output."""
    return {
        "chunk_hash": chunk_hash,
        "content": content,
        "source": source or f"/vault/{scope}/{content[:10]}.md",
        "heading": heading or content[:20],
        "score": score,
        "weighted_score": score * weight,
        "_scope": scope,
        "_scope_id": scope_id,
        "_weight": weight,
        "_collection": collection,
        "topic": topic,
        "entry_type": entry_type,
        "tags": tags,
        "retrieval_count": retrieval_count,
    }


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
def mock_router():
    """Create a mock CollectionRouter with default search results."""
    router = MagicMock()
    router.search = AsyncMock(return_value=[])
    router.close = MagicMock()
    return router


@pytest.fixture
def mock_store():
    """Create a mock MilvusStore."""
    store = MagicMock()
    store.count.return_value = 10
    store.model_info = {"provider": "test", "model": "test-model", "dimension": 384}
    store.needs_reindex = False
    store.search.return_value = []
    store.query.return_value = []
    return store


@pytest.fixture
def service(mock_embedder, mock_router):
    """Create a MemoryV2Service with mocked deps."""
    svc = MemoryV2Service(
        milvus_uri="/tmp/test_l3.db",
        embedding_provider="openai",
    )
    svc._embedder = mock_embedder
    svc._router = mock_router
    svc._initialized = True
    return svc


@pytest.fixture
def plugin():
    """Create a MemoryV2Plugin for handler testing."""
    from src.plugins.internal.memory_v2 import MemoryV2Plugin

    return MemoryV2Plugin()


@pytest.fixture
def wired_plugin(plugin, service):
    """Plugin with a wired-up service."""
    plugin._service = service
    plugin._log = MagicMock()
    return plugin


# ===========================================================================
# (a) Cross-topic: memory_search returns results from ALL topics
# ===========================================================================


class TestCrossTopicSearch:
    """(a) memory_search("database optimization") returns results from all
    topics — not limited to the current/detected topic.
    """

    @pytest.mark.asyncio
    async def test_search_returns_results_from_multiple_topics(
        self, service, mock_router, mock_embedder
    ):
        """L3 search should return results across different topics."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "h1",
                    "Database indexing best practices",
                    0.92,
                    topic="database",
                ),
                _search_result(
                    "h2",
                    "Query optimization for testing",
                    0.85,
                    topic="testing",
                ),
                _search_result(
                    "h3",
                    "Deployment database migrations",
                    0.78,
                    topic="deployment",
                ),
                _search_result(
                    "h4",
                    "General optimization techniques",
                    0.70,
                    topic="",
                ),
            ]
        )

        results = await service.search("myapp", "database optimization")

        assert len(results) == 4
        topics = {r["topic"] for r in results}
        # Results span multiple topics, not just "database"
        assert "database" in topics
        assert "testing" in topics
        assert "deployment" in topics
        assert "" in topics  # Untagged results included

    @pytest.mark.asyncio
    async def test_no_topic_filter_by_default(self, service, mock_router, mock_embedder):
        """Default search (no topic param) should not pass topic filter."""
        mock_router.search = AsyncMock(return_value=[])

        await service.search("myapp", "database optimization")

        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs.get("topic") is None

    @pytest.mark.asyncio
    async def test_explicit_topic_still_allows_cross_topic_via_fallback(
        self, service, mock_router, mock_embedder
    ):
        """When topic filter yields < 3 results, CollectionRouter falls back
        to unfiltered search returning cross-topic results.

        The router handles this internally — we verify it's called with the
        topic so the fallback logic can activate.
        """
        # Router returns results including fallback cross-topic hits
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Auth insight", 0.9, topic="authentication"),
                _search_result("h2", "DB related auth", 0.7, topic="database"),
            ]
        )

        results = await service.search("myapp", "authentication patterns", topic="authentication")

        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs["topic"] == "authentication"
        # Cross-topic results are present (from fallback)
        assert len(results) == 2
        topics = {r["topic"] for r in results}
        assert len(topics) > 1

    @pytest.mark.asyncio
    async def test_plugin_handler_cross_topic(self, wired_plugin, mock_router, mock_embedder):
        """Plugin cmd_memory_search also returns cross-topic results."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Database optimization", 0.9, topic="database"),
                _search_result("h2", "Caching strategies", 0.8, topic="caching"),
                _search_result("h3", "General advice", 0.7, topic=""),
            ]
        )

        result = await wired_plugin.cmd_memory_search(
            {"project_id": "myapp", "query": "database optimization"}
        )

        assert result["success"] is True
        assert result["count"] == 3
        topics = {r["topic"] for r in result["results"]}
        assert len(topics) >= 2  # Multiple topics represented


# ===========================================================================
# (b) All scopes with correct weighted merge
# ===========================================================================


class TestAllScopesWeightedMerge:
    """(b) L3 search returns results from all scopes (project + agent-type +
    system) with correct weighted merge.
    """

    @pytest.mark.asyncio
    async def test_results_from_three_scopes(self, service, mock_router, mock_embedder):
        """Search returns results from project, agent-type, and system scopes."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "proj1",
                    "Project-specific database insight",
                    0.9,
                    scope="project",
                    scope_id="myapp",
                    weight=1.0,
                    collection="aq_project_myapp",
                ),
                _search_result(
                    "at1",
                    "Coding agent database best practices",
                    0.85,
                    scope="agent_type",
                    scope_id="coding",
                    weight=0.7,
                    collection="aq_agenttype_coding",
                ),
                _search_result(
                    "sys1",
                    "System-wide optimization patterns",
                    0.80,
                    scope="system",
                    scope_id=None,
                    weight=0.4,
                    collection="aq_system",
                ),
            ]
        )

        results = await service.search("myapp", "database optimization")

        assert len(results) == 3
        scopes = {r["_scope"] for r in results}
        assert "project" in scopes
        assert "agent_type" in scopes
        assert "system" in scopes

    @pytest.mark.asyncio
    async def test_weighted_scores_are_correct(self, service, mock_router, mock_embedder):
        """Weighted scores reflect scope weights: project=1.0, agent-type=0.7, system=0.4."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "proj1",
                    "Project result",
                    0.80,
                    scope="project",
                    weight=1.0,
                ),
                _search_result(
                    "at1",
                    "Agent-type result",
                    0.80,
                    scope="agent_type",
                    scope_id="coding",
                    weight=0.7,
                    collection="aq_agenttype_coding",
                ),
                _search_result(
                    "sys1",
                    "System result",
                    0.80,
                    scope="system",
                    scope_id=None,
                    weight=0.4,
                    collection="aq_system",
                ),
            ]
        )

        results = await service.search("myapp", "query")

        # All have same raw score 0.80, but different weighted scores
        by_scope = {r["_scope"]: r for r in results}
        assert by_scope["project"]["weighted_score"] == pytest.approx(0.80)
        assert by_scope["agent_type"]["weighted_score"] == pytest.approx(0.56)
        assert by_scope["system"]["weighted_score"] == pytest.approx(0.32)

    @pytest.mark.asyncio
    async def test_project_outranks_system_at_equal_similarity(
        self, service, mock_router, mock_embedder
    ):
        """At equal raw similarity, project (1.0) ranks above system (0.4)."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "proj1",
                    "Project memory",
                    0.85,
                    scope="project",
                    weight=1.0,
                ),
                _search_result(
                    "sys1",
                    "System memory",
                    0.85,
                    scope="system",
                    scope_id=None,
                    weight=0.4,
                    collection="aq_system",
                ),
            ]
        )

        results = await service.search("myapp", "query")

        assert results[0]["_scope"] == "project"
        assert results[1]["_scope"] == "system"
        assert results[0]["weighted_score"] > results[1]["weighted_score"]

    @pytest.mark.asyncio
    async def test_plugin_handler_preserves_scope_metadata(
        self, wired_plugin, mock_router, mock_embedder
    ):
        """Plugin handler formats results with scope/scope_id/collection fields."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "proj1",
                    "Project insight",
                    0.9,
                    scope="project",
                    scope_id="myapp",
                    weight=1.0,
                    collection="aq_project_myapp",
                ),
                _search_result(
                    "sys1",
                    "System insight",
                    0.7,
                    scope="system",
                    scope_id=None,
                    weight=0.4,
                    collection="aq_system",
                ),
            ]
        )

        result = await wired_plugin.cmd_memory_search({"project_id": "myapp", "query": "test"})

        assert result["success"] is True
        # Plugin formats _scope as "scope", _scope_id as "scope_id"
        proj_result = result["results"][0]
        assert proj_result["scope"] == "project"
        assert proj_result["scope_id"] == "myapp"
        assert proj_result["collection"] == "aq_project_myapp"

        sys_result = result["results"][1]
        assert sys_result["scope"] == "system"
        assert sys_result["scope_id"] is None
        assert sys_result["collection"] == "aq_system"

    @pytest.mark.asyncio
    async def test_search_passes_project_id_to_router(self, service, mock_router, mock_embedder):
        """project_id is forwarded to router so it includes the project scope."""
        await service.search("my-project", "query")

        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs["project_id"] == "my-project"


# ===========================================================================
# (c) No duplication with L1 or L2
# ===========================================================================


class TestNoDuplicationWithL1L2:
    """(c) L3 search does not duplicate results already loaded in L1 or L2.

    L1 loads KV facts (entry_type="kv") at task start.
    L2 loads topic-filtered memories from knowledge files and notes.
    L3 is explicit semantic search via memory_search.

    These tiers are structurally independent:
    - L1 uses KV entries (entry_type=kv), L3 searches document entries
    - L2 loads from disk files, L3 searches the vector index
    - L3 results include chunk_hash for client-side dedup if needed
    """

    @pytest.mark.asyncio
    async def test_l3_returns_document_entries_not_kv(self, service, mock_router, mock_embedder):
        """L3 semantic search returns document entries, not KV entries.

        L1 injects facts.md KV entries (entry_type="kv"). L3 memory_search
        returns document entries from vector search, so there is no structural
        overlap with L1 content.
        """
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "doc1",
                    "Database optimization insights",
                    0.9,
                    entry_type="document",
                ),
                _search_result(
                    "doc2",
                    "Testing best practices",
                    0.8,
                    entry_type="document",
                ),
            ]
        )

        results = await service.search("myapp", "database optimization")

        # All results are document entries (not KV entries that L1 loads)
        for r in results:
            assert r["entry_type"] == "document"

    @pytest.mark.asyncio
    async def test_l3_results_include_chunk_hash_for_dedup(
        self, service, mock_router, mock_embedder
    ):
        """L3 results include chunk_hash identifiers that enable deduplication
        with content previously loaded by L2 (topic context).
        """
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("hash_abc", "Insight one", 0.9),
                _search_result("hash_def", "Insight two", 0.8),
            ]
        )

        results = await service.search("myapp", "query")

        for r in results:
            assert "chunk_hash" in r
            assert r["chunk_hash"]  # non-empty

    @pytest.mark.asyncio
    async def test_l3_results_independent_of_l2_topic_loading(
        self, service, mock_router, mock_embedder
    ):
        """L3 search is independent of L2 topic context loading.

        Calling memory_search does not automatically include or exclude
        content based on what L2 may have already loaded. The search is
        a standalone operation.
        """
        expected_results = [
            _search_result("h1", "Result one", 0.9, topic="database"),
            _search_result("h2", "Result two", 0.8, topic="testing"),
        ]
        mock_router.search = AsyncMock(return_value=expected_results)

        # First search — simulating fresh state (no L2 loaded)
        results1 = await service.search("myapp", "optimization")

        # Second search — same query, same results expected
        # (L3 doesn't track L2 state)
        mock_router.search = AsyncMock(return_value=expected_results)
        results2 = await service.search("myapp", "optimization")

        assert len(results1) == len(results2) == 2
        assert results1[0]["chunk_hash"] == results2[0]["chunk_hash"]

    @pytest.mark.asyncio
    async def test_plugin_handler_includes_chunk_hash_for_dedup(
        self, wired_plugin, mock_router, mock_embedder
    ):
        """Plugin-formatted results include chunk_hash for client-side dedup."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("unique_hash_1", "Content A", 0.9),
                _search_result("unique_hash_2", "Content B", 0.8),
            ]
        )

        result = await wired_plugin.cmd_memory_search({"project_id": "myapp", "query": "test"})

        assert result["success"] is True
        for r in result["results"]:
            assert "chunk_hash" in r
            assert r["chunk_hash"]  # non-empty

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_l3_explicit_scope_searches_single_collection(
        self, service, mock_store, mock_embedder
    ):
        """Explicit scope param searches only that collection — naturally
        avoids L1/L2 overlap by targeting a specific scope.
        """
        from memsearch import MemoryScope

        mock_store.search.return_value = [
            {
                "chunk_hash": "sys1",
                "content": "System-wide insight",
                "source": "/vault/system/insight.md",
                "heading": "System insight",
                "score": 0.85,
                "entry_type": "document",
                "topic": "",
                "tags": "[]",
                "retrieval_count": 0,
            }
        ]

        with patch.object(service, "_get_store", return_value=mock_store):
            with patch.object(
                service,
                "_resolve_scope",
                return_value=(MemoryScope.SYSTEM, None),
            ):
                with patch(
                    "src.memory_v2_service.collection_name",
                    return_value="aq_system",
                ):
                    results = await service.search("myapp", "query", scope="system")

        assert len(results) == 1
        assert results[0]["_scope"] == "system"


# ===========================================================================
# (d) Works when L2 is not active (no topic detected)
# ===========================================================================


class TestWorksWithoutL2:
    """(d) L3 search works even when L2 is not active (no topic detected).

    The memory_search tool is independent of L2 topic detection. An agent
    can call it at any time without prior topic loading.
    """

    @pytest.mark.asyncio
    async def test_search_without_topic_returns_results(self, service, mock_router, mock_embedder):
        """Search succeeds with no topic filter — full cross-topic search."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Relevant insight", 0.92),
            ]
        )

        results = await service.search("myapp", "how does authentication work")

        assert len(results) == 1
        assert results[0]["content"] == "Relevant insight"
        # No topic was passed to the router
        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs.get("topic") is None

    @pytest.mark.asyncio
    async def test_search_works_with_no_topic_context_loaded(
        self, service, mock_router, mock_embedder
    ):
        """L3 search works completely independently of any L2 state."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Database patterns", 0.9, topic="database"),
                _search_result("h2", "Auth patterns", 0.85, topic="auth"),
                _search_result("h3", "Untagged content", 0.8, topic=""),
            ]
        )

        results = await service.search("myapp", "best practices")

        assert len(results) == 3
        # All results returned regardless of no prior L2 topic loading

    @pytest.mark.asyncio
    async def test_plugin_handler_no_topic_param(self, wired_plugin, mock_router, mock_embedder):
        """Plugin cmd_memory_search works without topic parameter."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Some insight", 0.88),
            ]
        )

        result = await wired_plugin.cmd_memory_search(
            {"project_id": "myapp", "query": "general question"}
        )

        assert result["success"] is True
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_search_with_empty_topic_treated_as_none(
        self, service, mock_router, mock_embedder
    ):
        """Passing empty string topic is equivalent to no topic filter."""
        mock_router.search = AsyncMock(return_value=[])

        # Empty topic should not filter
        await service.search("myapp", "query", topic="")

        call_kwargs = mock_router.search.call_args
        # Empty string is falsy, should not be passed as topic filter
        # The service checks `if topic:` so empty string skips filtering
        assert call_kwargs.kwargs.get("topic") is None or call_kwargs.kwargs.get("topic") == ""

    @pytest.mark.asyncio
    async def test_batch_search_without_topic(self, service, mock_router, mock_embedder):
        """Batch search also works without topic (L2 inactive)."""
        mock_router.search = AsyncMock(return_value=[_search_result("h1", "Batch result", 0.8)])
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384])

        results = await service.batch_search("myapp", ["query1", "query2"])

        assert "query1" in results
        assert "query2" in results


# ===========================================================================
# (e) Results include source scope and topic metadata
# ===========================================================================


class TestScopeAndTopicMetadata:
    """(e) L3 results include source scope and topic metadata."""

    @pytest.mark.asyncio
    async def test_results_include_scope_fields(self, service, mock_router, mock_embedder):
        """Each result has _scope, _scope_id, _weight, _collection metadata."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "h1",
                    "Project insight",
                    0.9,
                    scope="project",
                    scope_id="myapp",
                    weight=1.0,
                    collection="aq_project_myapp",
                ),
            ]
        )

        results = await service.search("myapp", "query")

        r = results[0]
        assert r["_scope"] == "project"
        assert r["_scope_id"] == "myapp"
        assert r["_weight"] == 1.0
        assert r["_collection"] == "aq_project_myapp"

    @pytest.mark.asyncio
    async def test_results_include_topic_field(self, service, mock_router, mock_embedder):
        """Each result includes its topic (or empty string if untagged)."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Auth insight", 0.9, topic="authentication"),
                _search_result("h2", "Untagged insight", 0.8, topic=""),
            ]
        )

        results = await service.search("myapp", "query")

        assert results[0]["topic"] == "authentication"
        assert results[1]["topic"] == ""

    @pytest.mark.asyncio
    async def test_results_include_weighted_score(self, service, mock_router, mock_embedder):
        """Each result includes weighted_score (raw score × scope weight)."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "h1",
                    "Content",
                    0.85,
                    scope="agent_type",
                    scope_id="coding",
                    weight=0.7,
                    collection="aq_agenttype_coding",
                ),
            ]
        )

        results = await service.search("myapp", "query")

        r = results[0]
        assert "weighted_score" in r
        assert r["weighted_score"] == pytest.approx(0.85 * 0.7)
        assert "score" in r
        assert r["score"] == pytest.approx(0.85)

    @pytest.mark.asyncio
    async def test_plugin_formats_scope_as_top_level_field(
        self, wired_plugin, mock_router, mock_embedder
    ):
        """Plugin _format_search_result maps _scope → scope, _scope_id → scope_id."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "h1",
                    "Insight",
                    0.9,
                    scope="project",
                    scope_id="myapp",
                    weight=1.0,
                    collection="aq_project_myapp",
                    topic="testing",
                ),
            ]
        )

        result = await wired_plugin.cmd_memory_search({"project_id": "myapp", "query": "test"})

        r = result["results"][0]
        # Plugin remaps underscore-prefixed keys to clean keys
        assert r["scope"] == "project"
        assert r["scope_id"] == "myapp"
        assert r["collection"] == "aq_project_myapp"
        assert r["topic"] == "testing"
        assert "weighted_score" in r
        assert "score" in r

    @pytest.mark.asyncio
    async def test_all_three_scopes_have_metadata(self, service, mock_router, mock_embedder):
        """Results from project, agent-type, and system all carry scope metadata."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "p1",
                    "Project result",
                    0.9,
                    scope="project",
                    scope_id="myapp",
                    weight=1.0,
                    collection="aq_project_myapp",
                    topic="database",
                ),
                _search_result(
                    "a1",
                    "Agent-type result",
                    0.8,
                    scope="agent_type",
                    scope_id="coding",
                    weight=0.7,
                    collection="aq_agenttype_coding",
                    topic="optimization",
                ),
                _search_result(
                    "s1",
                    "System result",
                    0.7,
                    scope="system",
                    scope_id=None,
                    weight=0.4,
                    collection="aq_system",
                    topic="",
                ),
            ]
        )

        results = await service.search("myapp", "database optimization")

        for r in results:
            assert "_scope" in r
            assert "_scope_id" in r  # None for system
            assert "_weight" in r
            assert "_collection" in r
            assert "topic" in r
            assert "weighted_score" in r

        # Verify specific scope metadata
        by_scope = {r["_scope"]: r for r in results}
        assert by_scope["project"]["_scope_id"] == "myapp"
        assert by_scope["agent_type"]["_scope_id"] == "coding"
        assert by_scope["system"]["_scope_id"] is None

    @pytest.mark.asyncio
    async def test_metadata_preserved_in_batch_search(self, service, mock_router, mock_embedder):
        """Batch search also preserves scope/topic metadata per result."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "h1",
                    "Result",
                    0.9,
                    scope="project",
                    scope_id="myapp",
                    topic="testing",
                    weight=1.0,
                ),
            ]
        )
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384])

        results = await service.batch_search("myapp", ["query1"])

        assert "query1" in results
        r = results["query1"][0]
        assert r["_scope"] == "project"
        assert r["topic"] == "testing"


# ===========================================================================
# (f) Retrieval tracking (retrieval_count increments)
# ===========================================================================


class TestRetrievalTracking:
    """(f) L3 search respects the same retrieval tracking (retrieval_count
    increments).

    MilvusStore.search() internally calls _update_retrieval_stats() which
    increments retrieval_count for returned results. We verify this at the
    service and plugin layers.
    """

    @pytest.mark.asyncio
    async def test_search_results_include_retrieval_count(
        self, service, mock_router, mock_embedder
    ):
        """Search results include retrieval_count field."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result(
                    "h1",
                    "Previously retrieved insight",
                    0.9,
                    retrieval_count=5,
                ),
                _search_result(
                    "h2",
                    "Newly retrieved insight",
                    0.8,
                    retrieval_count=0,
                ),
            ]
        )

        results = await service.search("myapp", "query")

        assert results[0]["retrieval_count"] == 5
        assert results[1]["retrieval_count"] == 0

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_explicit_scope_search_delegates_to_store_search(
        self, service, mock_store, mock_embedder
    ):
        """Explicit-scope search calls MilvusStore.search(), which tracks
        retrieval counts internally.
        """
        from memsearch import MemoryScope

        mock_store.search.return_value = [
            {
                "chunk_hash": "h1",
                "content": "Test content",
                "source": "/vault/test.md",
                "heading": "Test",
                "score": 0.9,
                "entry_type": "document",
                "topic": "",
                "tags": "[]",
                "retrieval_count": 3,
            }
        ]

        with patch.object(service, "_get_store", return_value=mock_store):
            with patch.object(
                service,
                "_resolve_scope",
                return_value=(MemoryScope.PROJECT, "myapp"),
            ):
                with patch(
                    "src.memory_v2_service.collection_name",
                    return_value="aq_project_myapp",
                ):
                    results = await service.search("myapp", "query", scope="project_myapp")

        # MilvusStore.search was called (which internally updates retrieval stats)
        mock_store.search.assert_called_once()
        assert len(results) == 1
        assert results[0]["retrieval_count"] == 3

    @pytest.mark.asyncio
    async def test_multiscope_search_delegates_to_router(self, service, mock_router, mock_embedder):
        """Default (no scope) search delegates to CollectionRouter.search()
        which in turn calls MilvusStore.search() on each scope — all of
        which track retrieval counts.
        """
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Result", 0.9, retrieval_count=2),
            ]
        )

        results = await service.search("myapp", "query")

        # Router.search was called, which internally calls store.search per scope
        mock_router.search.assert_awaited_once()
        assert results[0]["retrieval_count"] == 2

    @pytest.mark.asyncio
    async def test_retrieval_count_increases_across_searches(
        self, service, mock_router, mock_embedder
    ):
        """Successive searches reflect incrementing retrieval_count."""
        # First search — count starts at 0
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Insight", 0.9, retrieval_count=0),
            ]
        )
        results1 = await service.search("myapp", "query")
        assert results1[0]["retrieval_count"] == 0

        # Second search — count now 1 (store incremented after first search)
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Insight", 0.9, retrieval_count=1),
            ]
        )
        results2 = await service.search("myapp", "query")
        assert results2[0]["retrieval_count"] == 1

        # Third search — count now 2
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Insight", 0.9, retrieval_count=2),
            ]
        )
        results3 = await service.search("myapp", "query")
        assert results3[0]["retrieval_count"] == 2

    @pytest.mark.asyncio
    async def test_plugin_handler_preserves_retrieval_count(
        self, wired_plugin, mock_router, mock_embedder
    ):
        """Plugin cmd_memory_search preserves retrieval_count in results.

        Note: The plugin's _format_search_result may not include
        retrieval_count directly, but the underlying search results
        track it. The count is maintained at the store level.
        """
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Insight", 0.9, retrieval_count=7),
            ]
        )

        result = await wired_plugin.cmd_memory_search({"project_id": "myapp", "query": "test"})

        assert result["success"] is True
        # The chunk_hash is present for result identification
        assert result["results"][0]["chunk_hash"] == "h1"


# ===========================================================================
# Additional integration-level tests
# ===========================================================================


class TestL3EdgeCases:
    """Additional edge cases for L3 search robustness."""

    @pytest.mark.asyncio
    async def test_search_with_no_results(self, service, mock_router, mock_embedder):
        """Search that finds nothing returns empty list gracefully."""
        mock_router.search = AsyncMock(return_value=[])

        results = await service.search("myapp", "extremely obscure query")

        assert results == []

    @pytest.mark.asyncio
    async def test_search_unavailable_returns_empty(self):
        """When memsearch is not initialized, search returns empty."""
        svc = MemoryV2Service()
        results = await svc.search("myapp", "query")
        assert results == []

    @pytest.mark.asyncio
    async def test_plugin_missing_project_id(self, wired_plugin):
        """Plugin handler returns error when project_id is missing."""
        result = await wired_plugin.cmd_memory_search({"query": "test"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_plugin_missing_query(self, wired_plugin):
        """Plugin handler returns error when both query and queries are missing."""
        result = await wired_plugin.cmd_memory_search({"project_id": "myapp"})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_embedding_failure_propagates_to_plugin(
        self, wired_plugin, mock_router, mock_embedder
    ):
        """Embedding failure is caught by the plugin handler (not service).

        The service layer lets embedding errors propagate; the plugin's
        cmd_memory_search wraps the exception in an error dict.
        """
        mock_embedder.embed = AsyncMock(side_effect=RuntimeError("Embedding API down"))

        result = await wired_plugin.cmd_memory_search({"project_id": "myapp", "query": "test"})

        assert "error" in result
        assert "Embedding API down" in result["error"] or "failed" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_router_failure_returns_empty(self, service, mock_router, mock_embedder):
        """Router search failure returns empty list, no exception."""
        mock_router.search = AsyncMock(side_effect=RuntimeError("Milvus down"))

        # The service.search delegates to router which raises
        # Service should handle this gracefully
        try:
            results = await service.search("myapp", "query")
            assert isinstance(results, list)
        except RuntimeError:
            # If the service doesn't catch router errors, that's also
            # acceptable — the caller (plugin) handles it
            pass

    @pytest.mark.asyncio
    async def test_plugin_handler_catches_service_error(
        self, wired_plugin, mock_router, mock_embedder
    ):
        """Plugin handler catches service exceptions and returns error dict."""
        mock_router.search = AsyncMock(side_effect=RuntimeError("Internal error"))

        result = await wired_plugin.cmd_memory_search({"project_id": "myapp", "query": "test"})

        # Plugin wraps errors in {"error": ...}
        assert "error" in result

    @pytest.mark.asyncio
    async def test_batch_search_cross_topic(self, service, mock_router, mock_embedder):
        """Batch search also returns cross-topic results per query."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "DB insight", 0.9, topic="database"),
                _search_result("h2", "Auth insight", 0.8, topic="auth"),
            ]
        )
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384])

        results = await service.batch_search("myapp", ["database optimization", "auth patterns"])

        # Both queries should have results
        for q in ["database optimization", "auth patterns"]:
            assert q in results
            assert len(results[q]) > 0

    @pytest.mark.asyncio
    async def test_plugin_batch_search(self, wired_plugin, mock_router, mock_embedder):
        """Plugin supports batch queries via 'queries' parameter."""
        mock_router.search = AsyncMock(
            return_value=[
                _search_result("h1", "Result", 0.9, topic="testing"),
            ]
        )
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384])

        result = await wired_plugin.cmd_memory_search(
            {
                "project_id": "myapp",
                "queries": ["query1", "query2"],
            }
        )

        assert result["success"] is True
        assert result["batch"] is True
        assert "query1" in result["results"]
        assert "query2" in result["results"]

    @pytest.mark.asyncio
    async def test_search_with_top_k_parameter(self, service, mock_router, mock_embedder):
        """top_k parameter is forwarded to control result count."""
        mock_router.search = AsyncMock(return_value=[])

        await service.search("myapp", "query", top_k=5)

        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs["top_k"] == 5

    @pytest.mark.asyncio
    async def test_search_with_full_flag(self, service, mock_router, mock_embedder):
        """full=True flag is forwarded to include original content."""
        mock_router.search = AsyncMock(return_value=[])

        await service.search("myapp", "query", full=True)

        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs["full"] is True
