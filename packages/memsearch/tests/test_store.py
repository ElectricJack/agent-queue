"""Tests for the Milvus store."""

from pathlib import Path

import pytest

from memsearch.store import MilvusStore


@pytest.fixture
def store(tmp_path: Path):
    db = tmp_path / "test_milvus.db"
    s = MilvusStore(uri=str(db), dimension=4)
    yield s
    s.close()


def test_upsert_and_search(store: MilvusStore):
    chunks = [
        {
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Hello world",
            "source": "test.md",
            "heading": "Intro",
            "chunk_hash": "h1",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
        },
        {
            "embedding": [0.0, 1.0, 0.0, 0.0],
            "content": "Goodbye world",
            "source": "test.md",
            "heading": "Outro",
            "chunk_hash": "h2",
            "heading_level": 1,
            "start_line": 6,
            "end_line": 10,
        },
    ]
    n = store.upsert(chunks)
    assert n == 2

    results = store.search([1.0, 0.0, 0.0, 0.0], top_k=1)
    assert len(results) >= 1
    assert results[0]["content"] == "Hello world"


def test_delete_by_source(store: MilvusStore):
    chunks = [
        {
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "A",
            "source": "a.md",
            "heading": "",
            "chunk_hash": "ha",
            "heading_level": 0,
            "start_line": 1,
            "end_line": 1,
        },
        {
            "embedding": [0.0, 1.0, 0.0, 0.0],
            "content": "B",
            "source": "b.md",
            "heading": "",
            "chunk_hash": "hb",
            "heading_level": 0,
            "start_line": 1,
            "end_line": 1,
        },
    ]
    store.upsert(chunks)
    store.delete_by_source("a.md")
    results = store.search([1.0, 0.0, 0.0, 0.0], top_k=10)
    sources = {r["source"] for r in results}
    assert "a.md" not in sources


def test_upsert_is_idempotent(store: MilvusStore):
    chunk = {
        "embedding": [1.0, 0.0, 0.0, 0.0],
        "content": "Same content",
        "source": "test.md",
        "heading": "",
        "chunk_hash": "same_hash",
        "heading_level": 0,
        "start_line": 1,
        "end_line": 1,
        "doc_type": "markdown",
    }
    store.upsert([chunk])
    store.upsert([chunk])
    results = store.search([1.0, 0.0, 0.0, 0.0], top_k=10)
    hashes = [r["chunk_hash"] for r in results]
    assert hashes.count("same_hash") == 1


def test_hybrid_search(store: MilvusStore):
    chunks = [
        {
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Redis caching with TTL and LRU eviction policy",
            "source": "test.md",
            "heading": "Caching",
            "chunk_hash": "h_redis",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
        },
        {
            "embedding": [0.0, 1.0, 0.0, 0.0],
            "content": "PostgreSQL database migration and schema changes",
            "source": "test.md",
            "heading": "Database",
            "chunk_hash": "h_pg",
            "heading_level": 1,
            "start_line": 6,
            "end_line": 10,
        },
    ]
    store.upsert(chunks)

    # Hybrid search: BM25 should boost the Redis result for keyword "Redis"
    results = store.search(
        [0.5, 0.5, 0.0, 0.0],  # ambiguous dense vector
        query_text="Redis caching",
        top_k=2,
    )
    assert len(results) >= 1
    assert results[0]["content"].startswith("Redis")


def test_dimension_mismatch(tmp_path: Path):
    db = str(tmp_path / "dim_test.db")
    # Create collection with dim=4
    s1 = MilvusStore(uri=db, dimension=4)
    s1.close()
    # Re-open with dim=8 — should raise ValueError
    with pytest.raises(ValueError, match="Embedding dimension mismatch"):
        MilvusStore(uri=db, dimension=8)


def test_drop(store: MilvusStore):
    chunk = {
        "embedding": [1.0, 0.0, 0.0, 0.0],
        "content": "Will be dropped",
        "source": "test.md",
        "heading": "",
        "chunk_hash": "hd",
        "heading_level": 0,
        "start_line": 1,
        "end_line": 1,
        "doc_type": "markdown",
    }
    store.upsert([chunk])
    store.drop()
    # After drop, collection is gone — re-ensure should work
    store._ensure_collection()
    results = store.search([1.0, 0.0, 0.0, 0.0], top_k=10)
    assert len(results) == 0


def test_collection_description(tmp_path: Path):
    """Collection should store the description when provided."""
    db = str(tmp_path / "desc_test.db")
    desc = "myproject | openai/text-embedding-3-small"
    s = MilvusStore(uri=db, dimension=4, description=desc)
    info = s._client.describe_collection(s._collection)
    assert info.get("description") == desc
    s.close()


