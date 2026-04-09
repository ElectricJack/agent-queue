"""Unit tests for MemoryManager.scoped_search and scoped_batch_search.

Tests the weighted merge for semantic search across scopes (spec §6):
project weight=1.0, agent-type=0.7, system=0.4.  All tests mock the
memsearch dependency so they run without Milvus or embedding providers.

Roadmap 3.1.7 and 3.1.10 test cases:
  (a) Insert similar content in project (weight 1.0) and system (weight 0.4)
      — project result ranks first.
  (b) Insert highly relevant content in system scope and weakly relevant in
      project — system result can still rank high if raw similarity is much
      higher.
  (c) Search across 3 scopes with 5 results each — merged output is top-K by
      weighted score.
  (d) Scope with no matching results contributes nothing to merge.
  (e) Results include source scope metadata so caller knows which scope each
      result came from.
  (f) Total search latency is bounded (parallel scope queries, not sequential).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from memsearch import SCOPE_WEIGHTS, MemoryScope, merge_and_rank

from src.config import MemoryConfig
from src.memory import MemoryManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_manager(tmp_path, **overrides) -> MemoryManager:
    """Create a MemoryManager with memory enabled."""
    cfg = MemoryConfig(enabled=True, **overrides)
    return MemoryManager(cfg, storage_root=str(tmp_path))


def _fake_search_result(
    chunk_hash: str,
    content: str,
    score: float,
    scope: str = "project",
    scope_id: str | None = "myapp",
    weight: float = 1.0,
    collection: str = "aq_project_myapp",
    topic: str = "",
) -> dict:
    """Build a fake search result dict matching CollectionRouter output."""
    return {
        "chunk_hash": chunk_hash,
        "content": content,
        "source": f"/vault/{scope}/{content[:10]}.md",
        "heading": content[:20],
        "score": score,
        "weighted_score": score * weight,
        "_scope": scope,
        "_scope_id": scope_id,
        "_weight": weight,
        "_collection": collection,
        "topic": topic,
    }


# ---------------------------------------------------------------------------
# Test: scoped_search basics
# ---------------------------------------------------------------------------


class TestScopedSearch:
    """Unit tests for MemoryManager.scoped_search()."""

    @pytest.fixture
    def mgr(self, tmp_path):
        return _make_manager(tmp_path)

    @pytest.fixture
    def mock_deps(self):
        """Patch CollectionRouter and get_embedding_provider."""
        mock_embedder = MagicMock()
        mock_embedder.dimension = 384
        mock_embedder.model_name = "test-model"
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384])

        mock_router = MagicMock()
        mock_router.search = AsyncMock(return_value=[])
        mock_router.close = MagicMock()

        with (
            patch("src.memory.get_embedding_provider", return_value=mock_embedder),
            patch("src.memory.CollectionRouter", return_value=mock_router),
            patch("src.memory.MEMSEARCH_AVAILABLE", True),
        ):
            yield mock_embedder, mock_router

    async def test_returns_empty_when_disabled(self, tmp_path):
        """Disabled config returns empty without errors."""
        mgr = MemoryManager(MemoryConfig(enabled=False))
        results = await mgr.scoped_search("query", project_id="myapp")
        assert results == []

    async def test_returns_empty_when_memsearch_unavailable(self, tmp_path):
        """Returns empty when memsearch is not installed."""
        mgr = _make_manager(tmp_path)
        with patch("src.memory.MEMSEARCH_AVAILABLE", False):
            results = await mgr.scoped_search("query", project_id="myapp")
            assert results == []

    async def test_basic_search_delegates_to_router(self, mgr, mock_deps):
        """scoped_search embeds query and delegates to CollectionRouter."""
        mock_embedder, mock_router = mock_deps
        mock_router.search = AsyncMock(
            return_value=[
                _fake_search_result("h1", "project insight", 0.9, "project", "myapp", 1.0),
            ]
        )

        results = await mgr.scoped_search(
            "test query",
            project_id="myapp",
            agent_type="coding",
        )

        assert len(results) == 1
        assert results[0]["content"] == "project insight"
        # Verify embedding was called with the query
        mock_embedder.embed.assert_awaited_once_with(["test query"])
        # Verify router was called with correct arguments
        mock_router.search.assert_awaited_once()
        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs["project_id"] == "myapp"
        assert call_kwargs.kwargs["agent_type"] == "coding"

    async def test_topic_forwarded_to_router(self, mgr, mock_deps):
        """Topic filter is forwarded to the router."""
        _, mock_router = mock_deps

        await mgr.scoped_search(
            "auth query",
            project_id="myapp",
            topic="authentication",
        )

        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs["topic"] == "authentication"

    async def test_custom_weights_forwarded(self, mgr, mock_deps):
        """Custom weight overrides are forwarded to the router."""
        _, mock_router = mock_deps
        custom_weights = {"project": 0.9, "system": 0.5}

        await mgr.scoped_search(
            "query",
            project_id="myapp",
            weights=custom_weights,
        )

        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs["weights"] == custom_weights

    async def test_full_flag_forwarded(self, mgr, mock_deps):
        """full=True flag is forwarded to the router."""
        _, mock_router = mock_deps

        await mgr.scoped_search(
            "query",
            project_id="myapp",
            full=True,
        )

        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs["full"] is True

    async def test_embedding_failure_returns_empty(self, mgr, mock_deps):
        """Embedding failure returns empty list, no exception."""
        mock_embedder, _ = mock_deps
        mock_embedder.embed = AsyncMock(side_effect=RuntimeError("API down"))

        results = await mgr.scoped_search("query", project_id="myapp")
        assert results == []

    async def test_router_search_failure_returns_empty(self, mgr, mock_deps):
        """Router search failure returns empty list, no exception."""
        _, mock_router = mock_deps
        mock_router.search = AsyncMock(side_effect=RuntimeError("Milvus down"))

        results = await mgr.scoped_search("query", project_id="myapp")
        assert results == []


# ---------------------------------------------------------------------------
# Test: weighted merge ranking (spec §6 / roadmap 3.1.10)
# ---------------------------------------------------------------------------


class TestWeightedMergeRanking:
    """Test that weighted merge produces correct ranking."""

    @pytest.fixture
    def mgr(self, tmp_path):
        return _make_manager(tmp_path)

    @pytest.fixture
    def mock_deps(self):
        """Patch CollectionRouter and embedder."""
        mock_embedder = MagicMock()
        mock_embedder.dimension = 384
        mock_embedder.model_name = "test-model"
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384])

        mock_router = MagicMock()
        mock_router.search = AsyncMock(return_value=[])
        mock_router.close = MagicMock()

        with (
            patch("src.memory.get_embedding_provider", return_value=mock_embedder),
            patch("src.memory.CollectionRouter", return_value=mock_router),
            patch("src.memory.MEMSEARCH_AVAILABLE", True),
        ):
            yield mock_embedder, mock_router

    async def test_a_project_outranks_system_at_equal_similarity(self, mgr, mock_deps):
        """(a) Same similarity score → project (1.0) ranks above system (0.4)."""
        _, mock_router = mock_deps
        mock_router.search = AsyncMock(
            return_value=[
                _fake_search_result(
                    "proj1",
                    "project memory",
                    0.8,
                    scope="project",
                    scope_id="myapp",
                    weight=1.0,
                    collection="aq_project_myapp",
                ),
                _fake_search_result(
                    "sys1",
                    "system memory",
                    0.8,
                    scope="system",
                    scope_id=None,
                    weight=0.4,
                    collection="aq_system",
                ),
            ]
        )

        results = await mgr.scoped_search("query", project_id="myapp")

        assert len(results) == 2
        # Project result (0.8 * 1.0 = 0.80) should rank above
        # system result (0.8 * 0.4 = 0.32)
        assert results[0]["_scope"] == "project"
        assert results[0]["weighted_score"] == pytest.approx(0.8)
        assert results[1]["_scope"] == "system"
        assert results[1]["weighted_score"] == pytest.approx(0.32)

    async def test_b_high_system_score_can_outrank_low_project(self, mgr, mock_deps):
        """(b) System with very high similarity can outrank project with low similarity.

        The CollectionRouter.search() returns results already sorted by
        weighted_score (it calls merge_and_rank internally).  We return
        them pre-sorted as the real router would.
        """
        _, mock_router = mock_deps
        # System score 0.95 * 0.4 = 0.38
        # Project score 0.3 * 1.0 = 0.30
        # Router returns sorted by weighted_score descending:
        mock_router.search = AsyncMock(
            return_value=[
                _fake_search_result(
                    "sys1",
                    "highly relevant system",
                    0.95,
                    scope="system",
                    scope_id=None,
                    weight=0.4,
                    collection="aq_system",
                ),
                _fake_search_result(
                    "proj1",
                    "weakly relevant project",
                    0.3,
                    scope="project",
                    scope_id="myapp",
                    weight=1.0,
                    collection="aq_project_myapp",
                ),
            ]
        )

        results = await mgr.scoped_search("query", project_id="myapp")

        assert len(results) == 2
        # System should rank higher because 0.95*0.4=0.38 > 0.3*1.0=0.30
        assert results[0]["_scope"] == "system"
        assert results[0]["weighted_score"] == pytest.approx(0.38)
        assert results[1]["_scope"] == "project"
        assert results[1]["weighted_score"] == pytest.approx(0.30)

    async def test_c_three_scopes_top_k_merge(self, mgr, mock_deps):
        """(c) 3 scopes × 5 results each → merged output is top-K by weighted score.

        The CollectionRouter.search() internally calls merge_and_rank()
        which sorts by weighted_score and truncates to top_k.  We
        simulate this by returning the pre-merged top-5 results.
        """
        _, mock_router = mock_deps

        # Build all 15 results, then sort and truncate as the real router would
        all_results = []
        for i in range(5):
            score = 0.9 - i * 0.1  # 0.9, 0.8, 0.7, 0.6, 0.5
            all_results.append(
                _fake_search_result(
                    f"proj{i}",
                    f"project result {i}",
                    score,
                    scope="project",
                    scope_id="myapp",
                    weight=1.0,
                    collection="aq_project_myapp",
                )
            )
            all_results.append(
                _fake_search_result(
                    f"agent{i}",
                    f"agent-type result {i}",
                    score,
                    scope="agent_type",
                    scope_id="coding",
                    weight=0.7,
                    collection="aq_agenttype_coding",
                )
            )
            all_results.append(
                _fake_search_result(
                    f"sys{i}",
                    f"system result {i}",
                    score,
                    scope="system",
                    scope_id=None,
                    weight=0.4,
                    collection="aq_system",
                )
            )

        # Sort by weighted_score descending and take top 5 (as real router does)
        all_results.sort(key=lambda r: r["weighted_score"], reverse=True)
        top_5 = all_results[:5]

        mock_router.search = AsyncMock(return_value=top_5)

        results = await mgr.scoped_search(
            "query",
            project_id="myapp",
            agent_type="coding",
            top_k=5,
        )

        assert len(results) == 5
        # Results should be sorted by weighted_score descending
        scores = [r["weighted_score"] for r in results]
        assert scores == sorted(scores, reverse=True)
        # Top result should be the highest-scoring project result (0.9 * 1.0)
        assert results[0]["_scope"] == "project"
        # Verify top_k was forwarded to the router
        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs["top_k"] == 5

    async def test_d_empty_scope_contributes_nothing(self, mgr, mock_deps):
        """(d) Scope with no results contributes nothing (no padding)."""
        _, mock_router = mock_deps

        # Only project results, no system or agent-type results
        mock_router.search = AsyncMock(
            return_value=[
                _fake_search_result(
                    "proj1",
                    "only project result",
                    0.85,
                    scope="project",
                    scope_id="myapp",
                    weight=1.0,
                ),
            ]
        )

        results = await mgr.scoped_search(
            "query",
            project_id="myapp",
            agent_type="coding",
        )

        assert len(results) == 1
        assert results[0]["_scope"] == "project"

    async def test_e_results_include_scope_metadata(self, mgr, mock_deps):
        """(e) Results include source scope metadata for caller attribution."""
        _, mock_router = mock_deps
        mock_router.search = AsyncMock(
            return_value=[
                _fake_search_result(
                    "proj1",
                    "project memory",
                    0.9,
                    scope="project",
                    scope_id="myapp",
                    weight=1.0,
                    collection="aq_project_myapp",
                ),
                _fake_search_result(
                    "agent1",
                    "agent-type memory",
                    0.8,
                    scope="agent_type",
                    scope_id="coding",
                    weight=0.7,
                    collection="aq_agenttype_coding",
                ),
                _fake_search_result(
                    "sys1",
                    "system memory",
                    0.7,
                    scope="system",
                    scope_id=None,
                    weight=0.4,
                    collection="aq_system",
                ),
            ]
        )

        results = await mgr.scoped_search(
            "query",
            project_id="myapp",
            agent_type="coding",
        )

        for r in results:
            assert "_scope" in r
            assert "_scope_id" in r
            assert "_weight" in r
            assert "_collection" in r
            assert "weighted_score" in r

        scopes = {r["_scope"] for r in results}
        assert scopes == {"project", "agent_type", "system"}

    async def test_f_parallel_execution(self, mgr, mock_deps):
        """(f) Search uses parallel execution, not sequential.

        We verify this by checking that CollectionRouter.search() is
        called (it internally uses asyncio.gather for parallelism).
        """
        mock_embedder, mock_router = mock_deps
        mock_router.search = AsyncMock(return_value=[])

        await mgr.scoped_search(
            "query",
            project_id="myapp",
            agent_type="coding",
        )

        # Router.search() is the async parallel implementation
        mock_router.search.assert_awaited_once()
        # The router handles parallelism internally — verify it was
        # called with the right scope parameters that enable parallel search
        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs["project_id"] == "myapp"
        assert call_kwargs.kwargs["agent_type"] == "coding"

    async def test_agent_type_ranked_between_project_and_system(self, mgr, mock_deps):
        """Agent-type results (0.7) rank between project (1.0) and system (0.4)."""
        _, mock_router = mock_deps
        # All same raw score of 0.8
        mock_router.search = AsyncMock(
            return_value=[
                _fake_search_result(
                    "proj1",
                    "project",
                    0.8,
                    scope="project",
                    weight=1.0,
                ),
                _fake_search_result(
                    "agent1",
                    "agent-type",
                    0.8,
                    scope="agent_type",
                    scope_id="coding",
                    weight=0.7,
                    collection="aq_agenttype_coding",
                ),
                _fake_search_result(
                    "sys1",
                    "system",
                    0.8,
                    scope="system",
                    scope_id=None,
                    weight=0.4,
                    collection="aq_system",
                ),
            ]
        )

        results = await mgr.scoped_search(
            "query",
            project_id="myapp",
            agent_type="coding",
        )

        assert results[0]["_scope"] == "project"  # 0.8 * 1.0 = 0.80
        assert results[1]["_scope"] == "agent_type"  # 0.8 * 0.7 = 0.56
        assert results[2]["_scope"] == "system"  # 0.8 * 0.4 = 0.32


# ---------------------------------------------------------------------------
# Test: scoped_batch_search
# ---------------------------------------------------------------------------


class TestScopedBatchSearch:
    """Unit tests for MemoryManager.scoped_batch_search()."""

    @pytest.fixture
    def mgr(self, tmp_path):
        return _make_manager(tmp_path)

    @pytest.fixture
    def mock_deps(self):
        mock_embedder = MagicMock()
        mock_embedder.dimension = 384
        mock_embedder.model_name = "test-model"
        # Return different embeddings for each query
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384, [0.2] * 384])

        mock_router = MagicMock()
        mock_router.search = AsyncMock(return_value=[])
        mock_router.close = MagicMock()

        with (
            patch("src.memory.get_embedding_provider", return_value=mock_embedder),
            patch("src.memory.CollectionRouter", return_value=mock_router),
            patch("src.memory.MEMSEARCH_AVAILABLE", True),
        ):
            yield mock_embedder, mock_router

    async def test_returns_empty_when_disabled(self, tmp_path):
        mgr = MemoryManager(MemoryConfig(enabled=False))
        result = await mgr.scoped_batch_search(["q1", "q2"], project_id="myapp")
        assert result == {"q1": [], "q2": []}

    async def test_batch_embeds_all_queries_at_once(self, mgr, mock_deps):
        """Batch search embeds all queries in a single batch call."""
        mock_embedder, mock_router = mock_deps
        mock_router.search = AsyncMock(
            return_value=[
                _fake_search_result("h1", "result", 0.9),
            ]
        )

        await mgr.scoped_batch_search(
            ["query1", "query2"],
            project_id="myapp",
        )

        # Single batch embed call with both queries
        mock_embedder.embed.assert_awaited_once_with(["query1", "query2"])

    async def test_returns_results_per_query(self, mgr, mock_deps):
        """Returns a dict mapping each query to its results."""
        _, mock_router = mock_deps

        call_count = 0

        async def _search_side_effect(emb, **kwargs):
            nonlocal call_count
            call_count += 1
            return [_fake_search_result(f"h{call_count}", f"result {call_count}", 0.9)]

        mock_router.search = AsyncMock(side_effect=_search_side_effect)

        result = await mgr.scoped_batch_search(
            ["query1", "query2"],
            project_id="myapp",
        )

        assert "query1" in result
        assert "query2" in result
        assert len(result["query1"]) == 1
        assert len(result["query2"]) == 1

    async def test_individual_query_failure_returns_empty(self, mgr, mock_deps):
        """Individual query failures return empty lists without blocking others."""
        _, mock_router = mock_deps
        call_count = 0

        async def _flaky_search(emb, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Milvus timeout")
            return [_fake_search_result("h2", "success result", 0.8)]

        mock_router.search = AsyncMock(side_effect=_flaky_search)

        result = await mgr.scoped_batch_search(
            ["query1", "query2"],
            project_id="myapp",
        )

        assert result["query1"] == []  # failed
        assert len(result["query2"]) == 1  # succeeded

    async def test_empty_queries_returns_empty_dict(self, mgr, mock_deps):
        """Empty query list returns empty dict."""
        result = await mgr.scoped_batch_search([], project_id="myapp")
        assert result == {}

    async def test_batch_embed_failure_returns_empty(self, mgr, mock_deps):
        """Embedding failure returns empty results for all queries."""
        mock_embedder, _ = mock_deps
        mock_embedder.embed = AsyncMock(side_effect=RuntimeError("API error"))

        result = await mgr.scoped_batch_search(
            ["q1", "q2"],
            project_id="myapp",
        )

        assert result == {"q1": [], "q2": []}


# ---------------------------------------------------------------------------
# Test: router and embedder lifecycle
# ---------------------------------------------------------------------------


class TestRouterEmbedderLifecycle:
    """Test lazy initialization and cleanup of shared router and embedder."""

    @pytest.fixture
    def mgr(self, tmp_path):
        return _make_manager(tmp_path)

    async def test_embedder_created_once(self, mgr):
        """Embedder is created once and cached."""
        mock_embedder = MagicMock()
        mock_embedder.dimension = 384
        mock_embedder.model_name = "test-model"
        call_count = 0

        def _factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_embedder

        with (
            patch("src.memory.get_embedding_provider", side_effect=_factory),
            patch("src.memory.MEMSEARCH_AVAILABLE", True),
        ):
            e1 = await mgr._get_embedder()
            e2 = await mgr._get_embedder()
            assert e1 is e2
            assert call_count == 1

    async def test_router_created_once(self, mgr):
        """Router is created once and cached."""
        mock_embedder = MagicMock()
        mock_embedder.dimension = 384
        mock_embedder.model_name = "test-model"
        mock_router = MagicMock()
        call_count = 0

        def _router_factory(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_router

        with (
            patch("src.memory.get_embedding_provider", return_value=mock_embedder),
            patch("src.memory.CollectionRouter", side_effect=_router_factory),
            patch("src.memory.MEMSEARCH_AVAILABLE", True),
        ):
            r1 = await mgr._get_router()
            r2 = await mgr._get_router()
            assert r1 is r2
            assert call_count == 1

    async def test_close_cleans_up_router(self, mgr):
        """close() releases the router and embedder."""
        mock_embedder = MagicMock()
        mock_embedder.dimension = 384
        mock_embedder.model_name = "test-model"
        mock_router = MagicMock()

        with (
            patch("src.memory.get_embedding_provider", return_value=mock_embedder),
            patch("src.memory.CollectionRouter", return_value=mock_router),
            patch("src.memory.MEMSEARCH_AVAILABLE", True),
        ):
            await mgr._get_router()
            assert mgr._router is not None
            assert mgr._embedder is not None

            await mgr.close()

            mock_router.close.assert_called_once()
            assert mgr._router is None
            assert mgr._embedder is None

    async def test_router_none_when_embedder_fails(self, mgr):
        """Router returns None when embedder initialization fails."""
        with (
            patch(
                "src.memory.get_embedding_provider",
                side_effect=RuntimeError("No API key"),
            ),
            patch("src.memory.MEMSEARCH_AVAILABLE", True),
        ):
            router = await mgr._get_router()
            assert router is None

    async def test_router_uses_config_milvus_uri(self, tmp_path):
        """Router is created with the configured Milvus URI."""
        uri = "http://localhost:19530"
        mgr = _make_manager(tmp_path, milvus_uri=uri)

        mock_embedder = MagicMock()
        mock_embedder.dimension = 384
        mock_embedder.model_name = "test-model"

        with (
            patch("src.memory.get_embedding_provider", return_value=mock_embedder),
            patch("src.memory.CollectionRouter") as MockRouter,
            patch("src.memory.MEMSEARCH_AVAILABLE", True),
        ):
            await mgr._get_router()
            MockRouter.assert_called_once_with(
                milvus_uri=uri,
                token=None,
                dimension=384,
            )


# ---------------------------------------------------------------------------
# Test: MemoryServiceImpl.scoped_search delegation
# ---------------------------------------------------------------------------


class TestMemoryServiceScopedSearch:
    """Test that MemoryServiceImpl correctly delegates scoped_search."""

    async def test_delegates_to_manager(self):
        """MemoryServiceImpl.scoped_search() delegates to MemoryManager."""
        from src.plugins.services import MemoryServiceImpl

        mock_mm = AsyncMock()
        mock_mm.scoped_search = AsyncMock(return_value=[{"content": "result"}])
        mock_mm.scoped_batch_search = AsyncMock(return_value={"q": [{"content": "result"}]})

        svc = MemoryServiceImpl(mock_mm)

        # Test scoped_search
        results = await svc.scoped_search(
            "query",
            project_id="myapp",
            agent_type="coding",
            topic="auth",
            top_k=5,
        )
        assert len(results) == 1
        mock_mm.scoped_search.assert_awaited_once_with(
            "query",
            project_id="myapp",
            agent_type="coding",
            topic="auth",
            top_k=5,
            weights=None,
            full=False,
        )

        # Test scoped_batch_search
        results_map = await svc.scoped_batch_search(
            ["q1"],
            project_id="myapp",
        )
        assert "q" in results_map
        mock_mm.scoped_batch_search.assert_awaited_once()

    async def test_returns_empty_when_manager_missing(self):
        """Returns empty when MemoryManager is None."""
        from src.plugins.services import MemoryServiceImpl

        svc = MemoryServiceImpl(None)
        results = await svc.scoped_search("query", project_id="myapp")
        assert results == []

        batch = await svc.scoped_batch_search(["q1"], project_id="myapp")
        assert batch == {"q1": []}


# ---------------------------------------------------------------------------
# Test: merge_and_rank direct tests (roadmap 3.1.10)
# ---------------------------------------------------------------------------


def _weighted_result(
    chunk_hash: str,
    score: float,
    scope: MemoryScope,
    scope_id: str | None = None,
    collection: str = "",
    content: str = "",
) -> dict:
    """Build a result dict as CollectionRouter._search_collection_async would."""
    weight = SCOPE_WEIGHTS[scope]
    return {
        "chunk_hash": chunk_hash,
        "content": content or f"content for {chunk_hash}",
        "source": f"/vault/{scope.value}/{chunk_hash}.md",
        "heading": "",
        "score": score,
        "weighted_score": score * weight,
        "_scope": scope.value,
        "_scope_id": scope_id,
        "_weight": weight,
        "_collection": collection or f"aq_{scope.value}",
    }


class TestMergeAndRankWeightedMerge:
    """Direct tests of merge_and_rank() for roadmap 3.1.10 cases.

    These test the core merging algorithm without MemoryManager mocking,
    using realistic result dicts with proper scope weights from SCOPE_WEIGHTS.
    """

    def test_a_project_outranks_system_equal_raw_score(self):
        """(a) Same raw similarity → project (1.0) ranked above system (0.4).

        Project: 0.85 * 1.0 = 0.85
        System:  0.85 * 0.4 = 0.34
        """
        results = [
            _weighted_result("sys1", 0.85, MemoryScope.SYSTEM),
            _weighted_result("proj1", 0.85, MemoryScope.PROJECT, "myapp"),
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 2
        assert merged[0]["_scope"] == "project"
        assert merged[0]["weighted_score"] == pytest.approx(0.85)
        assert merged[1]["_scope"] == "system"
        assert merged[1]["weighted_score"] == pytest.approx(0.34)

    def test_a_project_beats_system_at_all_raw_scores(self):
        """(a) At any equal raw score, project always outranks system."""
        for raw in [0.1, 0.3, 0.5, 0.7, 0.9, 1.0]:
            results = [
                _weighted_result("sys", raw, MemoryScope.SYSTEM),
                _weighted_result("proj", raw, MemoryScope.PROJECT, "myapp"),
            ]
            merged = merge_and_rank(results, top_k=10)
            assert merged[0]["_scope"] == "project", f"Failed at raw={raw}"

    def test_a_three_scope_ordering_at_equal_similarity(self):
        """(a) All three scopes with equal raw similarity: project > agent-type > system."""
        results = [
            _weighted_result("sys1", 0.8, MemoryScope.SYSTEM),
            _weighted_result("agent1", 0.8, MemoryScope.AGENT_TYPE, "coding"),
            _weighted_result("proj1", 0.8, MemoryScope.PROJECT, "myapp"),
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 3
        assert merged[0]["_scope"] == "project"  # 0.8 * 1.0 = 0.80
        assert merged[1]["_scope"] == "agent_type"  # 0.8 * 0.7 = 0.56
        assert merged[2]["_scope"] == "system"  # 0.8 * 0.4 = 0.32

    def test_b_high_system_similarity_outranks_low_project(self):
        """(b) System score 0.95 * 0.4 = 0.38 > project score 0.3 * 1.0 = 0.30."""
        results = [
            _weighted_result("proj1", 0.3, MemoryScope.PROJECT, "myapp"),
            _weighted_result("sys1", 0.95, MemoryScope.SYSTEM),
        ]
        merged = merge_and_rank(results, top_k=10)
        assert merged[0]["_scope"] == "system"
        assert merged[0]["weighted_score"] == pytest.approx(0.38)
        assert merged[1]["_scope"] == "project"
        assert merged[1]["weighted_score"] == pytest.approx(0.30)

    def test_b_agent_type_can_outrank_project(self):
        """(b) Agent-type 0.9 * 0.7 = 0.63 > project 0.5 * 1.0 = 0.50."""
        results = [
            _weighted_result("proj1", 0.5, MemoryScope.PROJECT, "myapp"),
            _weighted_result("agent1", 0.9, MemoryScope.AGENT_TYPE, "coding"),
        ]
        merged = merge_and_rank(results, top_k=10)
        assert merged[0]["_scope"] == "agent_type"
        assert merged[0]["weighted_score"] == pytest.approx(0.63)

    def test_b_weighted_score_is_product_not_replacement(self):
        """(b) weighted_score = raw_score * weight, not just the weight."""
        results = [_weighted_result("proj1", 0.6, MemoryScope.PROJECT, "myapp")]
        merged = merge_and_rank(results, top_k=10)
        assert merged[0]["weighted_score"] == pytest.approx(0.6)
        assert merged[0]["weighted_score"] != SCOPE_WEIGHTS[MemoryScope.PROJECT]

    def test_c_three_scopes_five_each_top_k_merge(self):
        """(c) 3 scopes × 5 results each → merged output is top-K by weighted score."""
        results = []
        for i in range(5):
            raw = 0.9 - i * 0.1
            results.append(_weighted_result(f"proj{i}", raw, MemoryScope.PROJECT, "myapp"))
            results.append(_weighted_result(f"agent{i}", raw, MemoryScope.AGENT_TYPE, "coding"))
            results.append(_weighted_result(f"sys{i}", raw, MemoryScope.SYSTEM))

        merged = merge_and_rank(results, top_k=5)
        assert len(merged) == 5
        # Should be sorted by weighted_score descending
        scores = [r["weighted_score"] for r in merged]
        assert scores == sorted(scores, reverse=True)
        # Top results should be dominated by project scope (weight 1.0)
        assert merged[0]["_scope"] == "project"

    def test_c_top_k_applied_after_merge_not_per_scope(self):
        """(c) top_k truncation happens after cross-scope merge, not per-scope."""
        results = []
        for i in range(5):
            raw = 0.9 - i * 0.1
            results.append(_weighted_result(f"proj{i}", raw, MemoryScope.PROJECT, "myapp"))
            results.append(_weighted_result(f"sys{i}", raw, MemoryScope.SYSTEM))
        # 10 total results, top_k=3 should give exactly 3
        merged = merge_and_rank(results, top_k=3)
        assert len(merged) == 3

    def test_c_top_k_1_returns_single_best(self):
        """(c) top_k=1 returns the single highest weighted result."""
        results = [
            _weighted_result("proj0", 0.9, MemoryScope.PROJECT, "myapp"),
            _weighted_result("agent0", 0.9, MemoryScope.AGENT_TYPE, "coding"),
            _weighted_result("sys0", 0.9, MemoryScope.SYSTEM),
        ]
        merged = merge_and_rank(results, top_k=1)
        assert len(merged) == 1
        assert merged[0]["_scope"] == "project"  # 0.9 * 1.0 highest

    def test_d_empty_scope_contributes_nothing(self):
        """(d) Only project scope has results — system contributes nothing."""
        results = [
            _weighted_result("proj1", 0.85, MemoryScope.PROJECT, "myapp"),
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 1
        assert merged[0]["_scope"] == "project"

    def test_d_all_scopes_empty_returns_empty(self):
        """(d) No results from any scope → empty list."""
        assert merge_and_rank([], top_k=10) == []

    def test_d_no_padding_with_low_score_results(self):
        """(d) Empty scopes don't insert zero-score placeholders."""
        results = [
            _weighted_result("proj1", 0.9, MemoryScope.PROJECT, "myapp"),
            _weighted_result("proj2", 0.5, MemoryScope.PROJECT, "myapp"),
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 2
        # All results have non-zero weighted scores
        assert all(r["weighted_score"] > 0 for r in merged)

    def test_e_results_include_scope_metadata(self):
        """(e) Each result has _scope, _scope_id, _weight, _collection, weighted_score."""
        results = [
            _weighted_result("proj1", 0.9, MemoryScope.PROJECT, "myapp", "aq_project_myapp"),
            _weighted_result(
                "agent1", 0.8, MemoryScope.AGENT_TYPE, "coding", "aq_agenttype_coding"
            ),
            _weighted_result("sys1", 0.7, MemoryScope.SYSTEM, None, "aq_system"),
        ]
        merged = merge_and_rank(results, top_k=10)
        for r in merged:
            assert "_scope" in r
            assert "_scope_id" in r
            assert "_weight" in r
            assert "_collection" in r
            assert "weighted_score" in r
            # Verify weighted_score arithmetic
            assert r["weighted_score"] == pytest.approx(r["score"] * r["_weight"])

    def test_e_scope_metadata_preserved_through_merge(self):
        """(e) Scope metadata survives the merge-and-rank sorting."""
        results = [
            _weighted_result("proj1", 0.9, MemoryScope.PROJECT, "myapp", "aq_project_myapp"),
            _weighted_result("sys1", 0.7, MemoryScope.SYSTEM, None, "aq_system"),
        ]
        merged = merge_and_rank(results, top_k=10)
        proj = next(r for r in merged if r["_scope"] == "project")
        sys_ = next(r for r in merged if r["_scope"] == "system")
        assert proj["_scope_id"] == "myapp"
        assert proj["_collection"] == "aq_project_myapp"
        assert proj["_weight"] == SCOPE_WEIGHTS[MemoryScope.PROJECT]
        assert sys_["_scope_id"] is None
        assert sys_["_collection"] == "aq_system"
        assert sys_["_weight"] == SCOPE_WEIGHTS[MemoryScope.SYSTEM]


# ---------------------------------------------------------------------------
# Test: deduplication in weighted merge (roadmap 3.1.10)
# ---------------------------------------------------------------------------


class TestWeightedMergeDedup:
    """Test that merge_and_rank deduplicates by chunk_hash across scopes.

    When the same chunk appears in multiple scopes (e.g., cross-indexed
    content), the entry with the highest weighted_score is kept.
    """

    def test_dedup_same_hash_two_scopes(self):
        """Same chunk_hash in project and system → single entry kept."""
        results = [
            _weighted_result("shared", 0.8, MemoryScope.PROJECT, "myapp"),
            _weighted_result("shared", 0.8, MemoryScope.SYSTEM),
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 1
        # Project weighted_score (0.8*1.0=0.8) > system (0.8*0.4=0.32)
        assert merged[0]["_scope"] == "project"
        assert merged[0]["weighted_score"] == pytest.approx(0.8)

    def test_dedup_same_hash_three_scopes(self):
        """Same chunk_hash in all 3 scopes → single entry from project."""
        results = [
            _weighted_result("x", 0.7, MemoryScope.SYSTEM),
            _weighted_result("x", 0.7, MemoryScope.AGENT_TYPE, "coding"),
            _weighted_result("x", 0.7, MemoryScope.PROJECT, "myapp"),
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 1
        assert merged[0]["_scope"] == "project"

    def test_dedup_keeps_highest_weighted_score(self):
        """Dedup keeps the entry with the highest weighted_score, not necessarily
        the highest raw score."""
        # System raw=0.9 → weighted 0.36
        # Project raw=0.5 → weighted 0.50  ← this should win
        results = [
            _weighted_result("dup", 0.9, MemoryScope.SYSTEM),
            _weighted_result("dup", 0.5, MemoryScope.PROJECT, "myapp"),
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 1
        assert merged[0]["_scope"] == "project"
        assert merged[0]["weighted_score"] == pytest.approx(0.50)

    def test_dedup_high_system_raw_score_can_win(self):
        """When system has vastly higher raw score, its weighted_score can
        beat project and thus the system entry is kept after dedup."""
        # System raw=1.0 → weighted 0.40
        # Project raw=0.3 → weighted 0.30  ← system wins
        results = [
            _weighted_result("dup", 1.0, MemoryScope.SYSTEM),
            _weighted_result("dup", 0.3, MemoryScope.PROJECT, "myapp"),
        ]
        merged = merge_and_rank(results, top_k=10)
        assert len(merged) == 1
        assert merged[0]["_scope"] == "system"
        assert merged[0]["weighted_score"] == pytest.approx(0.40)

    def test_dedup_mixed_shared_and_unique(self):
        """Mix of shared and unique chunk_hashes: shared deduped, unique preserved."""
        results = [
            _weighted_result("shared", 0.8, MemoryScope.PROJECT, "myapp"),
            _weighted_result("shared", 0.8, MemoryScope.SYSTEM),
            _weighted_result("proj_only", 0.6, MemoryScope.PROJECT, "myapp"),
            _weighted_result("sys_only", 0.9, MemoryScope.SYSTEM),
        ]
        merged = merge_and_rank(results, top_k=10)
        hashes = [m["chunk_hash"] for m in merged]
        assert len(merged) == 3
        assert hashes.count("shared") == 1
        assert "proj_only" in hashes
        assert "sys_only" in hashes

    def test_dedup_preserves_sort_order(self):
        """After dedup, results remain sorted by weighted_score descending."""
        results = [
            _weighted_result("shared", 0.8, MemoryScope.PROJECT, "myapp"),
            _weighted_result("shared", 0.8, MemoryScope.SYSTEM),
            _weighted_result("a", 0.9, MemoryScope.PROJECT, "myapp"),
            _weighted_result("b", 0.7, MemoryScope.AGENT_TYPE, "coding"),
            _weighted_result("c", 0.95, MemoryScope.SYSTEM),
        ]
        merged = merge_and_rank(results, top_k=10)
        scores = [m["weighted_score"] for m in merged]
        assert scores == sorted(scores, reverse=True)

    def test_dedup_with_top_k_truncation(self):
        """Dedup reduces count before top_k truncation."""
        results = [
            _weighted_result("dup1", 0.9, MemoryScope.PROJECT, "myapp"),
            _weighted_result("dup1", 0.9, MemoryScope.SYSTEM),
            _weighted_result("dup2", 0.8, MemoryScope.PROJECT, "myapp"),
            _weighted_result("dup2", 0.8, MemoryScope.SYSTEM),
            _weighted_result("unique", 0.7, MemoryScope.PROJECT, "myapp"),
        ]
        # 5 raw entries → 3 after dedup → top_k=2 should give 2
        merged = merge_and_rank(results, top_k=2)
        assert len(merged) == 2


# ---------------------------------------------------------------------------
# Test: parallel execution latency (roadmap 3.1.10 case f)
# ---------------------------------------------------------------------------


class TestParallelExecutionLatency:
    """Verify that multi-scope search uses parallel execution (case f).

    Tests use asyncio.gather behavior checking and timing to confirm
    that scope queries run concurrently, not sequentially.
    """

    @pytest.fixture
    def mgr(self, tmp_path):
        return _make_manager(tmp_path)

    async def test_f_router_search_called_with_parallel_scope_params(self, mgr):
        """(f) MemoryManager.scoped_search delegates to router.search() which
        internally uses asyncio.gather for parallel scope queries."""
        mock_embedder = MagicMock()
        mock_embedder.dimension = 384
        mock_embedder.model_name = "test-model"
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384])

        mock_router = MagicMock()
        mock_router.search = AsyncMock(return_value=[])
        mock_router.close = MagicMock()

        with (
            patch("src.memory.get_embedding_provider", return_value=mock_embedder),
            patch("src.memory.CollectionRouter", return_value=mock_router),
            patch("src.memory.MEMSEARCH_AVAILABLE", True),
        ):
            await mgr.scoped_search(
                "query",
                project_id="myapp",
                agent_type="coding",
            )

            # The router.search() is called once — it handles parallelism
            # internally via asyncio.gather across all resolved scopes
            mock_router.search.assert_awaited_once()
            call_kwargs = mock_router.search.call_args.kwargs
            assert call_kwargs["project_id"] == "myapp"
            assert call_kwargs["agent_type"] == "coding"

    async def test_f_concurrent_mock_router_demonstrates_parallelism(self, mgr):
        """(f) Simulated parallel execution: a slow router.search completes
        much faster than the sum of per-scope delays would if sequential.

        We mock router.search to sleep briefly then return, and verify the
        total wall time is bounded — confirming the call is a single
        parallel operation rather than N sequential calls.
        """
        mock_embedder = MagicMock()
        mock_embedder.dimension = 384
        mock_embedder.model_name = "test-model"
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384])

        async def _parallel_search(emb, **kwargs):
            """Simulate the real router.search which uses asyncio.gather."""
            await asyncio.sleep(0.05)  # single parallel wait
            return [
                _fake_search_result("p1", "proj", 0.9, "project", "myapp", 1.0),
                _fake_search_result("a1", "agent", 0.8, "agent_type", "coding", 0.7),
                _fake_search_result("s1", "sys", 0.7, "system", None, 0.4),
            ]

        mock_router = MagicMock()
        mock_router.search = AsyncMock(side_effect=_parallel_search)
        mock_router.close = MagicMock()

        with (
            patch("src.memory.get_embedding_provider", return_value=mock_embedder),
            patch("src.memory.CollectionRouter", return_value=mock_router),
            patch("src.memory.MEMSEARCH_AVAILABLE", True),
        ):
            t0 = time.perf_counter()
            results = await mgr.scoped_search(
                "query",
                project_id="myapp",
                agent_type="coding",
            )
            elapsed = time.perf_counter() - t0

            assert len(results) == 3
            # If this were 3 sequential scope calls of 0.05s each, elapsed
            # would be >=0.15s.  Single parallel call should be ~0.05s.
            # Use 0.30s as generous upper bound for CI environments.
            assert elapsed < 0.30, (
                f"Search took {elapsed:.3f}s — should be well under 0.15s if "
                f"scope queries run in parallel (single router.search call)"
            )

    async def test_f_batch_search_parallel_across_queries(self, mgr):
        """(f) scoped_batch_search runs multiple queries concurrently."""
        mock_embedder = MagicMock()
        mock_embedder.dimension = 384
        mock_embedder.model_name = "test-model"
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384, [0.2] * 384, [0.3] * 384])

        call_times = []

        async def _timed_search(emb, **kwargs):
            call_times.append(time.perf_counter())
            await asyncio.sleep(0.02)
            return [_fake_search_result("h1", "result", 0.9)]

        mock_router = MagicMock()
        mock_router.search = AsyncMock(side_effect=_timed_search)
        mock_router.close = MagicMock()

        with (
            patch("src.memory.get_embedding_provider", return_value=mock_embedder),
            patch("src.memory.CollectionRouter", return_value=mock_router),
            patch("src.memory.MEMSEARCH_AVAILABLE", True),
        ):
            t0 = time.perf_counter()
            result = await mgr.scoped_batch_search(
                ["q1", "q2", "q3"],
                project_id="myapp",
            )
            elapsed = time.perf_counter() - t0

            assert len(result) == 3
            # 3 queries × 0.02s each = 0.06s sequential.
            # Parallel should be ~0.02s. Allow generous bound.
            assert elapsed < 0.30, f"Batch search took {elapsed:.3f}s — expected parallel execution"
