"""Tests for the Milvus store."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from memsearch.store import (
    MilvusStore,
    _build_collection_meta,
    _parse_collection_meta,
)


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
    results = store.query(filter_expr='chunk_hash == "defaults_test"', full=True)
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


# ---- Embedding model version tracking tests ---------------------------------


class TestCollectionMetaHelpers:
    """Unit tests for _build_collection_meta / _parse_collection_meta."""

    def test_build_meta_basic(self):
        raw = _build_collection_meta("", "openai", "text-embedding-3-small", 1536)
        data = json.loads(raw)
        assert data["_memsearch"] == 1
        assert data["provider"] == "openai"
        assert data["model"] == "text-embedding-3-small"
        assert data["dimension"] == 1536
        assert "description" not in data

    def test_build_meta_with_description(self):
        raw = _build_collection_meta("my project", "onnx", "bge-m3", 1024)
        data = json.loads(raw)
        assert data["description"] == "my project"
        assert data["provider"] == "onnx"
        assert data["model"] == "bge-m3"

    def test_parse_meta_valid(self):
        raw = json.dumps({"_memsearch": 1, "provider": "openai", "model": "m", "dimension": 4})
        result = _parse_collection_meta(raw)
        assert result is not None
        assert result["provider"] == "openai"

    def test_parse_meta_empty(self):
        assert _parse_collection_meta("") is None

    def test_parse_meta_plain_text(self):
        """Legacy plain-text descriptions return None."""
        assert _parse_collection_meta("myproject | openai/text-embedding-3-small") is None

    def test_parse_meta_invalid_json(self):
        assert _parse_collection_meta("{broken") is None

    def test_parse_meta_json_without_sentinel(self):
        """Valid JSON but missing _memsearch key → not memsearch metadata."""
        assert _parse_collection_meta('{"provider": "openai"}') is None


def test_model_info_stored_on_creation(tmp_path: Path):
    """New collections store embedding model metadata in the description."""
    db = str(tmp_path / "model_create.db")
    s = MilvusStore(
        uri=db,
        dimension=4,
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
    )
    # model_info should be available immediately after creation
    assert s.model_info is not None
    assert s.model_info["provider"] == "openai"
    assert s.model_info["model"] == "text-embedding-3-small"
    assert s.model_info["dimension"] == 4
    assert not s.needs_reindex

    # Verify the description in Milvus is JSON with metadata
    info = s._client.describe_collection(s._collection)
    desc = info.get("description", "")
    meta = _parse_collection_meta(desc)
    assert meta is not None
    assert meta["provider"] == "openai"
    assert meta["model"] == "text-embedding-3-small"
    s.close()


def test_model_info_persists_across_reopen(tmp_path: Path):
    """Model metadata survives closing and reopening the collection."""
    db = str(tmp_path / "model_persist.db")
    s1 = MilvusStore(
        uri=db,
        dimension=4,
        embedding_provider="onnx",
        embedding_model="bge-m3-onnx-int8",
    )
    s1.close()

    # Reopen with the same model
    s2 = MilvusStore(
        uri=db,
        dimension=4,
        embedding_provider="onnx",
        embedding_model="bge-m3-onnx-int8",
    )
    assert s2.model_info is not None
    assert s2.model_info["provider"] == "onnx"
    assert s2.model_info["model"] == "bge-m3-onnx-int8"
    assert s2.model_info["dimension"] == 4
    assert not s2.needs_reindex
    s2.close()


def test_model_mismatch_sets_needs_reindex(tmp_path: Path):
    """Opening a collection with a different model sets needs_reindex."""
    db = str(tmp_path / "model_mismatch.db")
    s1 = MilvusStore(
        uri=db,
        dimension=4,
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
    )
    s1.close()

    # Reopen with a different model (same dimension — not a dimension error)
    s2 = MilvusStore(
        uri=db,
        dimension=4,
        embedding_provider="openai",
        embedding_model="text-embedding-3-large",
    )
    assert s2.needs_reindex is True
    # stored_model_info should reflect the original model
    assert s2.model_info is not None
    assert s2.model_info["model"] == "text-embedding-3-small"
    s2.close()


def test_provider_change_sets_needs_reindex(tmp_path: Path):
    """Changing the embedding provider triggers needs_reindex."""
    db = str(tmp_path / "provider_change.db")
    s1 = MilvusStore(
        uri=db,
        dimension=4,
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
    )
    s1.close()

    s2 = MilvusStore(
        uri=db,
        dimension=4,
        embedding_provider="voyage",
        embedding_model="voyage-3-lite",
    )
    assert s2.needs_reindex is True
    assert s2.model_info["provider"] == "openai"  # stored = original
    s2.close()


def test_legacy_collection_no_model_info(tmp_path: Path):
    """Collections without model metadata (legacy) return model_info=None."""
    db = str(tmp_path / "legacy.db")
    # Create without model params (simulates legacy behavior)
    s1 = MilvusStore(uri=db, dimension=4)
    assert s1.model_info is None
    assert not s1.needs_reindex
    s1.close()

    # Reopen with model params — should not error, model_info stays None
    s2 = MilvusStore(
        uri=db,
        dimension=4,
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
    )
    assert s2.model_info is None  # legacy: no stored metadata
    assert not s2.needs_reindex  # can't compare without stored info
    s2.close()


def test_read_only_mode_reads_model_info(tmp_path: Path):
    """Read-only mode (dimension=None) still reads stored model metadata."""
    db = str(tmp_path / "readonly_model.db")
    s1 = MilvusStore(
        uri=db,
        dimension=4,
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
    )
    s1.close()

    # Open in read-only mode (dimension=None, no model params)
    s2 = MilvusStore(uri=db, dimension=None)
    assert s2.model_info is not None
    assert s2.model_info["provider"] == "openai"
    assert s2.model_info["model"] == "text-embedding-3-small"
    assert s2.model_info["dimension"] == 4
    assert not s2.needs_reindex  # no current model to compare
    s2.close()


def test_model_metadata_with_user_description(tmp_path: Path):
    """User-supplied description is preserved alongside model metadata."""
    db = str(tmp_path / "desc_model.db")
    s = MilvusStore(
        uri=db,
        dimension=4,
        description="my project notes",
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
    )
    info = s._client.describe_collection(s._collection)
    meta = _parse_collection_meta(info.get("description", ""))
    assert meta is not None
    assert meta["description"] == "my project notes"
    assert meta["provider"] == "openai"
    assert meta["model"] == "text-embedding-3-small"
    s.close()


# ---- Temporal fact API tests -------------------------------------------------


def test_set_temporal_basic(store: MilvusStore):
    """set_temporal creates a temporal entry retrievable by get_temporal."""
    result = store.set_temporal(
        "deploy_branch",
        "main",
        source="facts.md",
        content="deploy branch is main",
        timestamp=1700000000,
    )

    assert result["entry_type"] == "temporal"
    assert result["kv_key"] == "deploy_branch"
    assert result["kv_value"] == '"main"'
    assert result["valid_from"] == 1700000000
    assert result["valid_to"] == 0
    assert result["source"] == "facts.md"
    assert result["content"] == "deploy branch is main"
    assert "embedding" not in result  # embedding excluded from return

    # Retrievable via get_temporal at the same timestamp
    current = store.get_temporal("deploy_branch", at=1700000000)
    assert len(current) == 1
    assert current[0]["kv_value"] == '"main"'
    assert current[0]["valid_from"] == 1700000000
    assert current[0]["valid_to"] == 0


def test_set_temporal_updates_close_previous(store: MilvusStore):
    """Setting a new value closes the old entry and opens a new one."""
    # First value
    store.set_temporal("deploy_branch", "main", timestamp=1700000000)

    # Second value — should close the first
    store.set_temporal("deploy_branch", "release", timestamp=1710000000)

    # Full history should show both entries
    history = store.get_temporal_history("deploy_branch")
    assert len(history) == 2

    # First entry should be closed (valid_to == 1710000000)
    assert history[0]["kv_value"] == '"main"'
    assert history[0]["valid_from"] == 1700000000
    assert history[0]["valid_to"] == 1710000000

    # Second entry should be open (valid_to == 0)
    assert history[1]["kv_value"] == '"release"'
    assert history[1]["valid_from"] == 1710000000
    assert history[1]["valid_to"] == 0


def test_set_temporal_multiple_updates(store: MilvusStore):
    """Three successive updates produce correct history chain."""
    store.set_temporal("branch", "main", timestamp=1000)
    store.set_temporal("branch", "develop", timestamp=2000)
    store.set_temporal("branch", "release", timestamp=3000)

    history = store.get_temporal_history("branch")
    assert len(history) == 3

    assert history[0]["kv_value"] == '"main"'
    assert history[0]["valid_from"] == 1000
    assert history[0]["valid_to"] == 2000

    assert history[1]["kv_value"] == '"develop"'
    assert history[1]["valid_from"] == 2000
    assert history[1]["valid_to"] == 3000

    assert history[2]["kv_value"] == '"release"'
    assert history[2]["valid_from"] == 3000
    assert history[2]["valid_to"] == 0


def test_get_temporal_current_value(store: MilvusStore):
    """get_temporal with at=None returns the currently open entry."""
    store.set_temporal("config", "v1", timestamp=1000)
    store.set_temporal("config", "v2", timestamp=2000)

    # Query at a time well after the last update
    current = store.get_temporal("config", at=9999999999)
    assert len(current) == 1
    assert current[0]["kv_value"] == '"v2"'


def test_get_temporal_as_of(store: MilvusStore):
    """Historical 'as-of' query returns the value valid at a past timestamp."""
    store.set_temporal("deploy_branch", "main", timestamp=1700000000)
    store.set_temporal("deploy_branch", "release", timestamp=1710000000)

    # As-of query during the first window
    result = store.get_temporal("deploy_branch", at=1705000000)
    assert len(result) == 1
    assert result[0]["kv_value"] == '"main"'

    # As-of query during the second window
    result = store.get_temporal("deploy_branch", at=1715000000)
    assert len(result) == 1
    assert result[0]["kv_value"] == '"release"'


def test_get_temporal_before_any_entry(store: MilvusStore):
    """Querying before any entry exists returns empty list."""
    store.set_temporal("key", "value", timestamp=1000)

    result = store.get_temporal("key", at=500)
    assert result == []


def test_get_temporal_nonexistent_key(store: MilvusStore):
    """Querying a key that doesn't exist returns empty list."""
    result = store.get_temporal("nonexistent", at=1700000000)
    assert result == []


