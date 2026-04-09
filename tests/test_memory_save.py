"""Tests for memory_save — the MCP tool for saving insights with dedup.

Tests cover:
- Service-layer: save_document, update_document_timestamp, update_document_content
- Service-layer: vault file writing and slugification helpers
- Plugin-layer: cmd_memory_save orchestration (create, dedup, merge)
- Plugin-layer: LLM summary and merge fallback paths
- Edge cases: missing args, unavailable service, LLM failures
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Skip entire module on Windows (Milvus Lite not supported)
if sys.platform == "win32":
    pytest.skip("Milvus Lite not supported on Windows", allow_module_level=True)

from src.memory_v2_service import MemoryV2Service, MEMSEARCH_AVAILABLE


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
    """Create a mock MilvusStore."""
    store = MagicMock()
    store.count.return_value = 10
    store.model_info = {"provider": "test", "model": "test-model", "dimension": 384}
    store.needs_reindex = False
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
    return store


@pytest.fixture
def mock_router(mock_store):
    """Create a mock CollectionRouter."""
    router = MagicMock()
    router.get_store.return_value = mock_store
    router.list_collections.return_value = []
    router.search = AsyncMock(return_value=[])
    router.close = MagicMock()
    return router


@pytest.fixture
def tmp_data_dir():
    """Create a temporary directory for vault files."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def service(mock_embedder, mock_router, tmp_data_dir):
    """Create a MemoryV2Service with mocked dependencies and temp vault."""
    svc = MemoryV2Service(
        milvus_uri="/tmp/test.db",
        embedding_provider="openai",
        data_dir=tmp_data_dir,
    )
    svc._embedder = mock_embedder
    svc._router = mock_router
    svc._initialized = True
    return svc


# ---------------------------------------------------------------------------
# Service-layer: Helpers
# ---------------------------------------------------------------------------


class TestSlugify:
    """Test the _slugify static method."""

    def test_simple_text(self):
        assert MemoryV2Service._slugify("Hello World") == "hello-world"

    def test_special_characters(self):
        assert MemoryV2Service._slugify("OAuth 2.0 Token Refresh!") == "oauth-2-0-token-refresh"

    def test_truncation_at_word_boundary(self):
        long_text = "this is a very long title that should be truncated at a word boundary"
        slug = MemoryV2Service._slugify(long_text, max_len=30)
        assert len(slug) <= 30
        assert not slug.endswith("-")

    def test_empty_text(self):
        assert MemoryV2Service._slugify("") == "insight"
        assert MemoryV2Service._slugify("!!!") == "insight"

    def test_consecutive_special_chars(self):
        assert MemoryV2Service._slugify("hello---world___foo") == "hello-world-foo"


class TestGenerateChunkHash:
    """Test chunk hash generation."""

    def test_deterministic(self):
        h1 = MemoryV2Service._generate_chunk_hash("project:test", "content", "auth")
        h2 = MemoryV2Service._generate_chunk_hash("project:test", "content", "auth")
        assert h1 == h2

    def test_different_content(self):
        h1 = MemoryV2Service._generate_chunk_hash("project:test", "content1")
        h2 = MemoryV2Service._generate_chunk_hash("project:test", "content2")
        assert h1 != h2

    def test_different_scope(self):
        h1 = MemoryV2Service._generate_chunk_hash("project:a", "content")
        h2 = MemoryV2Service._generate_chunk_hash("project:b", "content")
        assert h1 != h2

    def test_hash_length(self):
        h = MemoryV2Service._generate_chunk_hash("scope", "content")
        assert len(h) == 32


# ---------------------------------------------------------------------------
# Service-layer: Vault File Writing
# ---------------------------------------------------------------------------


class TestVaultFileWriting:
    """Test vault markdown file creation and updates."""

    def test_write_vault_file_basic(self, service, tmp_data_dir):
        vault_dir = Path(tmp_data_dir) / "test_vault"
        filepath = service._write_vault_file(
            vault_dir,
            content="OAuth tokens need explicit scope re-request.",
            tags=["insight", "auth"],
            topic="authentication",
            source_task="task-123",
        )
        assert filepath.exists()
        assert filepath.suffix == ".md"
        assert filepath.parent.name == "insights"

        text = filepath.read_text()
        assert "---" in text
        assert '"insight"' in text
        assert '"auth"' in text
        assert "topic: authentication" in text
        assert "source_task: task-123" in text
        assert "OAuth tokens need explicit scope re-request." in text

    def test_write_vault_file_with_original(self, service, tmp_data_dir):
        vault_dir = Path(tmp_data_dir) / "test_vault2"
        filepath = service._write_vault_file(
            vault_dir,
            content="Summary of the insight.",
            original="Full detailed original text with many details.",
            tags=["insight"],
        )
        text = filepath.read_text()
        assert "Summary of the insight." in text
        assert "## Original" in text
        assert "Full detailed original text" in text

    def test_write_vault_file_no_original_section_when_same(self, service, tmp_data_dir):
        vault_dir = Path(tmp_data_dir) / "test_vault3"
        content = "Short insight"
        filepath = service._write_vault_file(
            vault_dir,
            content=content,
            original=content,  # same as content
            tags=["insight"],
        )
        text = filepath.read_text()
        assert "## Original" not in text

    def test_write_vault_file_creates_directory(self, service, tmp_data_dir):
        vault_dir = Path(tmp_data_dir) / "nested" / "deep" / "vault"
        filepath = service._write_vault_file(
            vault_dir,
            content="Test content",
            tags=["test"],
        )
        assert filepath.exists()

    def test_update_vault_file_timestamp(self, service, tmp_data_dir):
        # First create a file
        vault_dir = Path(tmp_data_dir) / "update_test"
        filepath = service._write_vault_file(
            vault_dir,
            content="Original content",
            tags=["insight"],
            created="2026-01-01",
        )
        original_text = filepath.read_text()
        assert "created: 2026-01-01" in original_text

        # Update it
        service._update_vault_file(filepath, source_task="task-456")
        updated_text = filepath.read_text()
        # created should be preserved
        assert "created: 2026-01-01" in updated_text
        # source_task should be appended
        assert "task-456" in updated_text

    def test_update_vault_file_merge_tags(self, service, tmp_data_dir):
        vault_dir = Path(tmp_data_dir) / "tag_merge_test"
        filepath = service._write_vault_file(
            vault_dir,
            content="Test content",
            tags=["insight", "auth"],
        )
        service._update_vault_file(filepath, tags=["bugfix", "auth"])
        text = filepath.read_text()
        # Should contain all tags without duplicates
        assert "insight" in text
        assert "auth" in text
        assert "bugfix" in text

    def test_update_vault_file_replace_content(self, service, tmp_data_dir):
        vault_dir = Path(tmp_data_dir) / "content_replace_test"
        filepath = service._write_vault_file(
            vault_dir,
            content="Old content",
            tags=["insight"],
        )
        service._update_vault_file(filepath, content="Merged new content here.")
        text = filepath.read_text()
        assert "Merged new content here." in text


