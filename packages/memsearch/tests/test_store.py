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