def test_collection_description_empty_by_default(tmp_path: Path):
    """Collection should have empty description when not provided."""
    db = str(tmp_path / "desc_default_test.db")
    s = MilvusStore(uri=db, dimension=4)
    info = s._client.describe_collection(s._collection)
    assert info.get("description") == ""
    s.close()


# ---- Unified schema tests (entry_type, KV, temporal, topic, tags) ----------


def test_unified_schema_fields_exist(store: MilvusStore):
    """Collection should have all unified schema fields."""
    info = store._client.describe_collection(store._collection)
    field_names = {f["name"] for f in info.get("fields", [])}
    expected = {
        "chunk_hash",
        "entry_type",
        "embedding",
        "content",
        "sparse_vector",
        "original",
        "kv_namespace",
        "kv_key",
        "kv_value",
        "valid_from",
        "valid_to",
        "topic",
        "source",
        "tags",
        "updated_at",
        "heading",
        "heading_level",
        "start_line",
        "end_line",
    }
    assert expected.issubset(field_names), f"Missing fields: {expected - field_names}"


def test_upsert_defaults_applied(store: MilvusStore):
    """Upsert without new fields should apply defaults automatically."""
    chunk = {
        "embedding": [1.0, 0.0, 0.0, 0.0],
        "content": "Hello world",
        "source": "test.md",
        "heading": "Intro",
        "chunk_hash": "defaults_test",
        "heading_level": 1,
        "start_line": 1,
        "end_line": 5,
    }
    store.upsert([chunk])
    results = store.query(filter_expr='chunk_hash == "defaults_test"')
    assert len(results) == 1
    r = results[0]
    assert r["entry_type"] == "document"
    assert r["original"] == ""
    assert r["kv_namespace"] == ""
    assert r["kv_key"] == ""
    assert r["kv_value"] == ""
    assert r["valid_from"] == 0
    assert r["valid_to"] == 0
    assert r["topic"] == ""
    assert r["tags"] == "[]"
    assert r["updated_at"] == 0


def test_kv_entry(store: MilvusStore):
    """KV entries can be stored and queried via scalar filters."""
    kv = {
        "chunk_hash": "kv_test_1",
        "entry_type": "kv",
        "embedding": [0.0, 0.0, 0.0, 0.0],
        "content": "",
        "original": "",
        "source": "facts.md",
        "heading": "",
        "heading_level": 0,
        "start_line": 0,
        "end_line": 0,
        "kv_namespace": "project",
        "kv_key": "test_command",
        "kv_value": '"pytest tests/ -v"',
        "valid_from": 0,
        "valid_to": 0,
        "topic": "",
        "tags": "[]",
        "updated_at": 1700000000,
    }
    store.upsert([kv])
    results = store.query(filter_expr='entry_type == "kv" AND kv_namespace == "project" AND kv_key == "test_command"')
    assert len(results) == 1
    assert results[0]["kv_value"] == '"pytest tests/ -v"'


def test_temporal_entry(store: MilvusStore):
    """Temporal entries can be stored and queried."""
    entries = [
        {
            "chunk_hash": "temporal_1",
            "entry_type": "temporal",
            "embedding": [0.0, 0.0, 0.0, 0.0],
            "content": "deploy branch was main",
            "original": "",
            "source": "facts.md",
            "heading": "",
            "heading_level": 0,
            "start_line": 0,
            "end_line": 0,
            "kv_namespace": "",
            "kv_key": "deploy_branch",
            "kv_value": '"main"',
            "valid_from": 1700000000,
            "valid_to": 1710000000,
            "topic": "",
            "tags": "[]",
            "updated_at": 1700000000,
        },
        {
            "chunk_hash": "temporal_2",
            "entry_type": "temporal",
            "embedding": [0.0, 0.0, 0.0, 0.0],
            "content": "deploy branch changed to release",
            "original": "",
            "source": "facts.md",
            "heading": "",
            "heading_level": 0,
            "start_line": 0,
            "end_line": 0,
            "kv_namespace": "",
            "kv_key": "deploy_branch",
            "kv_value": '"release"',
            "valid_from": 1710000000,
            "valid_to": 1720000000,
            "topic": "",
            "tags": "[]",
            "updated_at": 1710000000,
        },
    ]
    store.upsert(entries)

    # Query full history by entry_type + key
    results = store.query(filter_expr='entry_type == "temporal" AND kv_key == "deploy_branch"')
    assert len(results) == 2
    values = {r["kv_value"] for r in results}
    assert values == {'"main"', '"release"'}

    # Verify temporal fields are stored correctly
    by_hash = {r["chunk_hash"]: r for r in results}
    assert by_hash["temporal_1"]["valid_from"] == 1700000000
    assert by_hash["temporal_1"]["valid_to"] == 1710000000
    assert by_hash["temporal_2"]["valid_from"] == 1710000000
    assert by_hash["temporal_2"]["valid_to"] == 1720000000

    # Windowed INT64 filter (tested in isolation — Milvus Lite has
    # limitations with complex multi-type AND expressions)
    results = store.query(filter_expr="valid_from <= 1705000000")
    matched_hashes = {r["chunk_hash"] for r in results}
    assert "temporal_1" in matched_hashes