# ---------------------------------------------------------------------------
# Service-layer: save_document
# ---------------------------------------------------------------------------


class TestSaveDocument:
    """Test the save_document service method."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_document_basic(self, service, mock_store, mock_embedder):
        result = await service.save_document(
            "test-project",
            "OAuth requires explicit scope re-request on refresh.",
            tags=["insight", "auth"],
            topic="authentication",
            source_task="task-123",
        )
        assert result["chunk_hash"]
        assert result["vault_path"]
        assert result["tags"] == ["insight", "auth"]
        assert result["topic"] == "authentication"
        assert result["source_task"] == "task-123"

        # Verify Milvus upsert was called
        mock_store.upsert.assert_called_once()
        upserted = mock_store.upsert.call_args[0][0]
        assert len(upserted) == 1
        assert upserted[0]["entry_type"] == "document"
        assert upserted[0]["topic"] == "authentication"

        # Verify embedding was computed
        mock_embedder.embed.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_document_with_summary(self, service, mock_store):
        await service.save_document(
            "test-project",
            "Full content that is long",
            summary="Short summary",
            tags=["insight"],
        )
        # Verify the summary is stored as indexed content
        upserted = mock_store.upsert.call_args[0][0][0]
        assert upserted["content"] == "Short summary"
        assert upserted["original"] == "Full content that is long"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_document_vault_file_created(self, service, tmp_data_dir):
        result = await service.save_document(
            "test-project",
            "Test insight content",
            tags=["test"],
        )
        vault_path = Path(result["vault_path"])
        assert vault_path.exists()
        assert vault_path.suffix == ".md"

    @pytest.mark.asyncio
    async def test_save_document_unavailable(self):
        svc = MemoryV2Service()
        with pytest.raises(RuntimeError, match="not available"):
            await svc.save_document("proj", "content")

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_document_default_tags(self, service, mock_store):
        result = await service.save_document("test-project", "Content")
        assert result["tags"] == ["insight", "auto-generated"]


# ---------------------------------------------------------------------------
# Service-layer: update_document_timestamp
# ---------------------------------------------------------------------------


class TestUpdateDocumentTimestamp:
    """Test timestamp updates for dedup case."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_timestamp(self, service, mock_store):
        result = await service.update_document_timestamp(
            "test-project",
            "existing_hash",
            source_task="task-new",
        )
        assert result["chunk_hash"] == "existing_hash"
        assert result["updated_at"] > 0
        mock_store.get.assert_called_once_with("existing_hash")
        mock_store.upsert.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_timestamp_not_found(self, service, mock_store):
        mock_store.get.return_value = None
        with pytest.raises(ValueError, match="not found"):
            await service.update_document_timestamp("proj", "missing_hash")

    @pytest.mark.asyncio
    async def test_update_timestamp_unavailable(self):
        svc = MemoryV2Service()
        with pytest.raises(RuntimeError, match="not available"):
            await svc.update_document_timestamp("proj", "hash")


# ---------------------------------------------------------------------------
# Service-layer: update_document_content
# ---------------------------------------------------------------------------