def test_get_temporal_history_sorted(store: MilvusStore):
    """get_temporal_history returns entries sorted by valid_from."""
    # Insert in reverse order to verify sorting
    store.set_temporal("key", "third", timestamp=3000)
    # These are independent keys so they won't close each other — use same key
    # Actually set_temporal closes previous, so inserting in order:
    store.set_temporal("key", "fourth", timestamp=4000)

    history = store.get_temporal_history("key")
    assert len(history) == 2
    assert history[0]["valid_from"] <= history[1]["valid_from"]


def test_get_temporal_history_empty(store: MilvusStore):
    """get_temporal_history for nonexistent key returns empty list."""
    history = store.get_temporal_history("nonexistent")
    assert history == []


def test_temporal_namespace_isolation(store: MilvusStore):
    """Facts in different namespaces are independent."""
    store.set_temporal("test_cmd", "pytest", namespace="project-a", timestamp=1000)
    store.set_temporal("test_cmd", "cargo test", namespace="project-b", timestamp=1000)

    result_a = store.get_temporal("test_cmd", namespace="project-a", at=2000)
    assert len(result_a) == 1
    assert result_a[0]["kv_value"] == '"pytest"'

    result_b = store.get_temporal("test_cmd", namespace="project-b", at=2000)
    assert len(result_b) == 1
    assert result_b[0]["kv_value"] == '"cargo test"'

    # Updating one namespace doesn't affect the other
    store.set_temporal("test_cmd", "pytest -v", namespace="project-a", timestamp=2000)

    history_a = store.get_temporal_history("test_cmd", namespace="project-a")
    assert len(history_a) == 2  # old + new

    history_b = store.get_temporal_history("test_cmd", namespace="project-b")
    assert len(history_b) == 1  # unchanged