def test_topic_and_tags_filtering(store: MilvusStore):
    """Topic and tags fields support scalar filtering."""
    chunks = [
        {
            "chunk_hash": "topic_1",
            "entry_type": "document",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Authentication uses JWT tokens",
            "original": "Authentication uses JWT tokens with RS256 signing",
            "source": "auth.md",
            "heading": "Auth",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "kv_namespace": "",
            "kv_key": "",
            "kv_value": "",
            "valid_from": 0,
            "valid_to": 0,
            "topic": "authentication",
            "tags": '["security", "jwt"]',
            "updated_at": 1700000000,
        },
        {
            "chunk_hash": "topic_2",
            "entry_type": "document",
            "embedding": [0.0, 1.0, 0.0, 0.0],
            "content": "Unit tests use pytest fixtures",
            "original": "Unit tests use pytest fixtures for setup",
            "source": "testing.md",
            "heading": "Testing",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "kv_namespace": "",
            "kv_key": "",
            "kv_value": "",
            "valid_from": 0,
            "valid_to": 0,
            "topic": "testing",
            "tags": '["pytest", "testing"]',
            "updated_at": 1700000000,
        },
    ]
    store.upsert(chunks)

    # Filter by topic
    results = store.query(filter_expr='topic == "authentication"')
    assert len(results) == 1
    assert results[0]["chunk_hash"] == "topic_1"

    # Filter by tags (substring match on JSON array)
    results = store.query(filter_expr='tags like "%pytest%"')
    assert len(results) == 1
    assert results[0]["chunk_hash"] == "topic_2"


# ---- Topic-filtered hybrid search tests ------------------------------------


def test_topic_filter_in_hybrid_search(store: MilvusStore):
    """Hybrid search with topic filter returns only matching topic + untagged entries."""
    chunks = [
        {
            "chunk_hash": "tf_auth",
            "entry_type": "document",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "JWT token refresh requires scope re-request",
            "source": "auth.md",
            "heading": "Auth",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "authentication",
        },
        {
            "chunk_hash": "tf_test",
            "entry_type": "document",
            "embedding": [0.0, 1.0, 0.0, 0.0],
            "content": "Use pytest fixtures for test setup and teardown",
            "source": "testing.md",
            "heading": "Testing",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "testing",
        },
        {
            "chunk_hash": "tf_general",
            "entry_type": "document",
            "embedding": [0.5, 0.5, 0.0, 0.0],
            "content": "Follow project coding conventions for all modules",
            "source": "general.md",
            "heading": "General",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "",  # untagged — should be included in all topic searches
        },
    ]
    store.upsert(chunks)

    # Search with topic filter for "authentication"
    filter_expr = '(topic == "authentication" or topic == "")'
    results = store.search(
        [1.0, 0.0, 0.0, 0.0],
        query_text="JWT token",
        top_k=10,
        filter_expr=filter_expr,
    )
    result_hashes = {r["chunk_hash"] for r in results}
    assert "tf_auth" in result_hashes, "Should include the matching-topic entry"
    assert "tf_general" in result_hashes, "Should include untagged entries"
    assert "tf_test" not in result_hashes, "Should exclude entries with a different topic"