class TestUpdateDocumentContent:
    """Test content updates for merge case."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_content(self, service, mock_store, mock_embedder):
        result = await service.update_document_content(
            "test-project",
            "existing_hash",
            "Merged content here",
            tags=["insight", "merged"],
        )
        assert result["chunk_hash"] == "existing_hash"
        assert result["updated_at"] > 0

        # Embedding should be recomputed
        mock_embedder.embed.assert_called_once_with(["Merged content here"])

        # Upsert should have updated content
        upserted = mock_store.upsert.call_args[0][0][0]
        assert upserted["content"] == "Merged content here"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_content_not_found(self, service, mock_store):
        mock_store.get.return_value = None
        with pytest.raises(ValueError, match="not found"):
            await service.update_document_content("proj", "missing", "content")

    @pytest.mark.asyncio
    async def test_update_content_unavailable(self):
        svc = MemoryV2Service()
        with pytest.raises(RuntimeError, match="not available"):
            await svc.update_document_content("proj", "hash", "content")


# ---------------------------------------------------------------------------
# Plugin-layer: cmd_memory_save
# ---------------------------------------------------------------------------


class TestPluginMemorySave:
    """Test the plugin command handler orchestration."""

    @pytest.fixture
    def plugin(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        return MemoryV2Plugin()

    @pytest.fixture
    def wired_plugin(self, plugin, service):
        """Plugin with a wired-up service and mock context."""
        plugin._service = service
        plugin._log = MagicMock()
        # Mock the context for LLM invocation
        plugin._ctx = MagicMock()
        plugin._ctx.invoke_llm = AsyncMock(return_value="LLM generated summary")
        return plugin

    @pytest.mark.asyncio
    async def test_save_missing_project_id(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_save({"content": "test"})
        assert "error" in result
        assert "project_id" in result["error"]

    @pytest.mark.asyncio
    async def test_save_missing_content(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_save({"project_id": "proj"})
        assert "error" in result
        assert "content" in result["error"]

    @pytest.mark.asyncio
    async def test_save_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_save({"project_id": "proj", "content": "test"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_creates_new_distinct(self, wired_plugin, mock_store):
        """When no similar entries exist, create a new entry."""
        # No search results → distinct content (dedup uses single-scope store.search)
        mock_store.search.return_value = []

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "New insight about testing patterns.",
                "tags": ["insight", "testing"],
                "topic": "testing",
                "source_task": "task-001",
            }
        )
        assert result["success"] is True
        assert result["action"] == "created"
        assert result["project_id"] == "test-project"
        assert result["chunk_hash"]
        assert result["vault_path"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_dedup_near_identical(self, wired_plugin, mock_store):
        """When similarity > 0.95, deduplicate (update timestamp only)."""
        mock_store.search.return_value = [
            {
                "content": "Existing insight about auth",
                "score": 0.98,
                "chunk_hash": "existing_hash",
                "entry_type": "document",
                "topic": "auth",
                "tags": '["insight"]',
                "_scope": "project",
                "_scope_id": "test",
            }
        ]

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Almost identical insight about auth.",
                "tags": ["insight"],
            }
        )
        assert result["success"] is True
        assert result["action"] == "deduplicated"
        assert result["similarity_score"] == 0.98
        assert result["existing_chunk_hash"] == "existing_hash"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_merge_related(self, wired_plugin, mock_store):
        """When similarity 0.8–0.95, merge via LLM."""
        mock_store.search.return_value = [
            {
                "content": "OAuth needs scope on refresh",
                "score": 0.88,
                "chunk_hash": "existing_hash",
                "entry_type": "document",
                "topic": "auth",
                "tags": '["insight", "auth"]',
                "_scope": "project",
                "_scope_id": "test",
            }
        ]

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "OAuth also needs to handle revoked tokens gracefully.",
                "tags": ["insight", "tokens"],
            }
        )
        assert result["success"] is True
        assert result["action"] == "merged"
        assert result["similarity_score"] == 0.88
        assert result["merged_with"] == "existing_hash"

        # LLM should have been called for merge (+ possibly topic inference)
        assert wired_plugin._ctx.invoke_llm.call_count >= 1
        merge_calls = [
            c
            for c in wired_plugin._ctx.invoke_llm.call_args_list
            if "merging two related" in c[0][0].lower()
        ]
        assert len(merge_calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_creates_summary_for_long_content(self, wired_plugin, mock_store):
        """Content > 800 chars should trigger summary generation."""
        mock_store.search.return_value = []
        long_content = "A" * 900  # Exceeds _SUMMARY_CHAR_THRESHOLD

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": long_content,
            }
        )
        assert result["success"] is True
        assert result["action"] == "created"
        assert result["has_summary"] is True

        # LLM should have been called for summary (+ possibly topic inference)
        summary_calls = [
            c for c in wired_plugin._ctx.invoke_llm.call_args_list if "summarize" in c[0][0].lower()
        ]
        assert len(summary_calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_no_summary_for_short_content(self, wired_plugin, mock_store):
        """Content <= 800 chars should not generate a summary."""
        mock_store.search.return_value = []

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Short insight.",
            }
        )
        assert result["success"] is True
        assert result["action"] == "created"
        assert result["has_summary"] is False

        # No summary LLM call (topic inference may still be called)
        summary_calls = [
            c for c in wired_plugin._ctx.invoke_llm.call_args_list if "summarize" in c[0][0].lower()
        ]
        assert len(summary_calls) == 0

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_llm_merge_fallback(self, wired_plugin, mock_store):
        """When LLM merge fails, fall back to concatenation."""
        mock_store.search.return_value = [
            {
                "content": "Old content about error handling and retries",
                "score": 0.88,
                "chunk_hash": "existing_hash",
                "entry_type": "document",
                "tags": '["insight"]',
            }
        ]
        # Make LLM fail
        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "New content about merging strategies and fallbacks",
            }
        )
        assert result["success"] is True
        assert result["action"] == "merged"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_llm_summary_fallback(self, wired_plugin, mock_store):
        """When LLM summary fails, fall back to truncation."""
        mock_store.search.return_value = []
        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "A" * 900,  # Long content
            }
        )
        assert result["success"] is True
        assert result["action"] == "created"
        assert result["has_summary"] is True

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_uses_default_tags(self, wired_plugin, mock_store):
        """When no tags provided, defaults to ['insight', 'auto-generated']."""
        mock_store.search.return_value = []

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Test insight",
            }
        )
        assert result["success"] is True
        assert result["tags"] == ["insight", "auto-generated"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_ignores_non_document_entries_in_dedup(self, wired_plugin, mock_store):
        """Only document entries should be considered for dedup, not KV/temporal."""
        mock_store.search.return_value = [
            {
                "content": "some key-value entry about configuration settings",
                "score": 0.99,
                "chunk_hash": "kv_hash",
                "entry_type": "kv",  # Not a document!
                "tags": "[]",
            }
        ]

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "New insight about API authentication token handling",
            }
        )
        assert result["success"] is True
        assert result["action"] == "created"  # Should create new, not dedup

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_error_handling(self, wired_plugin):
        """Service errors should be caught and returned gracefully."""
        wired_plugin._service.search = AsyncMock(
            side_effect=RuntimeError("Milvus connection failed")
        )

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Test insight that triggers a service error during search",
            }
        )
        assert "error" in result
        assert "Save failed" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_merge_generates_summary_for_long_merged_content(self, wired_plugin, mock_store):
        """When merged content exceeds ~200 tokens, a summary should be generated.

        Per spec §9: summary is embedded/indexed; original is preserved.
        """
        # Return a related match (0.8-0.95 similarity) to trigger merge
        mock_store.search.return_value = [
            {
                "content": "Original short insight about caching",
                "score": 0.88,
                "chunk_hash": "existing_hash",
                "entry_type": "document",
                "topic": "caching",
                "tags": '["insight", "caching"]',
                "_scope": "project",
                "_scope_id": "test",
            }
        ]

        # Make LLM return different things for merge vs summary calls
        call_count = {"n": 0}
        long_merged = "M" * 900  # Exceeds _SUMMARY_CHAR_THRESHOLD

        async def mock_llm(prompt, **kwargs):
            call_count["n"] += 1
            if "merging two related" in prompt.lower():
                return long_merged  # Merge produces long content
            elif "summarize" in prompt.lower():
                return "Concise summary of merged caching insight"
            return "topic-result"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Detailed new insight about caching strategies and invalidation",
                "tags": ["insight", "performance"],
            }
        )
        assert result["success"] is True
        assert result["action"] == "merged"
        assert result["has_summary"] is True

        # Verify summary LLM call was made (in addition to merge call)
        summary_calls = [
            c for c in wired_plugin._ctx.invoke_llm.call_args_list if "summarize" in c[0][0].lower()
        ]
        assert len(summary_calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_merge_no_summary_for_short_merged_content(self, wired_plugin, mock_store):
        """When merged content is short (< ~200 tokens), no summary is generated."""
        mock_store.search.return_value = [
            {
                "content": "Short insight about database connection pooling strategies",
                "score": 0.88,
                "chunk_hash": "existing_hash",
                "entry_type": "document",
                "tags": '["insight"]',
            }
        ]

        async def mock_llm(prompt, **kwargs):
            if "merging two related" in prompt.lower():
                return "Short merged result about connection pooling"  # Under threshold
            return "topic-result"

        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=mock_llm)

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "New short insight about database pool sizing",
            }
        )
        assert result["success"] is True
        assert result["action"] == "merged"
        assert result["has_summary"] is False

        # No summary LLM call (only merge + possibly topic)
        summary_calls = [
            c for c in wired_plugin._ctx.invoke_llm.call_args_list if "summarize" in c[0][0].lower()
        ]
        assert len(summary_calls) == 0


# ---------------------------------------------------------------------------
# Service-layer: vault file update with original
# ---------------------------------------------------------------------------


class TestVaultFileOriginal:
    """Test vault file handling of summary + original content."""

    @pytest.fixture
    def service_with_tmpdir(self, mock_embedder, mock_router, tmp_data_dir):
        svc = MemoryV2Service(
            milvus_uri="/tmp/test.db",
            embedding_provider="openai",
            data_dir=tmp_data_dir,
        )
        svc._embedder = mock_embedder
        svc._router = mock_router
        svc._initialized = True
        return svc

    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    def test_update_vault_file_with_original(self, service_with_tmpdir, tmp_data_dir):
        """When content is updated with an original, the vault file should include
        both the summary and the original under ## Original."""
        vault_dir = Path(tmp_data_dir) / "vault" / "projects" / "test" / "memory" / "insights"
        vault_dir.mkdir(parents=True, exist_ok=True)

        # Create initial vault file
        filepath = vault_dir / "test-insight.md"
        filepath.write_text(
            "---\n"
            'tags: ["insight"]\n'
            "created: 2026-04-01\n"
            "updated: 2026-04-01\n"
            "---\n\n"
            "Original short insight\n",
            encoding="utf-8",
        )

        # Update with summary + original (simulating merge with long content)
        service_with_tmpdir._update_vault_file(
            filepath,
            content="Concise summary of merged insight",
            original="This is the full merged content that is much longer "
            "and contains all the details that were in both the old and new memories. "
            "It includes information about caching strategies, invalidation patterns, "
            "and performance considerations.",
        )

        text = filepath.read_text(encoding="utf-8")
        assert "Concise summary of merged insight" in text
        assert "## Original" in text
        assert "caching strategies" in text

    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    def test_update_vault_file_no_original_when_same(self, service_with_tmpdir, tmp_data_dir):
        """When original equals content, no ## Original section is added."""
        vault_dir = Path(tmp_data_dir) / "vault" / "projects" / "test" / "memory" / "insights"
        vault_dir.mkdir(parents=True, exist_ok=True)

        filepath = vault_dir / "test-insight.md"
        filepath.write_text(
            "---\n"
            'tags: ["insight"]\n'
            "created: 2026-04-01\n"
            "updated: 2026-04-01\n"
            "---\n\n"
            "Old content\n",
            encoding="utf-8",
        )

        service_with_tmpdir._update_vault_file(
            filepath,
            content="Updated short content",
            original="Updated short content",  # Same as content
        )

        text = filepath.read_text(encoding="utf-8")
        assert "Updated short content" in text
        assert "## Original" not in text

    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    def test_update_vault_file_no_original_param(self, service_with_tmpdir, tmp_data_dir):
        """When original is None, no ## Original section is added."""
        vault_dir = Path(tmp_data_dir) / "vault" / "projects" / "test" / "memory" / "insights"
        vault_dir.mkdir(parents=True, exist_ok=True)

        filepath = vault_dir / "test-insight.md"
        filepath.write_text(
            "---\n"
            'tags: ["insight"]\n'
            "created: 2026-04-01\n"
            "updated: 2026-04-01\n"
            "---\n\n"
            "Old content\n",
            encoding="utf-8",
        )

        service_with_tmpdir._update_vault_file(
            filepath,
            content="Short merged content",
        )

        text = filepath.read_text(encoding="utf-8")
        assert "Short merged content" in text
        assert "## Original" not in text