def test_temporal_no_namespace_isolated_from_namespaced(store: MilvusStore):
    """Empty namespace entries don't interfere with namespaced entries."""
    store.set_temporal("key", "global", timestamp=1000)
    store.set_temporal("key", "scoped", namespace="ns", timestamp=1000)

    global_result = store.get_temporal("key", at=2000)
    assert len(global_result) == 1
    assert global_result[0]["kv_value"] == '"global"'

    scoped_result = store.get_temporal("key", namespace="ns", at=2000)
    assert len(scoped_result) == 1
    assert scoped_result[0]["kv_value"] == '"scoped"'


def test_set_temporal_json_value_types(store: MilvusStore):
    """Various JSON value types are stored and retrieved correctly."""
    store.set_temporal("string_val", "hello", timestamp=1000)
    store.set_temporal("int_val", 42, timestamp=1000)
    store.set_temporal("dict_val", {"port": 8080, "host": "localhost"}, timestamp=1000)
    store.set_temporal("list_val", [1, 2, 3], timestamp=1000)
    store.set_temporal("bool_val", True, timestamp=1000)
    store.set_temporal("null_val", None, timestamp=1000)

    for key, expected in [
        ("string_val", '"hello"'),
        ("int_val", "42"),
        ("dict_val", json.dumps({"port": 8080, "host": "localhost"})),
        ("list_val", "[1, 2, 3]"),
        ("bool_val", "true"),
        ("null_val", "null"),
    ]:
        result = store.get_temporal(key, at=2000)
        assert len(result) == 1, f"Expected 1 result for {key}, got {len(result)}"
        assert result[0]["kv_value"] == expected, f"Value mismatch for {key}: {result[0]['kv_value']!r} != {expected!r}"


def test_set_temporal_tags(store: MilvusStore):
    """Tags are stored as JSON array and returned correctly."""
    store.set_temporal(
        "deploy_branch",
        "main",
        tags=["infra", "deployment"],
        timestamp=1000,
    )

    result = store.get_temporal("deploy_branch", at=2000)
    assert len(result) == 1
    assert json.loads(result[0]["tags"]) == ["infra", "deployment"]


def test_set_temporal_content_searchable(store: MilvusStore):
    """Content field is set correctly for BM25 discoverability."""
    store.set_temporal(
        "deploy_branch",
        "main",
        content="deploy branch changed to main for production release",
        timestamp=1000,
    )

    result = store.get_temporal("deploy_branch", at=2000)
    assert len(result) == 1
    assert "deploy branch" in result[0]["content"]


def test_set_temporal_returns_entry_without_embedding(store: MilvusStore):
    """set_temporal return value matches query output (no embedding)."""
    entry = store.set_temporal("key", "val", timestamp=1000)

    # All expected fields present
    assert "chunk_hash" in entry
    assert "entry_type" in entry
    assert "kv_key" in entry
    assert "kv_value" in entry
    assert "valid_from" in entry
    assert "valid_to" in entry
    assert "updated_at" in entry

    # Embedding excluded
    assert "embedding" not in entry