def test_topic_filter_with_source_prefix(store: MilvusStore):
    """Topic filter combines correctly with source prefix filter."""
    chunks = [
        {
            "chunk_hash": "combo_1",
            "entry_type": "document",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Auth in project A",
            "source": "/projects/a/auth.md",
            "heading": "",
            "heading_level": 0,
            "start_line": 1,
            "end_line": 1,
            "topic": "authentication",
        },
        {
            "chunk_hash": "combo_2",
            "entry_type": "document",
            "embedding": [0.0, 1.0, 0.0, 0.0],
            "content": "Auth in project B",
            "source": "/projects/b/auth.md",
            "heading": "",
            "heading_level": 0,
            "start_line": 1,
            "end_line": 1,
            "topic": "authentication",
        },
        {
            "chunk_hash": "combo_3",
            "entry_type": "document",
            "embedding": [0.0, 0.0, 1.0, 0.0],
            "content": "Testing in project A",
            "source": "/projects/a/testing.md",
            "heading": "",
            "heading_level": 0,
            "start_line": 1,
            "end_line": 1,
            "topic": "testing",
        },
    ]
    store.upsert(chunks)

    # Combined filter: source prefix AND topic
    filter_expr = 'source like "/projects/a/%" and (topic == "authentication" or topic == "")'
    results = store.search(
        [1.0, 0.0, 0.0, 0.0],
        query_text="authentication",
        top_k=10,
        filter_expr=filter_expr,
    )
    result_hashes = {r["chunk_hash"] for r in results}
    assert "combo_1" in result_hashes, "Should match: right source + right topic"
    assert "combo_2" not in result_hashes, "Should exclude: wrong source prefix"
    assert "combo_3" not in result_hashes, "Should exclude: wrong topic"


# ---- Topic fallback tests (MemSearch.search auto-widen) ----------------------


class _MockEmbedder:
    """Fake embedding provider for testing without API keys."""

    model_name = "mock-embed"
    dimension = 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0]] * len(texts)


@pytest.fixture
def memsearch_mock(tmp_path: Path):
    """MemSearch instance with a mock embedder (no API key needed)."""
    from unittest.mock import patch

    from memsearch.core import MemSearch

    with patch("memsearch.core.get_provider", return_value=_MockEmbedder()):
        ms = MemSearch(milvus_uri=str(tmp_path / "fallback_test.db"))
    yield ms
    ms.close()


@pytest.mark.asyncio
async def test_topic_fallback_widens_when_few_results(memsearch_mock):
    """(c) MemSearch.search() auto-widens to unfiltered when topic yields < 3 results.

    Roadmap 2.1.17(c): topic filter with < 3 results auto-widens to unfiltered
    search and returns more results (fallback per spec).
    Spec: docs/specs/design/memory-scoping.md §3 — 'If the topic filter returns
    too few results (< 3), the search automatically falls back to unfiltered search
    to avoid missing relevant cross-topic knowledge.'
    """
    ms = memsearch_mock

    # Populate: 1 rare-topic chunk + 1 untagged = 2 results with topic filter (< 3)
    # Plus 4 chunks with a different topic that should appear after fallback
    chunks = [
        {
            "chunk_hash": "rare1",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Rare topic knowledge about edge cases",
            "source": "rare.md",
            "heading": "Rare",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "rare_topic",
        },
        {
            "chunk_hash": "untagged1",
            "embedding": [0.9, 0.1, 0.0, 0.0],
            "content": "General knowledge without topic tag",
            "source": "general.md",
            "heading": "General",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "",
        },
        {
            "chunk_hash": "common1",
            "embedding": [0.8, 0.2, 0.0, 0.0],
            "content": "Common topic knowledge item one",
            "source": "common1.md",
            "heading": "Common",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "common_topic",
        },
        {
            "chunk_hash": "common2",
            "embedding": [0.7, 0.3, 0.0, 0.0],
            "content": "Common topic knowledge item two",
            "source": "common2.md",
            "heading": "Common",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "common_topic",
        },
        {
            "chunk_hash": "common3",
            "embedding": [0.6, 0.4, 0.0, 0.0],
            "content": "Common topic knowledge item three",
            "source": "common3.md",
            "heading": "Common",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "common_topic",
        },
        {
            "chunk_hash": "common4",
            "embedding": [0.5, 0.5, 0.0, 0.0],
            "content": "Common topic knowledge item four",
            "source": "common4.md",
            "heading": "Common",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "common_topic",
        },
    ]
    ms.store.upsert(chunks)

    # Search with rare_topic — only 2 matches (rare1 + untagged1), below threshold of 3
    results = await ms.search("knowledge", topic="rare_topic", top_k=10)

    # Fallback should have widened: we now get results from ALL topics
    result_hashes = {r["chunk_hash"] for r in results}
    assert len(results) >= 3, f"Fallback should have widened search to return more results, got {len(results)}"
    # Chunks from the other topic should now be included
    common_found = result_hashes & {"common1", "common2", "common3", "common4"}
    assert len(common_found) > 0, "After fallback, results should include chunks from other topics"