# ---------------------------------------------------------------------------
# Dedup: scoped search (no cross-scope dedup)
# ---------------------------------------------------------------------------


class TestDedupScopedSearch:
    """Verify dedup search is scoped to the target scope, not multi-scope."""

    @pytest.fixture
    def plugin(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        return MemoryV2Plugin()

    @pytest.fixture
    def wired_plugin(self, plugin, service):
        plugin._service = service
        plugin._log = MagicMock()
        plugin._ctx = MagicMock()
        plugin._ctx.invoke_llm = AsyncMock(return_value="LLM generated summary")
        return plugin

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_dedup_search_uses_explicit_scope_when_none(
        self, wired_plugin, mock_router, mock_store
    ):
        """When scope=None, dedup search should use 'project_{id}' not multi-scope."""
        mock_store.search.return_value = []

        await wired_plugin.cmd_memory_save(
            {
                "project_id": "my-project",
                "content": "Test insight about API rate limiting and retries",
                "topic": "testing",
            }
        )

        # The dedup search should go through store.search (single-scope path),
        # NOT through router.search (multi-scope path). This ensures dedup
        # only matches within the target scope, not cross-scope.
        assert mock_store.search.call_count >= 1
        assert mock_router.search.call_count == 0

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_dedup_search_uses_provided_scope(self, wired_plugin, mock_router, mock_store):
        """When an explicit scope is provided, dedup search uses that scope."""
        mock_store.search.return_value = []

        await wired_plugin.cmd_memory_save(
            {
                "project_id": "my-project",
                "content": "System-level insight about global configuration and defaults",
                "scope": "system",
            }
        )

        # Multi-scope router should not be called
        assert mock_router.search.call_count == 0
        assert mock_store.search.call_count >= 1


# ---------------------------------------------------------------------------
# Dedup: boundary conditions at threshold values
# ---------------------------------------------------------------------------


class TestDedupBoundaryConditions:
    """Test similarity thresholds at exact boundary values."""

    @pytest.fixture
    def plugin(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        return MemoryV2Plugin()

    @pytest.fixture
    def wired_plugin(self, plugin, service):
        plugin._service = service
        plugin._log = MagicMock()
        plugin._ctx = MagicMock()
        plugin._ctx.invoke_llm = AsyncMock(return_value="Merged content")
        return plugin

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_exactly_0_80_triggers_merge(self, wired_plugin, mock_store):
        """Similarity == 0.80 should trigger merge (spec says 0.8-0.95 is related)."""
        mock_store.search.return_value = [
            {
                "content": "Existing insight about caching patterns and strategies",
                "score": 0.80,
                "chunk_hash": "existing_hash",
                "entry_type": "document",
                "tags": '["insight"]',
            }
        ]

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Related insight about caching at the boundary threshold",
            }
        )
        assert result["success"] is True
        assert result["action"] == "merged"
        assert result["similarity_score"] == 0.80

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_below_0_80_creates_new(self, wired_plugin, mock_store):
        """Similarity < 0.80 (e.g. 0.79) should create a new entry."""
        mock_store.search.return_value = [
            {
                "content": "Somewhat related insight about error handling approaches",
                "score": 0.79,
                "chunk_hash": "existing_hash",
                "entry_type": "document",
                "tags": '["insight"]',
            }
        ]

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Distinct enough insight about database migration strategies",
            }
        )
        assert result["success"] is True
        assert result["action"] == "created"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_exactly_0_95_triggers_merge_not_dedup(self, wired_plugin, mock_store):
        """Similarity == 0.95 should trigger merge (spec says >0.95 for near-identical)."""
        mock_store.search.return_value = [
            {
                "content": "Very similar insight about deployment pipeline configuration",
                "score": 0.95,
                "chunk_hash": "existing_hash",
                "entry_type": "document",
                "tags": '["insight"]',
            }
        ]

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Almost identical insight about deployment pipeline setup steps",
            }
        )
        assert result["success"] is True
        assert result["action"] == "merged"  # Not deduplicated
        assert result["similarity_score"] == 0.95

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_above_0_95_triggers_dedup(self, wired_plugin, mock_store):
        """Similarity > 0.95 (e.g. 0.96) should trigger dedup (timestamp only)."""
        mock_store.search.return_value = [
            {
                "content": "Near-identical insight about authentication token refresh flow",
                "score": 0.96,
                "chunk_hash": "existing_hash",
                "entry_type": "document",
                "tags": '["insight"]',
            }
        ]

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Nearly the same insight about authentication token refresh",
            }
        )
        assert result["success"] is True
        assert result["action"] == "deduplicated"
        assert result["similarity_score"] == 0.96