# ---- delete_temporal tests ---------------------------------------------------


def test_delete_temporal_closes_open_entry(store: MilvusStore):
    """delete_temporal closes the current entry without creating a new one."""
    store.set_temporal("branch", "main", timestamp=1000)

    closed = store.delete_temporal("branch", timestamp=2000)
    assert len(closed) == 1
    assert closed[0]["kv_key"] == "branch"
    assert closed[0]["kv_value"] == '"main"'
    assert closed[0]["valid_to"] == 2000
    assert "embedding" not in closed[0]

    # No current value anymore
    current = store.get_temporal("branch", at=3000)
    assert current == []

    # History still preserved
    history = store.get_temporal_history("branch")
    assert len(history) == 1
    assert history[0]["valid_to"] == 2000


def test_delete_temporal_nonexistent_key(store: MilvusStore):
    """delete_temporal on a nonexistent key returns empty list."""
    closed = store.delete_temporal("nonexistent", timestamp=1000)
    assert closed == []


def test_delete_temporal_already_closed(store: MilvusStore):
    """delete_temporal on an already-closed (superseded) key returns empty."""
    store.set_temporal("key", "v1", timestamp=1000)
    store.set_temporal("key", "v2", timestamp=2000)

    # Delete current (v2)
    closed = store.delete_temporal("key", timestamp=3000)
    assert len(closed) == 1
    assert closed[0]["kv_value"] == '"v2"'

    # Deleting again returns empty — no open entries left
    closed2 = store.delete_temporal("key", timestamp=4000)
    assert closed2 == []


def test_delete_temporal_preserves_full_history(store: MilvusStore):
    """After delete, full history chain remains queryable."""
    store.set_temporal("branch", "main", timestamp=1000)
    store.set_temporal("branch", "develop", timestamp=2000)
    store.set_temporal("branch", "release", timestamp=3000)
    store.delete_temporal("branch", timestamp=4000)

    history = store.get_temporal_history("branch")
    assert len(history) == 3

    # Chain: main [1000,2000) → develop [2000,3000) → release [3000,4000)
    assert history[0]["kv_value"] == '"main"'
    assert history[0]["valid_from"] == 1000
    assert history[0]["valid_to"] == 2000

    assert history[1]["kv_value"] == '"develop"'
    assert history[1]["valid_from"] == 2000
    assert history[1]["valid_to"] == 3000

    assert history[2]["kv_value"] == '"release"'
    assert history[2]["valid_from"] == 3000
    assert history[2]["valid_to"] == 4000

    # As-of queries still work for the past
    result = store.get_temporal("branch", at=2500)
    assert len(result) == 1
    assert result[0]["kv_value"] == '"develop"'

    # But nothing is current
    result = store.get_temporal("branch", at=5000)
    assert result == []


def test_delete_temporal_namespace_isolation(store: MilvusStore):
    """delete_temporal only affects the specified namespace."""
    store.set_temporal("key", "a", namespace="ns-a", timestamp=1000)
    store.set_temporal("key", "b", namespace="ns-b", timestamp=1000)

    store.delete_temporal("key", namespace="ns-a", timestamp=2000)

    # ns-a is deleted
    assert store.get_temporal("key", namespace="ns-a", at=3000) == []

    # ns-b still active
    result = store.get_temporal("key", namespace="ns-b", at=3000)
    assert len(result) == 1
    assert result[0]["kv_value"] == '"b"'


def test_recreate_after_delete(store: MilvusStore):
    """A fact can be re-created after deletion, extending the history chain."""
    store.set_temporal("branch", "main", timestamp=1000)
    store.delete_temporal("branch", timestamp=2000)

    # Re-create — this should not find any open entries to close
    store.set_temporal("branch", "develop", timestamp=3000)

    history = store.get_temporal_history("branch")
    assert len(history) == 2

    assert history[0]["kv_value"] == '"main"'
    assert history[0]["valid_from"] == 1000
    assert history[0]["valid_to"] == 2000

    assert history[1]["kv_value"] == '"develop"'
    assert history[1]["valid_from"] == 3000
    assert history[1]["valid_to"] == 0

    # Current value is the re-created one
    current = store.get_temporal("branch", at=4000)
    assert len(current) == 1
    assert current[0]["kv_value"] == '"develop"'


# ---- list_temporal_keys tests ------------------------------------------------


def test_list_temporal_keys_basic(store: MilvusStore):
    """list_temporal_keys returns all unique keys in a namespace."""
    store.set_temporal("branch", "main", timestamp=1000)
    store.set_temporal("test_cmd", "pytest", timestamp=1000)
    store.set_temporal("deploy_target", "prod", timestamp=1000)

    keys = store.list_temporal_keys()
    assert keys == ["branch", "deploy_target", "test_cmd"]  # sorted


def test_list_temporal_keys_namespace_filter(store: MilvusStore):
    """list_temporal_keys only returns keys from the specified namespace."""
    store.set_temporal("key1", "v1", namespace="ns-a", timestamp=1000)
    store.set_temporal("key2", "v2", namespace="ns-b", timestamp=1000)
    store.set_temporal("key3", "v3", timestamp=1000)  # default namespace

    assert store.list_temporal_keys(namespace="ns-a") == ["key1"]
    assert store.list_temporal_keys(namespace="ns-b") == ["key2"]
    assert store.list_temporal_keys(namespace="") == ["key3"]


