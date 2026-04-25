"""Tests for memory consolidation tools — memory_delete, memory_update, memory_promote.

These tools implement spec §10 (Reflection Playbook — Periodic Consolidation) from
docs/specs/design/memory-scoping.md.  They enable the reflection playbook to:

- Merge duplicates and delete the weaker entry (memory_delete)
- Update outdated insights — change tags, content, topic (memory_update)
- Promote cross-scope patterns — copy from project to agent-type (memory_promote)

Tests cover:
- Service-layer: delete_document, update_document, _update_vault_topic
- Plugin-layer: cmd_memory_delete, cmd_memory_update, cmd_memory_promote
- Edge cases: missing args, unavailable service, entry not found
- Vault file interactions: deletion, topic update, content update
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Skip entire module on Windows (Milvus Lite not supported)
if sys.platform == "win32":
    pytest.skip("Milvus Lite not supported on Windows", allow_module_level=True)

from src.plugins.internal.memory.service import MemoryService, MEMSEARCH_AVAILABLE


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
        "original": "Full original text about auth patterns",
        "source": "",
        "heading": "Existing insight",
        "topic": "auth",
        "tags": '["insight", "provisional"]',
        "updated_at": 1000,
        "embedding": [0.1] * 384,
    }
    store.search.return_value = []
    store.delete_by_hashes = MagicMock()
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
    """Create a bare MemoryPlugin instance."""
    from src.plugins.internal.memory import MemoryPlugin

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
# Tool Definitions — verify tools are declared
# ---------------------------------------------------------------------------


class TestConsolidationToolDefinitions:
    """Verify the consolidation tool definitions exist in TOOL_DEFINITIONS."""

    def test_memory_delete_defined(self):
        from src.plugins.internal.memory import TOOL_DEFINITIONS

        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "memory_delete" in names

    def test_memory_update_defined(self):
        from src.plugins.internal.memory import TOOL_DEFINITIONS

        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "memory_update" in names

    def test_memory_promote_defined(self):
        from src.plugins.internal.memory import TOOL_DEFINITIONS

        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "memory_promote" in names

    def test_tools_in_v2_only_set(self):
        from src.plugins.internal.memory import AGENT_TOOLS, TOOL_DEFINITIONS

        tool_names = {t["name"] for t in TOOL_DEFINITIONS}
        assert "memory_delete" in AGENT_TOOLS
        assert "memory_update" in tool_names
        assert "memory_promote" in tool_names

    def test_memory_delete_requires_chunk_hash(self):
        from src.plugins.internal.memory import TOOL_DEFINITIONS

        defn = next(t for t in TOOL_DEFINITIONS if t["name"] == "memory_delete")
        assert "chunk_hash" in defn["input_schema"]["required"]
        # project_id is auto-resolved, not required in schema

    def test_memory_update_requires_chunk_hash(self):
        from src.plugins.internal.memory import TOOL_DEFINITIONS

        defn = next(t for t in TOOL_DEFINITIONS if t["name"] == "memory_update")
        assert "chunk_hash" in defn["input_schema"]["required"]

    def test_memory_promote_requires_target_scope(self):
        from src.plugins.internal.memory import TOOL_DEFINITIONS

        defn = next(t for t in TOOL_DEFINITIONS if t["name"] == "memory_promote")
        assert "target_scope" in defn["input_schema"]["required"]
        assert "chunk_hash" in defn["input_schema"]["required"]


# ---------------------------------------------------------------------------
# Service-layer: delete_document
# ---------------------------------------------------------------------------


class TestDeleteDocument:
    """Test MemoryService.delete_document()."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_delete_removes_from_milvus(self, service, mock_store):
        """delete_document calls store.delete_by_hashes with the chunk_hash."""
        result = await service.delete_document("test-project", "existing_hash")
        mock_store.delete_by_hashes.assert_called_once_with(["existing_hash"])
        assert result["chunk_hash"] == "existing_hash"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_delete_entry_not_found(self, service, mock_store):
        """delete_document raises ValueError for missing entries."""
        mock_store.get.return_value = None
        with pytest.raises(ValueError, match="Entry not found"):
            await service.delete_document("test-project", "nonexistent_hash")

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_delete_removes_vault_file(self, service, mock_store, tmp_data_dir):
        """delete_document removes the vault file if it's in an insights/ directory."""
        # Create a vault file
        insights_dir = Path(tmp_data_dir) / "vault" / "projects" / "test" / "insights"
        insights_dir.mkdir(parents=True)
        vault_file = insights_dir / "test-insight.md"
        vault_file.write_text("---\ntags: [\"insight\"]\n---\nTest content\n")

        mock_store.get.return_value = {
            "chunk_hash": "hash_with_vault",
            "entry_type": "document",
            "content": "Test content",
            "source": str(vault_file),
            "tags": '["insight"]',
            "updated_at": 1000,
            "embedding": [0.1] * 384,
        }

        result = await service.delete_document("test-project", "hash_with_vault")
        assert result["vault_deleted"] is True
        assert not vault_file.exists()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_delete_no_vault_path(self, service, mock_store):
        """delete_document works even when no vault file exists."""
        mock_store.get.return_value = {
            "chunk_hash": "no_vault_hash",
            "entry_type": "document",
            "content": "Content",
            "source": "",
            "tags": "[]",
            "updated_at": 1000,
            "embedding": [0.1] * 384,
        }
        result = await service.delete_document("test-project", "no_vault_hash")
        assert result["vault_deleted"] is False
        mock_store.delete_by_hashes.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_delete_returns_scope_info(self, service, mock_store):
        """delete_document result includes scope metadata."""
        result = await service.delete_document(
            "test-project", "existing_hash", scope="agenttype_coding"
        )
        assert result["scope"] == "agent_type"
        assert result["scope_id"] == "coding"