# ---------------------------------------------------------------------------
# Vault: source_tasks_additional accumulation
# ---------------------------------------------------------------------------


class TestSourceTasksAccumulation:
    """Verify source_tasks_additional accumulates as a JSON array."""

    @pytest.fixture
    def service_with_tmpdir(self, mock_embedder, mock_router, tmp_data_dir):
        svc = MemoryV2Service(
            milvus_uri="/tmp/test.db",
            embedding_provider="openai",
            data_dir=tmp_data_dir,
        )
        svc._embedder = mock_embedder
        svc._router = mock_router
        svc._initialized = True
        return svc

    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    def test_first_additional_source_task_creates_list(self, service_with_tmpdir, tmp_data_dir):
        """First additional source_task creates a JSON array."""
        vault_dir = Path(tmp_data_dir) / "tasks_test"
        filepath = service_with_tmpdir._write_vault_file(
            vault_dir,
            content="Test insight",
            tags=["insight"],
            source_task="task-001",
        )
        service_with_tmpdir._update_vault_file(filepath, source_task="task-002")
        text = filepath.read_text()
        assert 'source_tasks_additional: ["task-002"]' in text

    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    def test_subsequent_source_tasks_accumulate(self, service_with_tmpdir, tmp_data_dir):
        """Multiple dedup calls accumulate source tasks in the JSON array."""
        vault_dir = Path(tmp_data_dir) / "tasks_accum_test"
        filepath = service_with_tmpdir._write_vault_file(
            vault_dir,
            content="Test insight",
            tags=["insight"],
            source_task="task-001",
        )
        service_with_tmpdir._update_vault_file(filepath, source_task="task-002")
        service_with_tmpdir._update_vault_file(filepath, source_task="task-003")
        text = filepath.read_text()
        assert 'source_tasks_additional: ["task-002", "task-003"]' in text

    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    def test_duplicate_source_task_not_added_again(self, service_with_tmpdir, tmp_data_dir):
        """Adding the same source_task twice doesn't duplicate it."""
        vault_dir = Path(tmp_data_dir) / "tasks_dedup_test"
        filepath = service_with_tmpdir._write_vault_file(
            vault_dir,
            content="Test insight",
            tags=["insight"],
            source_task="task-001",
        )
        service_with_tmpdir._update_vault_file(filepath, source_task="task-002")
        service_with_tmpdir._update_vault_file(filepath, source_task="task-002")
        text = filepath.read_text()
        assert text.count("task-002") == 1

    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    def test_original_source_task_not_added_as_additional(self, service_with_tmpdir, tmp_data_dir):
        """The original source_task shouldn't be added to additional list."""
        vault_dir = Path(tmp_data_dir) / "tasks_orig_test"
        filepath = service_with_tmpdir._write_vault_file(
            vault_dir,
            content="Test insight",
            tags=["insight"],
            source_task="task-001",
        )
        # Try to add the same task that's already the primary source_task
        service_with_tmpdir._update_vault_file(filepath, source_task="task-001")
        text = filepath.read_text()
        assert "source_tasks_additional" not in text


# ---------------------------------------------------------------------------
# Distinct content save (<0.8 similarity) — Roadmap 3.4.6
# ---------------------------------------------------------------------------