def test_list_temporal_keys_current_only(store: MilvusStore):
    """current_only=True excludes keys with only closed entries."""
    store.set_temporal("active", "yes", timestamp=1000)
    store.set_temporal("expired", "was-here", timestamp=1000)
    store.delete_temporal("expired", timestamp=2000)

    all_keys = store.list_temporal_keys()
    assert all_keys == ["active", "expired"]  # both appear

    current_keys = store.list_temporal_keys(current_only=True)
    assert current_keys == ["active"]  # only the open one


def test_list_temporal_keys_empty(store: MilvusStore):
    """list_temporal_keys returns empty list when no temporal facts exist."""
    keys = store.list_temporal_keys()
    assert keys == []


def test_list_temporal_keys_includes_superseded(store: MilvusStore):
    """Keys with updated (superseded) values still have open entries."""
    store.set_temporal("key", "v1", timestamp=1000)
    store.set_temporal("key", "v2", timestamp=2000)

    keys = store.list_temporal_keys(current_only=True)
    assert keys == ["key"]  # still current (v2 is open)


# ---- Hash collision resilience -----------------------------------------------


def test_same_second_updates_preserve_history(store: MilvusStore):
    """Two updates at the same timestamp produce distinct entries."""
    store.set_temporal("key", "first", timestamp=5000)
    store.set_temporal("key", "second", timestamp=5000)

    history = store.get_temporal_history("key")
    assert len(history) == 2

    # First entry should be closed
    values = [h["kv_value"] for h in history]
    assert '"first"' in values
    assert '"second"' in values

    # One should be closed, one open
    closed = [h for h in history if h["valid_to"] != 0]
    open_entries = [h for h in history if h["valid_to"] == 0]
    assert len(closed) == 1
    assert len(open_entries) == 1
    assert open_entries[0]["kv_value"] == '"second"'


# ---- Summary + Original pattern (spec §9) -----------------------------------


def test_search_excludes_original_by_default(store: MilvusStore):
    """Default search returns summary (content) but not original."""
    store.upsert(
        [
            {
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "content": "Summary of the document",
                "original": "This is the full original text of the document with all the details",
                "source": "test.md",
                "heading": "Test",
                "chunk_hash": "orig1",
                "heading_level": 1,
                "start_line": 1,
                "end_line": 10,
            }
        ]
    )
    results = store.search([1.0, 0.0, 0.0, 0.0], top_k=1)
    assert len(results) >= 1
    assert results[0]["content"] == "Summary of the document"
    assert "original" not in results[0]


def test_search_includes_original_when_full(store: MilvusStore):
    """search(full=True) returns both summary and original."""
    store.upsert(
        [
            {
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "content": "Summary of the document",
                "original": "This is the full original text of the document with all the details",
                "source": "test.md",
                "heading": "Test",
                "chunk_hash": "orig2",
                "heading_level": 1,
                "start_line": 1,
                "end_line": 10,
            }
        ]
    )
    results = store.search([1.0, 0.0, 0.0, 0.0], top_k=1, full=True)
    assert len(results) >= 1
    assert results[0]["content"] == "Summary of the document"
    assert results[0]["original"] == ("This is the full original text of the document with all the details")


def test_get_returns_full_entry_with_original(store: MilvusStore):
    """get() always returns original content (full retrieval path)."""
    store.upsert(
        [
            {
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "content": "Summary text",
                "original": "Full original text with all the verbose details",
                "source": "test.md",
                "heading": "Test",
                "chunk_hash": "get_test_1",
                "heading_level": 1,
                "start_line": 1,
                "end_line": 5,
            }
        ]
    )
    entry = store.get("get_test_1")
    assert entry is not None
    assert entry["content"] == "Summary text"
    assert entry["original"] == "Full original text with all the verbose details"
    assert entry["chunk_hash"] == "get_test_1"
    assert entry["source"] == "test.md"


def test_get_returns_none_for_missing_hash(store: MilvusStore):
    """get() returns None when chunk_hash doesn't exist."""
    result = store.get("nonexistent_hash")
    assert result is None


def test_query_excludes_original_by_default(store: MilvusStore):
    """Default query() returns summary but not original."""
    store.upsert(
        [
            {
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "content": "Query summary",
                "original": "Query original full text",
                "source": "test.md",
                "heading": "",
                "chunk_hash": "query_test_1",
                "heading_level": 0,
                "start_line": 1,
                "end_line": 1,
            }
        ]
    )
    results = store.query()
    assert len(results) >= 1
    match = [r for r in results if r["chunk_hash"] == "query_test_1"]
    assert len(match) == 1
    assert match[0]["content"] == "Query summary"
    assert "original" not in match[0]