@pytest.mark.asyncio
async def test_topic_no_fallback_when_enough_results(memsearch_mock):
    """(d) MemSearch.search() does NOT widen when topic filter yields >= 3 results.

    Roadmap 2.1.17(d): topic filter with >= 3 results does NOT widen
    (stays filtered).
    """
    ms = memsearch_mock

    # Populate: 3 same-topic chunks + 1 untagged = 4 results >= threshold
    # Plus 2 chunks with a different topic that should NOT appear
    chunks = [
        {
            "chunk_hash": "auth1",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Authentication via OAuth tokens",
            "source": "auth1.md",
            "heading": "Auth",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "authentication",
        },
        {
            "chunk_hash": "auth2",
            "embedding": [0.9, 0.1, 0.0, 0.0],
            "content": "Authentication session management",
            "source": "auth2.md",
            "heading": "Auth",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "authentication",
        },
        {
            "chunk_hash": "auth3",
            "embedding": [0.8, 0.2, 0.0, 0.0],
            "content": "Authentication password hashing",
            "source": "auth3.md",
            "heading": "Auth",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "authentication",
        },
        {
            "chunk_hash": "untagged_n",
            "embedding": [0.7, 0.3, 0.0, 0.0],
            "content": "General project conventions",
            "source": "conventions.md",
            "heading": "Conventions",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "",
        },
        {
            "chunk_hash": "deploy1",
            "embedding": [0.6, 0.4, 0.0, 0.0],
            "content": "Deployment pipeline configuration",
            "source": "deploy.md",
            "heading": "Deploy",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "deployment",
        },
        {
            "chunk_hash": "deploy2",
            "embedding": [0.5, 0.5, 0.0, 0.0],
            "content": "Deployment rollback procedures",
            "source": "deploy2.md",
            "heading": "Deploy",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "deployment",
        },
    ]
    ms.store.upsert(chunks)

    # Search with authentication topic — 4 matches (3 auth + 1 untagged) >= 3 threshold
    results = await ms.search("authentication", topic="authentication", top_k=10)

    result_hashes = {r["chunk_hash"] for r in results}
    # Should NOT include deployment chunks (no fallback triggered)
    assert "deploy1" not in result_hashes, "Should not fallback — topic filter returned enough results"
    assert "deploy2" not in result_hashes, "Should not fallback — topic filter returned enough results"
    # Should include auth chunks and untagged
    auth_found = result_hashes & {"auth1", "auth2", "auth3"}
    assert len(auth_found) >= 1, "Should include authentication-topic chunks"


@pytest.mark.asyncio
async def test_topic_fallback_preserves_source_prefix(memsearch_mock):
    """Fallback removes topic filter but preserves source_prefix constraint."""
    ms = memsearch_mock

    # Populate: 1 chunk matching both source prefix + topic (< 3 → triggers fallback)
    # Plus chunks in different source paths that should remain excluded after fallback
    chunks = [
        {
            "chunk_hash": "projA_rare",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Rare topic in project A",
            "source": "/projects/alpha/rare.md",
            "heading": "Rare",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "rare_topic",
        },
        {
            "chunk_hash": "projA_common",
            "embedding": [0.9, 0.1, 0.0, 0.0],
            "content": "Common topic in project A",
            "source": "/projects/alpha/common.md",
            "heading": "Common",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "common_topic",
        },
        {
            "chunk_hash": "projB_rare",
            "embedding": [0.8, 0.2, 0.0, 0.0],
            "content": "Rare topic in project B",
            "source": "/projects/beta/rare.md",
            "heading": "Rare",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "rare_topic",
        },
        {
            "chunk_hash": "projB_common",
            "embedding": [0.7, 0.3, 0.0, 0.0],
            "content": "Common topic in project B",
            "source": "/projects/beta/common.md",
            "heading": "Common",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "common_topic",
        },
    ]
    ms.store.upsert(chunks)

    # Search with rare_topic + source prefix for project alpha
    # Only 1 match (projA_rare) — below threshold → fallback
    # Fallback should widen topic but KEEP source prefix
    results = await ms.search("topic", topic="rare_topic", source_prefix="/projects/alpha", top_k=10)

    result_hashes = {r["chunk_hash"] for r in results}
    # After fallback: should include projA_common (same source, different topic)
    assert "projA_common" in result_hashes, (
        "Fallback should widen topic but keep source prefix — projA_common should appear"
    )
    # Should NOT include project B chunks (source prefix still active)
    assert "projB_rare" not in result_hashes, "Source prefix should still exclude other projects after fallback"
    assert "projB_common" not in result_hashes, "Source prefix should still exclude other projects after fallback"


# ---- Roadmap 2.1.17 test cases (a)-(g) ------------------------------------
# Cases (c) and (d) are covered by the tests above:
#   (c) test_topic_fallback_widens_when_few_results
#   (d) test_topic_no_fallback_when_enough_results
# Cases (a), (b), (e), (f), (g) are below.