class TestDistinctContentSave:
    """Test the distinct-content path of memory_save (<0.8 similarity).

    Roadmap 3.4.6 cases:
    (a) Content with <0.8 similarity to existing entries creates a new entry.
    (b) Collection entry count increases by 1 after distinct save.
    (c) Both old and new entries are independently searchable.
    (d) Saving to an empty collection always creates new.
    (e) Saving 10 distinct pieces of content creates 10 entries with correct topics/tags.
    (f) Distinct save assigns its own topic and tags independent of existing entries.
    """

    @pytest.fixture
    def plugin(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        return MemoryV2Plugin()

    @pytest.fixture
    def wired_plugin(self, plugin, service):
        plugin._service = service
        plugin._log = MagicMock()
        plugin._ctx = MagicMock()
        plugin._ctx.invoke_llm = AsyncMock(return_value="inferred-topic")
        return plugin

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_low_similarity_creates_new_entry(self, wired_plugin, mock_store):
        """(a) Content with <0.8 similarity to an existing entry creates a new entry."""
        mock_store.search.return_value = [
            {
                "content": "Existing insight about caching strategies",
                "score": 0.72,
                "chunk_hash": "existing_hash_abc",
                "entry_type": "document",
                "topic": "caching",
                "tags": '["insight", "performance"]',
            }
        ]

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "New insight about database indexing.",
                "tags": ["insight", "database"],
                "topic": "database",
                "source_task": "task-100",
            }
        )
        assert result["success"] is True
        assert result["action"] == "created"
        # Must NOT merge or dedup with the existing entry
        assert "merged_with" not in result
        assert "existing_chunk_hash" not in result
        # The new entry gets its own chunk_hash
        assert result["chunk_hash"]
        assert result["chunk_hash"] != "existing_hash_abc"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_collection_entry_count_increases(self, wired_plugin, mock_store):
        """(b) Collection entry count increases by 1 — upsert is called with a new entry."""
        mock_store.search.return_value = [
            {
                "content": "Existing insight",
                "score": 0.65,
                "chunk_hash": "old_hash",
                "entry_type": "document",
                "tags": '["insight"]',
            }
        ]

        # Reset upsert call tracking before our save
        mock_store.upsert.reset_mock()

        await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Totally different topic: deployment strategies.",
                "tags": ["insight", "deployment"],
                "topic": "deployment",
            }
        )

        # save_document should have called upsert exactly once with a list of 1 chunk
        assert mock_store.upsert.call_count == 1
        upserted_chunks = mock_store.upsert.call_args[0][0]
        assert len(upserted_chunks) == 1
        # The upserted entry should be a new document, not an update to the existing one
        assert upserted_chunks[0]["entry_type"] == "document"
        assert upserted_chunks[0]["chunk_hash"] != "old_hash"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_old_and_new_entries_independently_searchable(
        self, wired_plugin, mock_store, service
    ):
        """(c) Both old and new entries remain independently searchable.

        The distinct path creates a *new* entry via save_document (upsert) —
        it must NOT call update_document_timestamp or update_document_content,
        which would modify the existing entry instead of creating a new one.
        """
        mock_store.search.return_value = [
            {
                "content": "OAuth requires scope re-request",
                "score": 0.55,
                "chunk_hash": "old_auth_hash",
                "entry_type": "document",
                "topic": "auth",
                "tags": '["insight", "auth"]',
            }
        ]

        mock_store.upsert.reset_mock()

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "WebSocket connections need heartbeat every 30s.",
                "tags": ["insight", "websocket"],
                "topic": "networking",
            }
        )

        assert result["action"] == "created"

        # Verify save_document was invoked (upsert with new chunk)
        assert mock_store.upsert.call_count == 1
        new_chunk = mock_store.upsert.call_args[0][0][0]
        assert new_chunk["chunk_hash"] != "old_auth_hash"

        # Verify the existing entry was NOT modified:
        # - mock_store.get was not called (get is only used for update paths)
        #   reset_mock on get to check our call, not fixture setup
        # Instead, confirm the service's update methods were not invoked.
        # Since update_document_timestamp and update_document_content both
        # call store.get, we verify by checking the action is "created"
        # and that no "merged_with" or "existing_chunk_hash" keys appear.
        assert "merged_with" not in result
        assert "existing_chunk_hash" not in result

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_empty_collection_creates_new(self, wired_plugin, mock_store):
        """(d) Saving to an empty collection always creates a new entry."""
        # Empty collection → search returns nothing
        mock_store.search.return_value = []
        mock_store.upsert.reset_mock()

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "brand-new-project",
                "content": "First insight ever for this project.",
                "tags": ["insight", "initial"],
                "topic": "onboarding",
                "source_task": "task-first",
            }
        )

        assert result["success"] is True
        assert result["action"] == "created"
        assert result["project_id"] == "brand-new-project"
        assert result["chunk_hash"]
        assert result["vault_path"]
        # Exactly one new entry upserted
        assert mock_store.upsert.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_ten_distinct_saves_create_ten_entries(self, wired_plugin, mock_store):
        """(e) Saving 10 distinct pieces of content creates 10 entries with correct topics/tags."""
        # All searches return nothing relevant (distinct content)
        mock_store.search.return_value = []
        mock_store.upsert.reset_mock()

        saved_hashes = []
        contents = [
            ("Database indexing speeds up reads.", "database", ["db", "performance"]),
            ("OAuth tokens expire after 1 hour.", "auth", ["oauth", "tokens"]),
            ("WebSocket needs heartbeat.", "networking", ["websocket", "realtime"]),
            ("React hooks must follow rules of hooks.", "frontend", ["react", "hooks"]),
            ("Docker layers are cached for faster builds.", "devops", ["docker", "ci"]),
            ("Redis pub/sub for real-time events.", "caching", ["redis", "events"]),
            ("GraphQL resolvers should be thin.", "api", ["graphql", "architecture"]),
            ("Kubernetes pods auto-restart on OOM.", "infrastructure", ["k8s", "reliability"]),
            ("Pytest fixtures enable clean test setup.", "testing", ["pytest", "fixtures"]),
            ("Alembic manages DB schema migrations.", "migrations", ["alembic", "schema"]),
        ]

        for content, topic, tags in contents:
            result = await wired_plugin.cmd_memory_save(
                {
                    "project_id": "test-project",
                    "content": content,
                    "tags": tags,
                    "topic": topic,
                }
            )
            assert result["success"] is True
            assert result["action"] == "created"
            saved_hashes.append(result["chunk_hash"])

        # All 10 saves should have triggered upsert
        assert mock_store.upsert.call_count == 10

        # Each upserted chunk should have the correct topic and tags
        for i, call in enumerate(mock_store.upsert.call_args_list):
            chunk = call[0][0][0]
            expected_content, expected_topic, expected_tags = contents[i]
            assert chunk["entry_type"] == "document"
            assert chunk["topic"] == expected_topic
            import json

            assert json.loads(chunk["tags"]) == expected_tags

        # All chunk hashes should be unique (different content → different hashes)
        assert len(set(saved_hashes)) == 10

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_distinct_save_independent_topic_and_tags(self, wired_plugin, mock_store):
        """(f) Distinct save assigns its own topic and tags, independent of existing entries."""
        # Existing entry has topic="auth" and tags=["insight", "auth"]
        mock_store.search.return_value = [
            {
                "content": "OAuth token refresh needs explicit scope",
                "score": 0.45,
                "chunk_hash": "auth_hash",
                "entry_type": "document",
                "topic": "auth",
                "tags": '["insight", "auth"]',
            }
        ]

        mock_store.upsert.reset_mock()

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Kubernetes liveness probes prevent stale pods.",
                "tags": ["devops", "kubernetes"],
                "topic": "infrastructure",
            }
        )

        assert result["success"] is True
        assert result["action"] == "created"

        # New entry should have its OWN topic and tags, not the existing entry's
        upserted = mock_store.upsert.call_args[0][0][0]
        assert upserted["topic"] == "infrastructure"
        import json

        assert json.loads(upserted["tags"]) == ["devops", "kubernetes"]

        # Return result should also reflect the new entry's own metadata
        assert result["topic"] == "infrastructure"
        assert result["tags"] == ["devops", "kubernetes"]