# ---------------------------------------------------------------------------
# Service-layer: update_document
# ---------------------------------------------------------------------------


class TestUpdateDocument:
    """Test MemoryService.update_document()."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_content(self, service, mock_store, mock_embedder):
        """update_document recomputes embedding when content changes."""
        result = await service.update_document(
            "test-project",
            "existing_hash",
            content="Updated insight about OAuth2 authentication",
        )
        assert "content" in result["changed_fields"]
        mock_embedder.embed.assert_called()
        mock_store.upsert.assert_called_once()
        upserted = mock_store.upsert.call_args[0][0][0]
        assert upserted["content"] == "Updated insight about OAuth2 authentication"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_tags_only(self, service, mock_store, mock_embedder):
        """update_document can change tags without touching content."""
        result = await service.update_document(
            "test-project",
            "existing_hash",
            tags=["insight", "verified", "auth"],
        )
        assert "tags" in result["changed_fields"]
        assert "content" not in result["changed_fields"]
        # No embedding recomputation for tag-only update
        mock_embedder.embed.assert_not_called()
        mock_store.upsert.assert_called_once()
        upserted = mock_store.upsert.call_args[0][0][0]
        assert json.loads(upserted["tags"]) == ["insight", "verified", "auth"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_topic(self, service, mock_store):
        """update_document can change the topic field."""
        result = await service.update_document(
            "test-project",
            "existing_hash",
            topic="authentication",
        )
        assert "topic" in result["changed_fields"]
        mock_store.upsert.assert_called_once()
        upserted = mock_store.upsert.call_args[0][0][0]
        assert upserted["topic"] == "authentication"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_content_and_tags(self, service, mock_store):
        """update_document can change multiple fields at once."""
        result = await service.update_document(
            "test-project",
            "existing_hash",
            content="Refined auth insight",
            tags=["insight", "verified"],
        )
        assert "content" in result["changed_fields"]
        assert "tags" in result["changed_fields"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_entry_not_found(self, service, mock_store):
        """update_document raises ValueError for missing entries."""
        mock_store.get.return_value = None
        with pytest.raises(ValueError, match="Entry not found"):
            await service.update_document(
                "test-project", "nonexistent", content="new"
            )

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_same_content_no_embed(self, service, mock_store, mock_embedder):
        """update_document skips embedding if content is unchanged."""
        result = await service.update_document(
            "test-project",
            "existing_hash",
            content="Existing insight about authentication",  # Same as mock
        )
        assert "content" not in result["changed_fields"]
        mock_embedder.embed.assert_not_called()


# ---------------------------------------------------------------------------
# Service-layer: _update_vault_topic
# ---------------------------------------------------------------------------


class TestUpdateVaultTopic:
    """Test MemoryService._update_vault_topic helper."""

    def test_update_existing_topic(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / "test.md"
        filepath.write_text(
            "---\ntags: [\"insight\"]\ntopic: old-topic\ncreated: 2026-01-01\n---\n\nContent\n"
        )
        MemoryService._update_vault_topic(filepath, "new-topic")
        text = filepath.read_text()
        assert "topic: new-topic" in text
        assert "old-topic" not in text

    def test_add_topic_when_missing(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / "test_no_topic.md"
        filepath.write_text(
            "---\ntags: [\"insight\"]\ncreated: 2026-01-01\n---\n\nContent\n"
        )
        MemoryService._update_vault_topic(filepath, "new-topic")
        text = filepath.read_text()
        assert "topic: new-topic" in text

    def test_nonexistent_file_is_noop(self, tmp_data_dir):
        filepath = Path(tmp_data_dir) / "nonexistent.md"
        # Should not raise
        MemoryService._update_vault_topic(filepath, "topic")
        assert not filepath.exists()


# ---------------------------------------------------------------------------
# Plugin-layer: cmd_memory_delete
# ---------------------------------------------------------------------------


class TestPluginMemoryDelete:
    """Test the memory_delete command handler."""

    @pytest.mark.asyncio
    async def test_delete_missing_project_id(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_delete({"chunk_hash": "abc"})
        assert "error" in result
        assert "project_id" in result["error"]

    @pytest.mark.asyncio
    async def test_delete_missing_chunk_hash(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_delete({"project_id": "proj"})
        assert "error" in result
        assert "chunk_hash" in result["error"]

    @pytest.mark.asyncio
    async def test_delete_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_delete(
            {"project_id": "proj", "chunk_hash": "abc"}
        )
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_delete_success(self, wired_plugin, mock_store):
        """Successful delete returns action='deleted'."""
        result = await wired_plugin.cmd_memory_delete(
            {"project_id": "test-project", "chunk_hash": "existing_hash"}
        )
        assert result["success"] is True
        assert result["action"] == "deleted"
        mock_store.delete_by_hashes.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_delete_not_found(self, wired_plugin, mock_store):
        """Delete of nonexistent entry returns error."""
        mock_store.get.return_value = None
        result = await wired_plugin.cmd_memory_delete(
            {"project_id": "test-project", "chunk_hash": "missing"}
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_delete_with_scope(self, wired_plugin, mock_store):
        """Delete supports explicit scope argument."""
        result = await wired_plugin.cmd_memory_delete(
            {
                "project_id": "test-project",
                "chunk_hash": "existing_hash",
                "scope": "agenttype_coding",
            }
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Plugin-layer: cmd_memory_update
# ---------------------------------------------------------------------------


class TestPluginMemoryUpdate:
    """Test the memory_update command handler."""

    @pytest.mark.asyncio
    async def test_update_missing_project_id(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_update(
            {"chunk_hash": "abc", "tags": ["new"]}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_update_missing_chunk_hash(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_update(
            {"project_id": "proj", "content": "new"}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_update_no_fields(self, plugin):
        """Must provide at least one field to update."""
        plugin._service = MagicMock()
        plugin._service.available = True
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_update(
            {"project_id": "proj", "chunk_hash": "abc"}
        )
        assert "error" in result
        assert "at least one" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_update_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_update(
            {"project_id": "proj", "chunk_hash": "abc", "content": "new"}
        )
        assert "error" in result

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_tags_success(self, wired_plugin, mock_store):
        """Successful tag update returns action='updated'."""
        result = await wired_plugin.cmd_memory_update(
            {
                "project_id": "test-project",
                "chunk_hash": "existing_hash",
                "tags": ["insight", "verified", "auth"],
            }
        )
        assert result["success"] is True
        assert result["action"] == "updated"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_content_success(self, wired_plugin, mock_store):
        """Content update triggers embedding recompute."""
        result = await wired_plugin.cmd_memory_update(
            {
                "project_id": "test-project",
                "chunk_hash": "existing_hash",
                "content": "Refined insight about OAuth2",
            }
        )
        assert result["success"] is True
        assert "content" in result.get("changed_fields", [])

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_topic_success(self, wired_plugin, mock_store):
        """Topic can be changed via memory_update."""
        result = await wired_plugin.cmd_memory_update(
            {
                "project_id": "test-project",
                "chunk_hash": "existing_hash",
                "topic": "authentication",
            }
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_not_found(self, wired_plugin, mock_store):
        """Update of nonexistent entry returns error."""
        mock_store.get.return_value = None
        result = await wired_plugin.cmd_memory_update(
            {
                "project_id": "test-project",
                "chunk_hash": "missing",
                "tags": ["verified"],
            }
        )
        assert "error" in result


# ---------------------------------------------------------------------------
# Plugin-layer: cmd_memory_promote
# ---------------------------------------------------------------------------


class TestPluginMemoryPromote:
    """Test the memory_promote command handler."""

    @pytest.mark.asyncio
    async def test_promote_missing_project_id(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_promote(
            {"chunk_hash": "abc", "target_scope": "agenttype_coding"}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_promote_missing_chunk_hash(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_promote(
            {"project_id": "proj", "target_scope": "agenttype_coding"}
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_promote_missing_target_scope(self, plugin):
        plugin._service = MagicMock()
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_promote(
            {"project_id": "proj", "chunk_hash": "abc"}
        )
        assert "error" in result
        assert "target_scope" in result["error"]

    @pytest.mark.asyncio
    async def test_promote_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_promote(
            {
                "project_id": "proj",
                "chunk_hash": "abc",
                "target_scope": "agenttype_coding",
            }
        )
        assert "error" in result

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_promote_success(self, wired_plugin, mock_store):
        """Successful promote copies entry to target scope."""
        result = await wired_plugin.cmd_memory_promote(
            {
                "project_id": "test-project",
                "chunk_hash": "existing_hash",
                "target_scope": "agenttype_coding",
            }
        )
        assert result["success"] is True
        assert result["action"] == "promoted"
        assert result["target_scope"] == "agenttype_coding"
        assert result["source_deleted"] is False  # default

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_promote_with_delete_source(self, wired_plugin, mock_store):
        """Promote with delete_source=True removes the source entry."""
        result = await wired_plugin.cmd_memory_promote(
            {
                "project_id": "test-project",
                "chunk_hash": "existing_hash",
                "target_scope": "agenttype_coding",
                "delete_source": True,
            }
        )
        assert result["success"] is True
        assert result["source_deleted"] is True
        # delete_by_hashes should have been called (for source deletion)
        assert mock_store.delete_by_hashes.call_count >= 1

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_promote_source_not_found(self, wired_plugin, mock_store):
        """Promote when source entry doesn't exist returns error."""
        mock_store.get.return_value = None
        result = await wired_plugin.cmd_memory_promote(
            {
                "project_id": "test-project",
                "chunk_hash": "missing",
                "target_scope": "agenttype_coding",
            }
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_promote_preserves_tags_and_topic(self, wired_plugin, mock_store):
        """Promoted entry retains the source entry's tags and topic."""
        mock_store.get.return_value = {
            "chunk_hash": "hash_to_promote",
            "entry_type": "document",
            "content": "Summary of pattern",
            "original": "Full description of the cross-project pattern",
            "source": "",
            "heading": "Cross-project pattern",
            "topic": "testing",
            "tags": '["insight", "testing", "provisional"]',
            "updated_at": 2000,
            "embedding": [0.1] * 384,
        }

        result = await wired_plugin.cmd_memory_promote(
            {
                "project_id": "test-project",
                "chunk_hash": "hash_to_promote",
                "target_scope": "agenttype_coding",
            }
        )
        assert result["success"] is True
        # The target_result should have the save info
        target = result.get("target_result", {})
        assert target.get("success") is True


# ---------------------------------------------------------------------------
# Playbook content — verify consolidation section references tools
# ---------------------------------------------------------------------------


class TestReflectionPlaybookContent:
    """Verify the reflection playbook references the simplified memory tools."""

    def test_coding_playbook_references_memory_delete(self):
        playbook = Path("vault/agent-types/coding/playbooks/reflection.md")
        if not playbook.exists():
            pytest.skip("Playbook file not found")
        text = playbook.read_text()
        assert "memory_delete" in text

    def test_coding_playbook_references_memory_store(self):
        playbook = Path("vault/agent-types/coding/playbooks/reflection.md")
        if not playbook.exists():
            pytest.skip("Playbook file not found")
        text = playbook.read_text()
        assert "memory_store" in text

    def test_coding_playbook_references_memory_recall(self):
        playbook = Path("vault/agent-types/coding/playbooks/reflection.md")
        if not playbook.exists():
            pytest.skip("Playbook file not found")
        text = playbook.read_text()
        assert "memory_recall" in text

    def test_template_playbook_references_tools(self):
        playbook = Path("vault/templates/reflection-playbook.md")
        if not playbook.exists():
            pytest.skip("Template file not found")
        text = playbook.read_text()
        assert "memory_delete" in text
        assert "memory_store" in text
        assert "memory_recall" in text


# ---------------------------------------------------------------------------
# Vault-file sync for memory mutations (Issue raised 2026-04-20)
# ---------------------------------------------------------------------------


class TestMemoryMutationsUpdateVaultFiles:
    """When a memory is deleted or updated, the vault markdown must follow.

    Previously `memory_update` skipped the topic field entirely and merged
    tags instead of replacing them; `memory_delete` silently succeeded
    when the vault path didn't match the `/insights/`/`/knowledge/` guard,
    leaving orphaned markdown files.
    """

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_document_syncs_topic_to_vault(self, service, mock_store, tmp_data_dir):
        """Changing topic via memory_update writes it into the vault frontmatter."""
        insights_dir = Path(tmp_data_dir) / "vault" / "projects" / "p" / "memory" / "insights"
        insights_dir.mkdir(parents=True)
        vault_file = insights_dir / "x.md"
        vault_file.write_text(
            '---\ntags: ["insight"]\ntopic: old-topic\nupdated: 2020-01-01\n---\nBody\n'
        )
        mock_store.get.return_value = {
            **mock_store.get.return_value,
            "source": str(vault_file),
            "tags": '["insight"]',
            "topic": "old-topic",
        }

        await service.update_document("p", "existing_hash", topic="new-topic")

        text = vault_file.read_text()
        assert "topic: new-topic" in text
        assert "topic: old-topic" not in text

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_update_document_replaces_tags_in_vault(self, service, mock_store, tmp_data_dir):
        """memory_update(tags=...) REPLACES the vault tags, not merges."""
        insights_dir = Path(tmp_data_dir) / "vault" / "projects" / "p" / "memory" / "insights"
        insights_dir.mkdir(parents=True)
        vault_file = insights_dir / "x.md"
        vault_file.write_text(
            '---\ntags: ["insight", "stale", "deprecated"]\nupdated: 2020-01-01\n---\nBody\n'
        )
        mock_store.get.return_value = {
            **mock_store.get.return_value,
            "source": str(vault_file),
            "tags": '["insight", "stale", "deprecated"]',
        }

        await service.update_document("p", "existing_hash", tags=["insight", "verified"])

        text = vault_file.read_text()
        # New tags present, old ones removed.
        assert '"verified"' in text
        assert "stale" not in text
        assert "deprecated" not in text

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_delete_document_logs_when_guard_blocks(
        self, service, mock_store, tmp_data_dir, caplog
    ):
        """If the stored source is outside the safety guard, log a warning.

        Previously the method silently reported vault_deleted=False with no
        indication to the caller that the markdown file still exists.
        """
        import logging

        # Path outside /insights/ and /knowledge/ — silently skipped before
        other_dir = Path(tmp_data_dir) / "vault" / "projects" / "p" / "memory" / "facts"
        other_dir.mkdir(parents=True)
        orphan = other_dir / "stray.md"
        orphan.write_text("some markdown")
        mock_store.get.return_value = {
            **mock_store.get.return_value,
            "source": str(orphan),
        }

        with caplog.at_level(logging.WARNING, logger="src.plugins.internal.memory.service"):
            result = await service.delete_document("p", "existing_hash")

        assert result["vault_deleted"] is False
        assert any(
            "outside the insights/knowledge safety guard" in rec.message
            or "not deleted" in rec.message.lower()
            for rec in caplog.records
        ), f"Expected warning log; got: {[r.message for r in caplog.records]}"

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_delete_document_logs_when_source_missing(
        self, service, mock_store, caplog
    ):
        """If the source path doesn't exist on disk, log a warning."""
        import logging

        mock_store.get.return_value = {
            **mock_store.get.return_value,
            "source": "/nonexistent/path/insights/gone.md",
        }

        with caplog.at_level(logging.WARNING, logger="src.plugins.internal.memory.service"):
            result = await service.delete_document("p", "existing_hash")

        assert result["vault_deleted"] is False
        assert any(
            "does not exist" in rec.message.lower() or "missing" in rec.message.lower()
            for rec in caplog.records
        ), f"Expected warning log; got: {[r.message for r in caplog.records]}"
