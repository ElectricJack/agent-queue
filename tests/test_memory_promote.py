"""Tests for cmd_memory_promote_to_knowledge.

The promote-to-knowledge command is the one-step tool the nightly
consolidation task calls to move a stable insight into the curated
``knowledge/`` subdirectory.  Exercise:

- Tool and command registration (discoverable to supervisor / MCP).
- Subdir parameter flows through service.save_document to
  ``memory/knowledge/``, not ``memory/insights/``.
- Tag normalization: drops ``insight``/``auto-extracted``, adds
  ``knowledge`` and ``curated``.
- Source insight is deleted after successful promotion.
- Content/topic overrides work when merging a cluster.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

if sys.platform == "win32":
    pytest.skip("Milvus Lite not supported on Windows", allow_module_level=True)

from src.plugins.internal.memory.service import MemoryService, MEMSEARCH_AVAILABLE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_embedder():
    emb = MagicMock()
    emb.model_name = "test-model"
    emb.dimension = 384
    emb.embed = AsyncMock(return_value=[[0.1] * 384])
    return emb


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.count.return_value = 10
    store.model_info = {"provider": "test", "model": "test-model", "dimension": 384}
    store.needs_reindex = False
    store.upsert.return_value = 1
    store.get.return_value = {
        "chunk_hash": "source_hash",
        "entry_type": "document",
        "content": (
            "Task titles created from allowlisted emails must follow the "
            "format 'SenderFirstName: ConciseSubject' and stay under 80 "
            "characters."
        ),
        "original": (
            "Task titles created from allowlisted emails must follow the "
            "format 'SenderFirstName: ConciseSubject' and stay under 80 "
            "characters."
        ),
        "source": "",
        "heading": "Task Title Formatting",
        "topic": "task-creation",
        "tags": '["insight", "auto-extracted", "task_creation"]',
        "updated_at": 1000,
        "embedding": [0.1] * 384,
    }
    store.search.return_value = []
    store.delete_by_hashes = MagicMock()
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
def tmp_data_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def service(mock_embedder, mock_router, tmp_data_dir):
    svc = MemoryService(
        milvus_uri="/tmp/test-promote.db",
        embedding_provider="openai",
        data_dir=tmp_data_dir,
    )
    svc._embedder = mock_embedder
    svc._router = mock_router
    svc._initialized = True
    return svc


@pytest.fixture
def wired_plugin(service):
    from src.plugins.internal.memory import MemoryPlugin

    plugin = MemoryPlugin()
    plugin._service = service
    plugin._log = MagicMock()
    plugin._ctx = MagicMock()
    plugin._ctx.invoke_llm = AsyncMock(return_value="LLM summary")
    plugin._ctx.active_project_id = None
    return plugin


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_defined(self):
        from src.plugins.internal.memory import TOOL_DEFINITIONS

        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "memory_promote_to_knowledge" in names

    def test_tool_requires_chunk_hash(self):
        from src.plugins.internal.memory import TOOL_DEFINITIONS

        defn = next(
            t for t in TOOL_DEFINITIONS if t["name"] == "memory_promote_to_knowledge"
        )
        assert "chunk_hash" in defn["input_schema"]["required"]

    def test_exposed_as_agent_tool(self):
        from src.plugins.internal.memory import AGENT_TOOLS

        assert "memory_promote_to_knowledge" in AGENT_TOOLS


# ---------------------------------------------------------------------------
# Service-layer: save_document writes to the requested subdir
# ---------------------------------------------------------------------------


class TestSaveDocumentSubdir:
    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_default_subdir_is_insights(self, service):
        result = await service.save_document(
            "proj-a",
            "An insight worth keeping that deserves its own vault home.",
            tags=["insight", "testing"],
        )
        assert "/insights/" in result["vault_path"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_knowledge_subdir(self, service):
        result = await service.save_document(
            "proj-a",
            "A curated fact elevated to the knowledge layer.",
            tags=["knowledge"],
            subdir="knowledge",
        )
        assert "/knowledge/" in result["vault_path"]
        assert "/insights/" not in result["vault_path"]

    def test_write_vault_file_honors_subdir(self, service, tmp_data_dir):
        """_write_vault_file writes into the requested subdir."""
        vault_dir = Path(tmp_data_dir) / "vault-root"
        filepath = service._write_vault_file(
            vault_dir,
            content="An entry under a custom subdir to keep tests honest.",
            tags=["knowledge"],
            subdir="knowledge",
        )
        assert filepath.parent.name == "knowledge"
        assert filepath.exists()


# ---------------------------------------------------------------------------
# Plugin-layer: cmd_memory_promote_to_knowledge
# ---------------------------------------------------------------------------


class TestPromoteToKnowledge:
    @pytest.mark.asyncio
    async def test_missing_chunk_hash(self, wired_plugin):
        result = await wired_plugin.cmd_memory_promote_to_knowledge(
            {"project_id": "proj-a"}
        )
        assert "error" in result
        assert "chunk_hash" in result["error"]

    @pytest.mark.asyncio
    async def test_missing_project_id(self, wired_plugin):
        result = await wired_plugin.cmd_memory_promote_to_knowledge(
            {"chunk_hash": "h1"}
        )
        assert "error" in result
        assert "project_id" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_happy_path_writes_to_knowledge_and_deletes_source(
        self, wired_plugin, mock_store
    ):
        result = await wired_plugin.cmd_memory_promote_to_knowledge(
            {"project_id": "proj-a", "chunk_hash": "source_hash"}
        )
        assert result.get("success") is True
        assert result["action"] == "promoted_to_knowledge"
        assert result["source_chunk_hash"] == "source_hash"
        assert result["source_deleted"] is True
        assert "/knowledge/" in result["knowledge_vault_path"]
        # Milvus delete called for the source.
        mock_store.delete_by_hashes.assert_called_once_with(["source_hash"])

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_tags_normalized(self, wired_plugin):
        result = await wired_plugin.cmd_memory_promote_to_knowledge(
            {"project_id": "proj-a", "chunk_hash": "source_hash"}
        )
        tags = result["tags"]
        assert "knowledge" in tags
        assert "curated" in tags
        # Insight-era markers stripped.
        assert "insight" not in tags
        assert "auto-extracted" not in tags
        # Original domain tag preserved.
        assert "task_creation" in tags

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_content_override_applied(self, wired_plugin, tmp_data_dir):
        rewritten = (
            "Canonical merged fact about allowlisted email task titles: "
            "the format is 'SenderFirstName: ConciseSubject' under 80 chars."
        )
        result = await wired_plugin.cmd_memory_promote_to_knowledge(
            {
                "project_id": "proj-a",
                "chunk_hash": "source_hash",
                "content": rewritten,
                "topic": "email-task-creation",
            }
        )
        assert result.get("success") is True
        vault_path = Path(result["knowledge_vault_path"])
        assert vault_path.exists()
        text = vault_path.read_text()
        assert rewritten in text
        assert "topic: email-task-creation" in text

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_source_not_found(self, wired_plugin, mock_store):
        mock_store.get.return_value = None
        result = await wired_plugin.cmd_memory_promote_to_knowledge(
            {"project_id": "proj-a", "chunk_hash": "missing_hash"}
        )
        assert "error" in result
        assert "not found" in result["error"].lower()