@pytest.mark.asyncio
async def test_topic_search_returns_only_matching_topic(memsearch_mock):
    """(a) search with topic="testing" returns only memories tagged "testing" + untagged.

    Roadmap 2.1.17(a): search with topic="testing" returns only memories
    tagged with "testing" topic.
    Spec: docs/specs/design/memory-scoping.md §3 — 'memories without a topic
    are included in all searches (no filtering)'.
    """
    ms = memsearch_mock

    # Populate: enough "testing" entries so no fallback triggers (>= 3)
    chunks = [
        {
            "chunk_hash": "testing_1",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Unit tests use pytest fixtures for setup",
            "source": "testing1.md",
            "heading": "Tests",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "testing",
        },
        {
            "chunk_hash": "testing_2",
            "embedding": [0.9, 0.1, 0.0, 0.0],
            "content": "Integration tests spin up the full server",
            "source": "testing2.md",
            "heading": "Tests",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "testing",
        },
        {
            "chunk_hash": "testing_3",
            "embedding": [0.8, 0.2, 0.0, 0.0],
            "content": "Test coverage reports are generated with pytest-cov",
            "source": "testing3.md",
            "heading": "Tests",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "testing",
        },
        {
            "chunk_hash": "untagged_a",
            "embedding": [0.7, 0.3, 0.0, 0.0],
            "content": "General project conventions apply everywhere",
            "source": "conventions.md",
            "heading": "General",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "",  # untagged — should appear in all topic searches
        },
        {
            "chunk_hash": "auth_1",
            "embedding": [0.6, 0.4, 0.0, 0.0],
            "content": "Authentication uses OAuth2 with JWT tokens",
            "source": "auth.md",
            "heading": "Auth",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "authentication",
        },
        {
            "chunk_hash": "deploy_1",
            "embedding": [0.5, 0.5, 0.0, 0.0],
            "content": "Deployment uses blue-green strategy on Kubernetes",
            "source": "deploy.md",
            "heading": "Deploy",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "deployment",
        },
    ]
    ms.store.upsert(chunks)

    results = await ms.search("testing", topic="testing", top_k=10)

    result_hashes = {r["chunk_hash"] for r in results}
    # Should include all testing-topic chunks
    testing_found = result_hashes & {"testing_1", "testing_2", "testing_3"}
    assert len(testing_found) >= 1, "Should include testing-topic chunks"
    # Should include untagged chunks (topic == "")
    assert "untagged_a" in result_hashes, "Untagged memories (topic='') should be included in all topic searches"
    # Should NOT include chunks from other topics
    assert "auth_1" not in result_hashes, "Should exclude memories tagged with a different topic"
    assert "deploy_1" not in result_hashes, "Should exclude memories tagged with a different topic"
    # No fallback should have triggered (>= 3 results from topic filter)
    for r in results:
        assert "topic_fallback" not in r, "Should not have triggered fallback — enough direct matches"


@pytest.mark.asyncio
async def test_search_without_topic_returns_all_topics(memsearch_mock):
    """(b) search without topic filter returns memories across all topics.

    Roadmap 2.1.17(b): search without topic filter returns memories across
    all topics.
    """
    ms = memsearch_mock

    chunks = [
        {
            "chunk_hash": "notopic_auth",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Authentication uses JWT tokens",
            "source": "auth.md",
            "heading": "Auth",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "authentication",
        },
        {
            "chunk_hash": "notopic_test",
            "embedding": [0.9, 0.1, 0.0, 0.0],
            "content": "Use pytest fixtures for test setup",
            "source": "testing.md",
            "heading": "Testing",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "testing",
        },
        {
            "chunk_hash": "notopic_deploy",
            "embedding": [0.8, 0.2, 0.0, 0.0],
            "content": "Deploy to Kubernetes with Helm charts",
            "source": "deploy.md",
            "heading": "Deploy",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "deployment",
        },
        {
            "chunk_hash": "notopic_general",
            "embedding": [0.7, 0.3, 0.0, 0.0],
            "content": "General coding conventions for the project",
            "source": "general.md",
            "heading": "General",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "",
        },
    ]
    ms.store.upsert(chunks)

    # Search WITHOUT topic filter — all memories should be returned
    results = await ms.search("project knowledge", top_k=10)

    result_hashes = {r["chunk_hash"] for r in results}
    assert "notopic_auth" in result_hashes, "Should include authentication-topic memory"
    assert "notopic_test" in result_hashes, "Should include testing-topic memory"
    assert "notopic_deploy" in result_hashes, "Should include deployment-topic memory"
    assert "notopic_general" in result_hashes, "Should include untagged memory"
    assert len(results) == 4, f"Expected all 4 memories, got {len(results)}"