def test_query_includes_original_when_full(store: MilvusStore):
    """query(full=True) returns both summary and original."""
    store.upsert(
        [
            {
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "content": "Query summary",
                "original": "Query original full text",
                "source": "test.md",
                "heading": "",
                "chunk_hash": "query_test_2",
                "heading_level": 0,
                "start_line": 1,
                "end_line": 1,
            }
        ]
    )
    results = store.query(full=True)
    assert len(results) >= 1
    match = [r for r in results if r["chunk_hash"] == "query_test_2"]
    assert len(match) == 1
    assert match[0]["content"] == "Query summary"
    assert match[0]["original"] == "Query original full text"


# ---- KV API tests (spec §6 — scalar-only insert/query) ------------------


def test_set_kv_basic(store: MilvusStore):
    """set_kv stores a KV entry and returns it without embedding."""
    result = store.set_kv("test_command", "pytest tests/ -v", namespace="project")
    assert result["entry_type"] == "kv"
    assert result["kv_key"] == "test_command"
    assert result["kv_namespace"] == "project"
    assert result["kv_value"] == '"pytest tests/ -v"'
    assert "embedding" not in result
    assert result["updated_at"] > 0


def test_set_kv_json_value_types(store: MilvusStore):
    """set_kv JSON-encodes various value types correctly."""
    # String
    store.set_kv("str_key", "hello", namespace="types")
    r = store.get_kv("str_key", namespace="types")
    assert r is not None
    assert json.loads(r["kv_value"]) == "hello"

    # Integer
    store.set_kv("int_key", 42, namespace="types")
    r = store.get_kv("int_key", namespace="types")
    assert r is not None
    assert json.loads(r["kv_value"]) == 42

    # Dict
    store.set_kv("dict_key", {"a": 1, "b": [2, 3]}, namespace="types")
    r = store.get_kv("dict_key", namespace="types")
    assert r is not None
    assert json.loads(r["kv_value"]) == {"a": 1, "b": [2, 3]}

    # List
    store.set_kv("list_key", [1, "two", 3.0], namespace="types")
    r = store.get_kv("list_key", namespace="types")
    assert r is not None
    assert json.loads(r["kv_value"]) == [1, "two", 3.0]

    # Bool
    store.set_kv("bool_key", True, namespace="types")
    r = store.get_kv("bool_key", namespace="types")
    assert r is not None
    assert json.loads(r["kv_value"]) is True

    # None / null
    store.set_kv("null_key", None, namespace="types")
    r = store.get_kv("null_key", namespace="types")
    assert r is not None
    assert json.loads(r["kv_value"]) is None


def test_set_kv_upsert_overwrites(store: MilvusStore):
    """Calling set_kv with the same key/namespace overwrites the value."""
    store.set_kv("deploy_branch", "main", namespace="project")
    r1 = store.get_kv("deploy_branch", namespace="project")
    assert r1 is not None
    assert json.loads(r1["kv_value"]) == "main"

    store.set_kv("deploy_branch", "release", namespace="project")
    r2 = store.get_kv("deploy_branch", namespace="project")
    assert r2 is not None
    assert json.loads(r2["kv_value"]) == "release"

    # Only one entry should exist (upsert, not insert-new)
    all_kv = store.list_kv(namespace="project")
    deploy_entries = [e for e in all_kv if e["kv_key"] == "deploy_branch"]
    assert len(deploy_entries) == 1


def test_set_kv_with_metadata(store: MilvusStore):
    """set_kv stores source, content, and tags correctly."""
    result = store.set_kv(
        "test_command",
        "pytest tests/ -v",
        namespace="project",
        source="facts.md",
        content="The project test command",
        tags=["testing", "ci"],
    )
    assert result["source"] == "facts.md"
    assert result["content"] == "The project test command"
    assert json.loads(result["tags"]) == ["testing", "ci"]


def test_get_kv_basic(store: MilvusStore):
    """get_kv retrieves a stored KV entry."""
    store.set_kv("test_command", "pytest tests/ -v", namespace="project")
    result = store.get_kv("test_command", namespace="project")
    assert result is not None
    assert result["kv_key"] == "test_command"
    assert result["kv_namespace"] == "project"
    assert json.loads(result["kv_value"]) == "pytest tests/ -v"


def test_get_kv_nonexistent(store: MilvusStore):
    """get_kv returns None for a key that doesn't exist."""
    result = store.get_kv("nonexistent", namespace="project")
    assert result is None


def test_get_kv_wrong_namespace(store: MilvusStore):
    """get_kv returns None when namespace doesn't match."""
    store.set_kv("test_command", "pytest", namespace="project")
    result = store.get_kv("test_command", namespace="other")
    assert result is None


def test_get_kv_includes_original(store: MilvusStore):
    """get_kv returns full fields including original (via store.get)."""
    store.set_kv("key1", "value1", namespace="ns")
    result = store.get_kv("key1", namespace="ns")
    assert result is not None
    # get_kv uses store.get() which returns _FULL_FIELDS including original
    assert "original" in result


def test_list_kv_basic(store: MilvusStore):
    """list_kv returns all KV entries in a namespace sorted by key."""
    store.set_kv("beta", "b_val", namespace="ns1")
    store.set_kv("alpha", "a_val", namespace="ns1")
    store.set_kv("gamma", "g_val", namespace="ns1")

    results = store.list_kv(namespace="ns1")
    assert len(results) == 3
    keys = [r["kv_key"] for r in results]
    assert keys == ["alpha", "beta", "gamma"]