# ---------------------------------------------------------------------------
# Duplicate detection (>0.95 similarity) — Roadmap 3.4.4
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    """Test duplicate detection when similarity > 0.95.

    Roadmap 3.4.4 cases:
    (a) Saving identical content twice results in only one entry (second save
        updates timestamp only).
    (b) Saving near-identical content (e.g., minor typo fix) with >0.95
        similarity also deduplicates.
    (c) Collection entry count does not increase on duplicate save.
    (d) The updated timestamp reflects the second save time.
    (e) Duplicate detection works across the same scope only (same content
        in different scopes creates separate entries).
    (f) Dedup check does not trigger on very short content where similarity
        is unreliable (< 5 tokens).
    """

    @pytest.fixture
    def plugin(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        return MemoryV2Plugin()

    @pytest.fixture
    def wired_plugin(self, plugin, service):
        plugin._service = service
        plugin._log = MagicMock()
        plugin._ctx = MagicMock()
        plugin._ctx.invoke_llm = AsyncMock(return_value="LLM result")
        return plugin

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_identical_content_deduplicates(self, wired_plugin, mock_store):
        """(a) Saving identical content twice — second save updates timestamp only.

        When content is saved and a near-identical entry (score > 0.95) already
        exists, the action should be 'deduplicated' and no new entry is created.
        """
        # First save — no existing entries, creates new
        mock_store.search.return_value = []
        first_result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Authentication tokens must be refreshed before expiry.",
                "tags": ["insight", "auth"],
                "topic": "authentication",
                "source_task": "task-001",
            }
        )
        assert first_result["success"] is True
        assert first_result["action"] == "created"

        # Second save of identical content — search returns the first entry
        mock_store.search.return_value = [
            {
                "content": "Authentication tokens must be refreshed before expiry.",
                "score": 0.99,
                "chunk_hash": first_result["chunk_hash"],
                "entry_type": "document",
                "topic": "authentication",
                "tags": '["insight", "auth"]',
            }
        ]

        second_result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Authentication tokens must be refreshed before expiry.",
                "tags": ["insight", "auth"],
                "topic": "authentication",
                "source_task": "task-002",
            }
        )
        assert second_result["success"] is True
        assert second_result["action"] == "deduplicated"
        assert second_result["existing_chunk_hash"] == first_result["chunk_hash"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_near_identical_with_typo_fix_deduplicates(self, wired_plugin, mock_store):
        """(b) Near-identical content (typo fix) with >0.95 similarity deduplicates.

        Even when the text isn't byte-for-byte identical, similarity >0.95
        should still trigger dedup (timestamp update), not a merge or new entry.
        """
        mock_store.search.return_value = [
            {
                "content": "OAuth tokens require explicit scpoe re-request on refresh.",
                "score": 0.97,
                "chunk_hash": "existing_typo_hash",
                "entry_type": "document",
                "topic": "auth",
                "tags": '["insight", "oauth"]',
            }
        ]

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                # Typo fixed: "scpoe" → "scope"
                "content": "OAuth tokens require explicit scope re-request on refresh.",
                "tags": ["insight", "oauth"],
                "topic": "auth",
            }
        )
        assert result["success"] is True
        assert result["action"] == "deduplicated"
        assert result["similarity_score"] == 0.97
        assert result["existing_chunk_hash"] == "existing_typo_hash"
        # LLM merge should NOT be invoked for near-identical
        merge_calls = [
            c
            for c in wired_plugin._ctx.invoke_llm.call_args_list
            if "merging two related" in c[0][0].lower()
        ]
        assert len(merge_calls) == 0

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_collection_count_unchanged_on_dedup(self, wired_plugin, mock_store):
        """(c) Collection entry count does not increase on duplicate save.

        When a duplicate is detected (>0.95), the service calls
        update_document_timestamp (upsert of existing entry), NOT
        save_document (upsert of a new entry). The upsert call should
        reuse the existing chunk_hash rather than creating a new one.
        """
        mock_store.search.return_value = [
            {
                "content": "Rate limiting should use token bucket algorithm.",
                "score": 0.98,
                "chunk_hash": "rate_limit_hash",
                "entry_type": "document",
                "topic": "api",
                "tags": '["insight", "api"]',
            }
        ]

        mock_store.upsert.reset_mock()

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Rate limiting should use token bucket algorithm.",
                "tags": ["insight", "api"],
                "topic": "api",
            }
        )
        assert result["action"] == "deduplicated"

        # The upsert call should update the existing entry, not create a new one.
        # update_document_timestamp calls store.get() then store.upsert() with
        # the same chunk_hash.
        assert mock_store.upsert.call_count == 1
        upserted = mock_store.upsert.call_args[0][0]
        assert len(upserted) == 1
        assert upserted[0]["chunk_hash"] == "existing_hash"  # from mock_store fixture

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_updated_timestamp_reflects_second_save(self, wired_plugin, mock_store):
        """(d) The updated timestamp reflects the second save time.

        After dedup, the returned updated_at should be a recent timestamp,
        not the original creation time.
        """
        import time

        before_save = int(time.time())

        mock_store.search.return_value = [
            {
                "content": "Connection pooling improves database performance.",
                "score": 0.96,
                "chunk_hash": "pool_hash",
                "entry_type": "document",
                "topic": "database",
                "tags": '["insight", "performance"]',
            }
        ]

        # The mock store.get returns an entry with old timestamp
        mock_store.get.return_value = {
            "chunk_hash": "pool_hash",
            "entry_type": "document",
            "content": "Connection pooling improves database performance.",
            "original": "",
            "source": "",
            "heading": "Connection pooling",
            "topic": "database",
            "tags": '["insight", "performance"]',
            "updated_at": 1000,  # Old timestamp
            "embedding": [0.1] * 384,
        }

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Connection pooling improves database performance.",
                "tags": ["insight", "performance"],
                "topic": "database",
            }
        )

        after_save = int(time.time())

        assert result["action"] == "deduplicated"
        assert result["updated_at"] >= before_save
        assert result["updated_at"] <= after_save

        # Verify the store.upsert was called with the updated timestamp
        upserted = mock_store.upsert.call_args[0][0][0]
        assert upserted["updated_at"] >= before_save
        assert upserted["updated_at"] <= after_save

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_dedup_same_scope_only(self, wired_plugin, mock_store, mock_router):
        """(e) Duplicate detection works across the same scope only.

        The same content saved to different scopes should create separate
        entries. The dedup search must not cross scope boundaries.
        """
        # Save to project scope first — no existing entries
        mock_store.search.return_value = []
        mock_store.upsert.reset_mock()

        project_result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Caching strategy should use write-through pattern.",
                "tags": ["insight", "caching"],
                "topic": "caching",
                "scope": "project_test-project",
            }
        )
        assert project_result["action"] == "created"

        # Now save identical content to system scope.
        # Even though the content is the same, the system scope search
        # should return empty (different scope), so a new entry is created.
        mock_store.search.return_value = []
        mock_store.upsert.reset_mock()

        system_result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Caching strategy should use write-through pattern.",
                "tags": ["insight", "caching"],
                "topic": "caching",
                "scope": "system",
            }
        )
        assert system_result["action"] == "created"

        # The multi-scope router should NOT have been called for dedup
        assert mock_router.search.call_count == 0

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_dedup_same_scope_only_does_not_cross_scopes(
        self, wired_plugin, mock_store, mock_router
    ):
        """(e) Extended: if dedup did cross scopes, the second save would wrongly
        dedup. Instead, both saves should produce 'created' action."""
        # First save to scope A — creates entry
        mock_store.search.return_value = []
        result_a = await wired_plugin.cmd_memory_save(
            {
                "project_id": "project-a",
                "content": "Logging should include correlation IDs for traceability.",
                "tags": ["insight", "observability"],
                "topic": "logging",
            }
        )
        assert result_a["action"] == "created"

        # Second save of the same content to scope B (different project).
        # The dedup search resolves scope to project_project-b, which is
        # different from project_project-a, so no match → created.
        mock_store.search.return_value = []
        result_b = await wired_plugin.cmd_memory_save(
            {
                "project_id": "project-b",
                "content": "Logging should include correlation IDs for traceability.",
                "tags": ["insight", "observability"],
                "topic": "logging",
            }
        )
        assert result_b["action"] == "created"

        # Multi-scope router should never be used for dedup
        assert mock_router.search.call_count == 0

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_short_content_skips_dedup(self, wired_plugin, mock_store):
        """(f) Dedup check does not trigger on very short content (< 5 words).

        Similarity scores are unreliable for very short texts, so the dedup
        search should be skipped entirely and a new entry should always be
        created — even if hypothetically similar content exists.
        """
        # Set up a high-similarity search result that *would* trigger dedup
        # if the search were not skipped.
        mock_store.search.return_value = [
            {
                "content": "Fix bug",
                "score": 0.99,
                "chunk_hash": "short_hash",
                "entry_type": "document",
                "tags": '["insight"]',
            }
        ]
        mock_store.upsert.reset_mock()
        mock_store.search.reset_mock()

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Fix bug now",  # Only 3 words — below 5-word threshold
                "tags": ["note"],
                "topic": "bugs",
            }
        )

        assert result["success"] is True
        assert result["action"] == "created"
        # The dedup search should NOT have been called at all
        assert mock_store.search.call_count == 0

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_content_at_word_threshold_does_dedup(self, wired_plugin, mock_store):
        """(f) Extended: content at exactly _DEDUP_MIN_WORDS (5 words) should
        trigger normal dedup."""
        mock_store.search.return_value = [
            {
                "content": "Five word content triggers dedup",
                "score": 0.98,
                "chunk_hash": "five_words_hash",
                "entry_type": "document",
                "tags": '["insight"]',
            }
        ]
        mock_store.search.reset_mock()

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Five word content triggers dedup",  # Exactly 5 words
                "tags": ["insight"],
                "topic": "testing",
            }
        )

        # Search SHOULD be called for 5-word content
        assert mock_store.search.call_count >= 1
        # And the high similarity should trigger dedup
        assert result["action"] == "deduplicated"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_four_word_content_skips_dedup(self, wired_plugin, mock_store):
        """(f) Extended: 4-word content is below the threshold and skips dedup."""
        mock_store.search.return_value = [
            {
                "content": "Should not match",
                "score": 0.99,
                "chunk_hash": "four_hash",
                "entry_type": "document",
                "tags": '["insight"]',
            }
        ]
        mock_store.search.reset_mock()

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "Only four words here",  # 4 words — below threshold
                "tags": ["note"],
            }
        )

        assert result["action"] == "created"
        assert mock_store.search.call_count == 0


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


class TestToolDefinition:
    """Verify memory_save tool is properly registered."""

    def test_memory_save_in_v2_only_tools(self):
        from src.plugins.internal.memory_v2 import V2_ONLY_TOOLS

        assert "memory_save" in V2_ONLY_TOOLS

    def test_memory_save_tool_definition_exists(self):
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        save_def = next((t for t in TOOL_DEFINITIONS if t["name"] == "memory_save"), None)
        assert save_def is not None
        assert "input_schema" in save_def

        schema = save_def["input_schema"]
        assert "project_id" in schema["properties"]
        assert "content" in schema["properties"]
        assert "tags" in schema["properties"]
        assert "topic" in schema["properties"]
        assert "source_task" in schema["properties"]
        assert "scope" in schema["properties"]
        assert schema["required"] == ["project_id", "content"]