@pytest.mark.asyncio
async def test_nonexistent_topic_falls_back_to_unfiltered(memsearch_mock):
    """(e) search with non-existent topic returns 0 filtered → falls back to unfiltered.

    Roadmap 2.1.17(e): search with non-existent topic returns 0 filtered
    results then falls back to unfiltered.
    """
    ms = memsearch_mock

    chunks = [
        {
            "chunk_hash": "exist_auth",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Authentication patterns and best practices",
            "source": "auth.md",
            "heading": "Auth",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "authentication",
        },
        {
            "chunk_hash": "exist_test",
            "embedding": [0.9, 0.1, 0.0, 0.0],
            "content": "Testing strategy and frameworks",
            "source": "testing.md",
            "heading": "Testing",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "testing",
        },
        {
            "chunk_hash": "exist_deploy",
            "embedding": [0.8, 0.2, 0.0, 0.0],
            "content": "Deployment configuration and pipelines",
            "source": "deploy.md",
            "heading": "Deploy",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "deployment",
        },
        {
            "chunk_hash": "exist_general",
            "embedding": [0.7, 0.3, 0.0, 0.0],
            "content": "General project notes and conventions",
            "source": "general.md",
            "heading": "General",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "",
        },
    ]
    ms.store.upsert(chunks)

    # "nonexistent_topic_xyz" matches ZERO chunks → 0 < 3 → fallback to unfiltered
    results = await ms.search("patterns", topic="nonexistent_topic_xyz", top_k=10)

    result_hashes = {r["chunk_hash"] for r in results}
    # Fallback should have returned results from all topics
    assert len(results) >= 1, "Non-existent topic should trigger fallback and return unfiltered results"
    # At least some of the real chunks should appear
    real_found = result_hashes & {"exist_auth", "exist_test", "exist_deploy", "exist_general"}
    assert len(real_found) >= 1, "Fallback results should contain memories regardless of their topic"
    # All fallback results should be marked
    for r in results:
        assert r.get("topic_fallback") is True, "Fallback results should have topic_fallback=True metadata"


@pytest.mark.asyncio
async def test_topic_filter_applied_as_scalar_prefilter(memsearch_mock):
    """(f) topic filter is applied as scalar pre-filter before vector similarity.

    Roadmap 2.1.17(f): topic filter is applied as scalar pre-filter before
    vector similarity (verify with query plan or mock).

    We verify by mocking the store's search method and inspecting the
    filter_expr argument to confirm the topic filter is passed as a
    scalar expression to Milvus (not applied post-hoc in Python).
    """
    from unittest.mock import MagicMock

    ms = memsearch_mock

    # Populate some data so the store is not empty
    chunks = [
        {
            "chunk_hash": "prefilter_1",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Auth content",
            "source": "auth.md",
            "heading": "Auth",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "authentication",
        },
        {
            "chunk_hash": "prefilter_2",
            "embedding": [0.9, 0.1, 0.0, 0.0],
            "content": "Testing content",
            "source": "testing.md",
            "heading": "Testing",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "testing",
        },
        {
            "chunk_hash": "prefilter_3",
            "embedding": [0.8, 0.2, 0.0, 0.0],
            "content": "More auth content",
            "source": "auth2.md",
            "heading": "Auth2",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "authentication",
        },
        {
            "chunk_hash": "prefilter_u",
            "embedding": [0.7, 0.3, 0.0, 0.0],
            "content": "Untagged content",
            "source": "general.md",
            "heading": "General",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "",
        },
    ]
    ms.store.upsert(chunks)

    # Wrap the store's search method to capture calls
    original_search = ms.store.search
    captured_calls: list[dict] = []

    def capturing_search(*args, **kwargs):
        captured_calls.append({"args": args, "kwargs": kwargs})
        return original_search(*args, **kwargs)

    ms.store.search = MagicMock(side_effect=capturing_search)

    # Perform a topic-filtered search
    await ms.search("auth content", topic="authentication", top_k=10)

    # The first call to store.search should include the topic scalar filter
    assert len(captured_calls) >= 1, "store.search should have been called"
    first_call = captured_calls[0]
    filter_expr = first_call["kwargs"].get("filter_expr", "")

    # Verify the topic filter is a scalar pre-filter expression
    assert 'topic == "authentication"' in filter_expr, (
        f"Topic filter should be a scalar pre-filter in filter_expr, got: {filter_expr}"
    )
    assert 'topic == ""' in filter_expr, (
        f"Topic filter should include untagged entries (topic == ''), got: {filter_expr}"
    )