def test_list_kv_namespace_isolation(store: MilvusStore):
    """list_kv only returns entries in the requested namespace."""
    store.set_kv("key1", "val1", namespace="ns_a")
    store.set_kv("key2", "val2", namespace="ns_b")
    store.set_kv("key3", "val3", namespace="ns_a")

    results_a = store.list_kv(namespace="ns_a")
    assert len(results_a) == 2
    assert {r["kv_key"] for r in results_a} == {"key1", "key3"}

    results_b = store.list_kv(namespace="ns_b")
    assert len(results_b) == 1
    assert results_b[0]["kv_key"] == "key2"


def test_list_kv_empty_namespace(store: MilvusStore):
    """list_kv with empty namespace returns unnamespaced entries."""
    store.set_kv("bare_key", "bare_val")
    store.set_kv("ns_key", "ns_val", namespace="named")

    results = store.list_kv(namespace="")
    keys = {r["kv_key"] for r in results}
    assert "bare_key" in keys
    assert "ns_key" not in keys


def test_list_kv_empty(store: MilvusStore):
    """list_kv returns empty list when no entries exist."""
    results = store.list_kv(namespace="empty")
    assert results == []


def test_delete_kv_basic(store: MilvusStore):
    """delete_kv removes a KV entry and returns True."""
    store.set_kv("to_delete", "some_value", namespace="project")
    assert store.get_kv("to_delete", namespace="project") is not None

    deleted = store.delete_kv("to_delete", namespace="project")
    assert deleted is True

    assert store.get_kv("to_delete", namespace="project") is None


def test_delete_kv_nonexistent(store: MilvusStore):
    """delete_kv returns False for a key that doesn't exist."""
    deleted = store.delete_kv("nope", namespace="project")
    assert deleted is False


def test_delete_kv_namespace_isolation(store: MilvusStore):
    """delete_kv only deletes in the specified namespace."""
    store.set_kv("shared_key", "val_a", namespace="ns_a")
    store.set_kv("shared_key", "val_b", namespace="ns_b")

    deleted = store.delete_kv("shared_key", namespace="ns_a")
    assert deleted is True

    # ns_b should still have its entry
    assert store.get_kv("shared_key", namespace="ns_b") is not None
    assert store.get_kv("shared_key", namespace="ns_a") is None


def test_list_kv_keys_basic(store: MilvusStore):
    """list_kv_keys returns sorted unique keys."""
    store.set_kv("beta", "b", namespace="ns")
    store.set_kv("alpha", "a", namespace="ns")
    store.set_kv("gamma", "g", namespace="ns")

    keys = store.list_kv_keys(namespace="ns")
    assert keys == ["alpha", "beta", "gamma"]


def test_list_kv_keys_namespace_filter(store: MilvusStore):
    """list_kv_keys only returns keys from the specified namespace."""
    store.set_kv("k1", "v1", namespace="a")
    store.set_kv("k2", "v2", namespace="b")

    assert store.list_kv_keys(namespace="a") == ["k1"]
    assert store.list_kv_keys(namespace="b") == ["k2"]


def test_list_kv_keys_empty(store: MilvusStore):
    """list_kv_keys returns empty list when no entries exist."""
    keys = store.list_kv_keys(namespace="empty")
    assert keys == []


def test_kv_does_not_interfere_with_temporal(store: MilvusStore):
    """KV entries and temporal entries are independent."""
    store.set_kv("deploy_branch", "main", namespace="project")
    store.set_temporal("deploy_branch", "release", namespace="project", timestamp=1000)

    # KV lookup returns the KV entry
    kv_result = store.get_kv("deploy_branch", namespace="project")
    assert kv_result is not None
    assert json.loads(kv_result["kv_value"]) == "main"
    assert kv_result["entry_type"] == "kv"

    # Temporal lookup returns the temporal entry
    temporal_results = store.get_temporal("deploy_branch", namespace="project", at=1000)
    assert len(temporal_results) >= 1
    assert temporal_results[0]["entry_type"] == "temporal"
    assert json.loads(temporal_results[0]["kv_value"]) == "release"


def test_kv_does_not_interfere_with_documents(store: MilvusStore):
    """KV entries don't pollute document queries and vice versa."""
    store.set_kv("test_command", "pytest", namespace="project")
    store.upsert(
        [
            {
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "content": "Document about testing",
                "source": "test.md",
                "heading": "Testing",
                "chunk_hash": "doc_hash_1",
                "heading_level": 1,
                "start_line": 1,
                "end_line": 5,
            }
        ]
    )

    # list_kv should not include documents
    kv_results = store.list_kv(namespace="project")
    assert all(r["entry_type"] == "kv" for r in kv_results)

    # Document search should not include KV entries
    doc_results = store.query(filter_expr='entry_type == "document"')
    assert all(r["entry_type"] == "document" for r in doc_results)


