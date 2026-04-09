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
    async def test_save_creates_new_distinct(self, wired_plugin, mock_router):
        """When no similar entries exist, create a new entry."""
        # No search results → distinct content
        mock_router.search = AsyncMock(return_value=[])

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
    async def test_save_dedup_near_identical(self, wired_plugin, mock_router):
        """When similarity > 0.95, deduplicate (update timestamp only)."""
        mock_router.search = AsyncMock(
            return_value=[
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
        )

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
    async def test_save_merge_related(self, wired_plugin, mock_router):
        """When similarity 0.8–0.95, merge via LLM."""
        mock_router.search = AsyncMock(
            return_value=[
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
        )

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
    async def test_save_creates_summary_for_long_content(self, wired_plugin, mock_router):
        """Content > 800 chars should trigger summary generation."""
        mock_router.search = AsyncMock(return_value=[])
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
    async def test_save_no_summary_for_short_content(self, wired_plugin, mock_router):
        """Content <= 800 chars should not generate a summary."""
        mock_router.search = AsyncMock(return_value=[])

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
    async def test_save_llm_merge_fallback(self, wired_plugin, mock_router):
        """When LLM merge fails, fall back to concatenation."""
        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": "Old content",
                    "score": 0.88,
                    "chunk_hash": "existing_hash",
                    "entry_type": "document",
                    "tags": '["insight"]',
                }
            ]
        )
        # Make LLM fail
        wired_plugin._ctx.invoke_llm = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "New content to merge",
            }
        )
        assert result["success"] is True
        assert result["action"] == "merged"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_save_llm_summary_fallback(self, wired_plugin, mock_router):
        """When LLM summary fails, fall back to truncation."""
        mock_router.search = AsyncMock(return_value=[])
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
    async def test_save_uses_default_tags(self, wired_plugin, mock_router):
        """When no tags provided, defaults to ['insight', 'auto-generated']."""
        mock_router.search = AsyncMock(return_value=[])

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
    async def test_save_ignores_non_document_entries_in_dedup(self, wired_plugin, mock_router):
        """Only document entries should be considered for dedup, not KV/temporal."""
        mock_router.search = AsyncMock(
            return_value=[
                {
                    "content": "some kv entry",
                    "score": 0.99,
                    "chunk_hash": "kv_hash",
                    "entry_type": "kv",  # Not a document!
                    "tags": "[]",
                }
            ]
        )

        result = await wired_plugin.cmd_memory_save(
            {
                "project_id": "test-project",
                "content": "New insight",
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
                "content": "Test",
            }
        )
        assert "error" in result
        assert "Save failed" in result["error"]


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