@pytest.mark.asyncio
async def test_topic_filter_prefilter_with_source_prefix(memsearch_mock):
    """(f) scalar pre-filter includes both topic AND source_prefix when both are set.

    Extension of 2.1.17(f): verify combined filters are passed as scalar
    expressions to the store, not applied post-hoc.
    """
    from unittest.mock import MagicMock

    ms = memsearch_mock

    chunks = [
        {
            "chunk_hash": "combo_pf_1",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Auth in project A",
            "source": "/projects/a/auth.md",
            "heading": "Auth",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "authentication",
        },
    ]
    ms.store.upsert(chunks)

    original_search = ms.store.search
    captured_calls: list[dict] = []

    def capturing_search(*args, **kwargs):
        captured_calls.append({"args": args, "kwargs": kwargs})
        return original_search(*args, **kwargs)

    ms.store.search = MagicMock(side_effect=capturing_search)

    await ms.search("auth", topic="authentication", source_prefix="/projects/a", top_k=10)

    assert len(captured_calls) >= 1
    first_call = captured_calls[0]
    filter_expr = first_call["kwargs"].get("filter_expr", "")

    # Both source prefix AND topic filter should be in the scalar expression
    assert "source like" in filter_expr, f"Source prefix should be in scalar filter, got: {filter_expr}"
    assert 'topic == "authentication"' in filter_expr, f"Topic filter should be in scalar filter, got: {filter_expr}"
    # They should be joined with 'and'
    assert " and " in filter_expr, f"source_prefix and topic filters should be AND-joined, got: {filter_expr}"


@pytest.mark.asyncio
async def test_fallback_results_marked_with_metadata(memsearch_mock):
    """(g) fallback results are clearly distinguishable from direct matches.

    Roadmap 2.1.17(g): fallback results are clearly distinguishable from
    direct matches (e.g., metadata flag).

    When topic-filtered search returns < 3 results and falls back to
    unfiltered search, each result dict should contain
    ``topic_fallback=True`` so callers can distinguish fallback results
    from direct topic matches.
    """
    ms = memsearch_mock

    # Populate: 1 chunk with rare_topic + 1 untagged = 2 results (< 3 → fallback)
    # Plus chunks with a different topic that should appear in fallback
    chunks = [
        {
            "chunk_hash": "meta_rare",
            "embedding": [1.0, 0.0, 0.0, 0.0],
            "content": "Rare topic knowledge",
            "source": "rare.md",
            "heading": "Rare",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "rare_topic",
        },
        {
            "chunk_hash": "meta_untagged",
            "embedding": [0.9, 0.1, 0.0, 0.0],
            "content": "General untagged knowledge",
            "source": "general.md",
            "heading": "General",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "",
        },
        {
            "chunk_hash": "meta_common1",
            "embedding": [0.8, 0.2, 0.0, 0.0],
            "content": "Common topic knowledge item one",
            "source": "common1.md",
            "heading": "Common",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "common_topic",
        },
        {
            "chunk_hash": "meta_common2",
            "embedding": [0.7, 0.3, 0.0, 0.0],
            "content": "Common topic knowledge item two",
            "source": "common2.md",
            "heading": "Common",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "common_topic",
        },
    ]
    ms.store.upsert(chunks)

    # Search with rare_topic — 2 results (< 3) → triggers fallback
    results = await ms.search("knowledge", topic="rare_topic", top_k=10)

    assert len(results) >= 3, f"Fallback should have widened search, got {len(results)} results"
    # ALL fallback results should be marked with topic_fallback=True
    for r in results:
        assert r.get("topic_fallback") is True, (
            f"Fallback result missing topic_fallback=True: chunk_hash={r.get('chunk_hash')}"
        )

    # Now verify that direct matches do NOT have the fallback flag
    # Search with enough results (no fallback)
    chunks_extra = [
        {
            "chunk_hash": "meta_common3",
            "embedding": [0.6, 0.4, 0.0, 0.0],
            "content": "Common topic knowledge item three",
            "source": "common3.md",
            "heading": "Common",
            "heading_level": 1,
            "start_line": 1,
            "end_line": 5,
            "topic": "common_topic",
        },
    ]
    ms.store.upsert(chunks_extra)

    # common_topic has 3 entries → no fallback
    results_direct = await ms.search("knowledge", topic="common_topic", top_k=10)
    for r in results_direct:
        assert "topic_fallback" not in r, (
            f"Direct match should NOT have topic_fallback: chunk_hash={r.get('chunk_hash')}"
        )