def test_set_kv_deterministic_hash(store: MilvusStore):
    """set_kv generates a deterministic hash from namespace:key."""
    import hashlib

    r1 = store.set_kv("mykey", "val1", namespace="myns")
    expected_hash = hashlib.sha256(b"kv:myns:mykey").hexdigest()[:32]
    assert r1["chunk_hash"] == expected_hash

    # Second set with same key/ns should have the same hash
    r2 = store.set_kv("mykey", "val2", namespace="myns")
    assert r2["chunk_hash"] == expected_hash

    # Different key should have a different hash
    r3 = store.set_kv("otherkey", "val3", namespace="myns")
    assert r3["chunk_hash"] != expected_hash


# ---- Roadmap 2.1.15 — KV insert/query round-trip tests (a)-(g) -----------
#
# The following tests explicitly cover every roadmap case. Cases (a), (c), (d),
# and (f) are already exercised by the tests above; these complement the suite
# with the remaining gaps: (b) multi-key independent query, (e) scalar-only
# verification via mock, and (g) complex string values.


def test_kv_roundtrip_multiple_keys_query_independently(store: MilvusStore):
    """Roadmap 2.1.15(b): Insert multiple KV pairs with different keys and
    query each independently via get_kv."""
    pairs = {
        "db_host": "localhost",
        "db_port": 5432,
        "db_name": "myapp",
        "log_level": "debug",
        "feature_flags": {"dark_mode": True, "beta": False},
    }
    for key, value in pairs.items():
        store.set_kv(key, value, namespace="config")

    # Query each independently and verify round-trip fidelity
    for key, expected_value in pairs.items():
        result = store.get_kv(key, namespace="config")
        assert result is not None, f"get_kv returned None for key={key!r}"
        assert result["kv_key"] == key
        assert json.loads(result["kv_value"]) == expected_value

    # Verify all keys exist
    all_keys = store.list_kv_keys(namespace="config")
    assert sorted(all_keys) == sorted(pairs.keys())


def test_kv_scalar_only_no_vector_search(store: MilvusStore):
    """Roadmap 2.1.15(e): KV operations use scalar-only path — no vector
    search (hybrid_search) is invoked. Verified via mock."""
    # Insert a KV pair first
    store.set_kv("colour", "blue", namespace="prefs")

    # Patch the Milvus client's hybrid_search and the store's search method to
    # detect if vector search is ever called during KV operations.
    with (
        patch.object(store._client, "hybrid_search", wraps=store._client.hybrid_search)
            as mock_hybrid,
        patch.object(store, "search", wraps=store.search) as mock_search,
    ):
        # get_kv — scalar primary-key lookup
        result = store.get_kv("colour", namespace="prefs")
        assert result is not None
        assert json.loads(result["kv_value"]) == "blue"

        # list_kv — scalar filter query
        entries = store.list_kv(namespace="prefs")
        assert len(entries) == 1

        # list_kv_keys — scalar filter query
        keys = store.list_kv_keys(namespace="prefs")
        assert keys == ["colour"]

        # delete_kv — scalar lookup + delete
        store.set_kv("temp", "discard", namespace="prefs")
        store.delete_kv("temp", namespace="prefs")

        # None of the above should have triggered vector search
        mock_hybrid.assert_not_called()
        mock_search.assert_not_called()


def test_kv_complex_string_values(store: MilvusStore):
    """Roadmap 2.1.15(g): KV values can store complex strings — multi-line,
    unicode, and special characters survive the round-trip."""

    # Multi-line string with indentation
    multiline = "line one\nline two\n  indented line\n\ttab-indented\n"
    store.set_kv("multiline", multiline, namespace="strings")
    r = store.get_kv("multiline", namespace="strings")
    assert r is not None
    assert json.loads(r["kv_value"]) == multiline

    # Unicode: CJK, emoji, combining characters, RTL
    unicode_str = "日本語テスト 🚀🎉 café résumé naïve Ω≈ç√∫ مرحبا"
    store.set_kv("unicode", unicode_str, namespace="strings")
    r = store.get_kv("unicode", namespace="strings")
    assert r is not None
    assert json.loads(r["kv_value"]) == unicode_str

    # Special / control characters: quotes, backslashes, null-like
    special = 'he said "hello" and she said \'hi\' \\ /path/to/file \t\nnewline'
    store.set_kv("special", special, namespace="strings")
    r = store.get_kv("special", namespace="strings")
    assert r is not None
    assert json.loads(r["kv_value"]) == special

    # Empty string
    store.set_kv("empty", "", namespace="strings")
    r = store.get_kv("empty", namespace="strings")
    assert r is not None
    assert json.loads(r["kv_value"]) == ""

    # Very long string (1 KB)
    long_str = "x" * 1024
    store.set_kv("long", long_str, namespace="strings")
    r = store.get_kv("long", namespace="strings")
    assert r is not None
    assert json.loads(r["kv_value"]) == long_str

    # JSON-like string (must survive double-encoding)
    json_like = '{"nested": "json", "count": 42}'
    store.set_kv("json_like", json_like, namespace="strings")
    r = store.get_kv("json_like", namespace="strings")
    assert r is not None
    assert json.loads(r["kv_value"]) == json_like
