"""Tests for contradiction detection in contested memories (spec §7 Q2).

Covers:
- ``_detect_contradiction`` classifies contradictory vs compatible memories
- ``_handle_dedup_merge`` routes to ``_handle_contradiction`` when contradiction found
- ``_handle_contradiction`` tags both existing and new memory as ``#contested``
- ``_handle_contradiction`` creates new entry (does not merge)
- Non-contradictory similar memories still merge normally
- Fallback to ``False`` (compatible) when LLM is unavailable
- ``memory_stats`` includes ``contested_memories`` count
- ``count_by_tag`` counts entries by tag in service layer
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

if sys.platform == "win32":
    pytest.skip("Milvus Lite not supported on Windows", allow_module_level=True)

from src.plugins.internal.memory.service import MemoryService


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
    svc = MemoryService(
        milvus_uri="/tmp/test.db",
        embedding_provider="openai",
        data_dir=str(tmp_path),
    )
    svc._embedder = mock_embedder
    svc._router = mock_router
    svc._initialized = True
    return svc


# ---------------------------------------------------------------------------
# Service: count_by_tag
# ---------------------------------------------------------------------------


class TestCountByTag:
    """Tests for MemoryService.count_by_tag."""

    async def test_count_by_tag_returns_count(self, service, mock_store):
        """count_by_tag returns the number of entries matching a tag."""
        # Simulate 3 entries with the 'contested' tag
        mock_store.query.return_value = [
            {"chunk_hash": "a", "tags": '["insight", "contested"]'},
            {"chunk_hash": "b", "tags": '["contested"]'},
            {"chunk_hash": "c", "tags": '["auto-generated", "contested"]'},
        ]
        count = await service.count_by_tag("test-project", "contested")
        assert count == 3

    async def test_count_by_tag_returns_zero_when_no_matches(self, service, mock_store):
        """count_by_tag returns 0 when no entries match."""
        mock_store.query.return_value = []
        count = await service.count_by_tag("test-project", "contested")
        assert count == 0

    async def test_count_by_tag_returns_zero_when_unavailable(self, service):
        """count_by_tag returns 0 when service is not available."""
        service._initialized = False
        count = await service.count_by_tag("test-project", "contested")
        assert count == 0

    async def test_count_by_tag_handles_query_exception(self, service, mock_store):
        """count_by_tag returns 0 on query failure."""
        mock_store.query.side_effect = RuntimeError("query failed")
        count = await service.count_by_tag("test-project", "contested")
        assert count == 0

    async def test_count_by_tag_with_explicit_scope(self, service, mock_store, mock_router):
        """count_by_tag respects explicit scope parameter."""
        mock_store.query.return_value = [{"chunk_hash": "a"}]
        count = await service.count_by_tag("test-project", "contested", scope="system")
        assert count == 1
        # Verify get_store was called — scope resolution happened
        mock_router.get_store.assert_called()


# ---------------------------------------------------------------------------
# Service: stats includes contested_memories
# ---------------------------------------------------------------------------


class TestStatsContestedCount:
    """Tests that memory_stats includes contested_memories count."""

    async def test_stats_includes_contested_memories(self, service, mock_store):
        """stats() includes contested_memories in the response."""
        # Regular query returns for doc/kv/temporal
        mock_store.query.return_value = []
        mock_store.count.return_value = 10

        stats = await service.stats("test-project")
        assert "contested_memories" in stats
        assert isinstance(stats["contested_memories"], int)

    async def test_stats_contested_count_reflects_tagged_entries(self, service, mock_store):
        """stats() contested_memories reflects actual tagged entries."""
        call_count = 0

        def query_side_effect(*, filter_expr="", **_kwargs):
            nonlocal call_count
            call_count += 1
            # The 4th call is for contested tag count (after doc, kv, temporal)
            if "contested" in filter_expr:
                return [{"chunk_hash": "a"}, {"chunk_hash": "b"}]
            return []

        mock_store.query.side_effect = query_side_effect
        mock_store.count.return_value = 5

        stats = await service.stats("test-project")
        assert stats["contested_memories"] == 2


# ---------------------------------------------------------------------------
# Plugin: _detect_contradiction
# ---------------------------------------------------------------------------


class TestDetectContradiction:
    """Tests for MemoryPlugin._detect_contradiction."""

    @pytest.fixture
    def plugin(self):
        """Create a MemoryPlugin instance with mocked context."""
        from src.plugins.internal.memory import MemoryPlugin

        plugin = MemoryPlugin.__new__(MemoryPlugin)
        plugin._ctx = MagicMock()
        plugin._ctx.invoke_llm = AsyncMock()
        plugin._log = MagicMock()
        plugin._service = MagicMock()
        return plugin

    async def test_detects_contradiction(self, plugin):
        """Returns True when LLM says CONTRADICTION."""
        plugin._ctx.invoke_llm = AsyncMock(return_value="CONTRADICTION")

        result = await plugin._detect_contradiction(
            "Use SQLite for all tests.",
            "Never use SQLite for tests, use PostgreSQL instead.",
        )
        assert result is True

    async def test_detects_compatible(self, plugin):
        """Returns False when LLM says COMPATIBLE."""
        plugin._ctx.invoke_llm = AsyncMock(return_value="COMPATIBLE")

        result = await plugin._detect_contradiction(
            "Use SQLite for all tests.",
            "SQLite tests should use in-memory databases for speed.",
        )
        assert result is False

    async def test_returns_false_on_llm_failure(self, plugin):
        """Falls back to False (compatible) when LLM is unavailable."""
        plugin._ctx.invoke_llm = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        result = await plugin._detect_contradiction("Use approach A.", "Use approach B.")
        assert result is False

    async def test_handles_extra_whitespace_in_response(self, plugin):
        """Handles whitespace and casing in LLM response."""
        plugin._ctx.invoke_llm = AsyncMock(return_value="  contradiction  \n")

        result = await plugin._detect_contradiction("foo", "bar")
        assert result is True

    async def test_handles_verbose_llm_response(self, plugin):
        """Handles verbose response containing CONTRADICTION keyword."""
        plugin._ctx.invoke_llm = AsyncMock(
            return_value="These are a CONTRADICTION because they disagree."
        )

        result = await plugin._detect_contradiction("foo", "bar")
        assert result is True

    async def test_prompt_includes_both_contents(self, plugin):
        """The LLM prompt includes both memory contents."""
        plugin._ctx.invoke_llm = AsyncMock(return_value="COMPATIBLE")

        await plugin._detect_contradiction("Existing content here.", "New content here.")

        # Verify the LLM was called with a prompt containing both
        call_args = plugin._ctx.invoke_llm.call_args
        prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
        assert "Existing content here." in prompt
        assert "New content here." in prompt

    async def test_truncates_long_content(self, plugin):
        """Long content is truncated to 1500 chars in the prompt."""
        plugin._ctx.invoke_llm = AsyncMock(return_value="COMPATIBLE")
        long_content = "x" * 3000

        await plugin._detect_contradiction(long_content, long_content)

        call_args = plugin._ctx.invoke_llm.call_args
        prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
        # Each content block should be truncated to 1500 chars
        # The full 3000-char content should NOT appear in the prompt
        assert "x" * 3000 not in prompt


# ---------------------------------------------------------------------------
# Plugin: _handle_contradiction
# ---------------------------------------------------------------------------


class TestHandleContradiction:
    """Tests for MemoryPlugin._handle_contradiction."""

    @pytest.fixture
    def plugin(self):
        """Create a MemoryPlugin instance with mocked service."""
        from src.plugins.internal.memory import MemoryPlugin

        plugin = MemoryPlugin.__new__(MemoryPlugin)
        plugin._ctx = MagicMock()
        plugin._ctx.invoke_llm = AsyncMock(return_value="short summary")
        plugin._log = MagicMock()

        # Mock service
        svc = MagicMock()
        svc.available = True
        svc.update_document_content = AsyncMock(
            return_value={
                "chunk_hash": "existing-hash",
                "vault_path": "/tmp/existing.md",
                "collection": "aq_project_test",
                "scope": "project",
                "scope_id": "test",
                "updated_at": 1000,
            }
        )
        svc.save_document = AsyncMock(
            return_value={
                "chunk_hash": "new-hash",
                "vault_path": "/tmp/new.md",
                "collection": "aq_project_test",
                "scope": "project",
                "scope_id": "test",
                "topic": "testing",
                "tags": ["insight", "contested"],
                "source_task": "task-1",
                "source_playbook": "",
                "updated_at": 2000,
            }
        )
        plugin._service = svc
        return plugin

    async def test_tags_existing_memory_as_contested(self, plugin):
        """The existing memory gets tagged with #contested."""
        existing = {
            "chunk_hash": "existing-hash",
            "content": "Use SQLite for all tests.",
            "tags": '["insight", "auto-generated"]',
        }

        await plugin._handle_contradiction(
            project_id="test-project",
            content="Never use SQLite for tests.",
            existing=existing,
            similarity=0.88,
            tags=["insight"],
            topic="testing",
            source_task="task-1",
            source_playbook=None,
            scope=None,
        )

        # Verify update_document_content was called with contested tag
        plugin._service.update_document_content.assert_called_once()
        call_kwargs = plugin._service.update_document_content.call_args
        # The tags should include 'contested'
        tags_arg = call_kwargs.kwargs.get("tags") or call_kwargs[1].get("tags", [])
        assert "contested" in tags_arg

    async def test_creates_new_entry_with_contested_tag(self, plugin):
        """The new memory is created as a separate entry with #contested."""
        existing = {
            "chunk_hash": "existing-hash",
            "content": "Use SQLite for all tests.",
            "tags": '["insight"]',
        }

        await plugin._handle_contradiction(
            project_id="test-project",
            content="Never use SQLite for tests.",
            existing=existing,
            similarity=0.88,
            tags=["insight"],
            topic="testing",
            source_task="task-1",
            source_playbook=None,
            scope=None,
        )

        # The new entry should be created via save_document (through _handle_create_new)
        plugin._service.save_document.assert_called_once()

    async def test_result_action_is_contested(self, plugin):
        """Result dict has action='contested' and contradiction=True."""
        existing = {
            "chunk_hash": "existing-hash",
            "content": "Old content.",
            "tags": '["insight"]',
        }

        result = await plugin._handle_contradiction(
            project_id="test-project",
            content="New contradicting content.",
            existing=existing,
            similarity=0.88,
            tags=["insight"],
            topic=None,
            source_task="task-1",
            source_playbook=None,
            scope=None,
        )

        assert result["action"] == "contested"
        assert result["contradiction"] is True
        assert result["contested_with"] == "existing-hash"
        assert result["similarity_score"] == 0.88

    async def test_result_includes_existing_preview(self, plugin):
        """Result includes a preview of the existing content."""
        existing = {
            "chunk_hash": "existing-hash",
            "content": "Existing memory content that is being contradicted.",
            "tags": '["insight"]',
        }

        result = await plugin._handle_contradiction(
            project_id="test-project",
            content="New content.",
            existing=existing,
            similarity=0.85,
            tags=["insight"],
            topic=None,
            source_task=None,
            source_playbook=None,
            scope=None,
        )

        assert "existing_content_preview" in result
        assert "Existing memory content" in result["existing_content_preview"]

    async def test_existing_already_contested_not_duplicated(self, plugin):
        """If existing memory already has #contested tag, don't duplicate it."""
        existing = {
            "chunk_hash": "existing-hash",
            "content": "Old content.",
            "tags": '["insight", "contested"]',
        }

        await plugin._handle_contradiction(
            project_id="test-project",
            content="New content.",
            existing=existing,
            similarity=0.88,
            tags=["insight"],
            topic=None,
            source_task=None,
            source_playbook=None,
            scope=None,
        )

        # When existing already has #contested, update_document_content
        # should NOT be called (skip re-tagging)
        plugin._service.update_document_content.assert_not_called()

    async def test_existing_no_chunk_hash_skips_tag_update(self, plugin):
        """When existing has no chunk_hash, skip tagging it (graceful)."""
        existing = {
            "chunk_hash": "",
            "content": "Old content.",
            "tags": '["insight"]',
        }

        result = await plugin._handle_contradiction(
            project_id="test-project",
            content="New content.",
            existing=existing,
            similarity=0.88,
            tags=["insight"],
            topic=None,
            source_task=None,
            source_playbook=None,
            scope=None,
        )

        # Should not attempt to update an entry without a chunk_hash
        plugin._service.update_document_content.assert_not_called()
        # But should still create the new contested entry
        assert result["action"] == "contested"

    async def test_tag_update_failure_still_creates_new(self, plugin):
        """If tagging existing fails, new entry is still created."""
        plugin._service.update_document_content = AsyncMock(
            side_effect=RuntimeError("update failed")
        )

        existing = {
            "chunk_hash": "existing-hash",
            "content": "Old content.",
            "tags": '["insight"]',
        }

        result = await plugin._handle_contradiction(
            project_id="test-project",
            content="New content.",
            existing=existing,
            similarity=0.88,
            tags=["insight"],
            topic=None,
            source_task=None,
            source_playbook=None,
            scope=None,
        )

        # The new entry should still be created even if tagging failed
        assert result["action"] == "contested"
        plugin._service.save_document.assert_called_once()


# ---------------------------------------------------------------------------
# Plugin: _handle_dedup_merge routes to contradiction handler
# ---------------------------------------------------------------------------


class TestDedupMergeContradictionRouting:
    """Tests that _handle_dedup_merge routes contradictions correctly."""

    @pytest.fixture
    def plugin(self):
        """Create a MemoryPlugin instance with mocked deps."""
        from src.plugins.internal.memory import MemoryPlugin

        plugin = MemoryPlugin.__new__(MemoryPlugin)
        plugin._ctx = MagicMock()
        plugin._log = MagicMock()

        # Mock service
        svc = MagicMock()
        svc.available = True
        svc.update_document_content = AsyncMock(
            return_value={
                "chunk_hash": "hash",
                "vault_path": "/tmp/test.md",
                "collection": "aq_project_test",
                "scope": "project",
                "scope_id": "test",
                "updated_at": 1000,
            }
        )
        svc.save_document = AsyncMock(
            return_value={
                "chunk_hash": "new-hash",
                "vault_path": "/tmp/new.md",
                "collection": "aq_project_test",
                "scope": "project",
                "scope_id": "test",
                "topic": "",
                "tags": ["insight", "contested"],
                "source_task": "",
                "source_playbook": "",
                "updated_at": 2000,
            }
        )
        plugin._service = svc
        return plugin

    async def test_contradiction_routes_to_handle_contradiction(self, plugin):
        """When contradiction detected, dedup_merge routes to _handle_contradiction."""
        # Mock: detect contradiction = True
        plugin._detect_contradiction = AsyncMock(return_value=True)

        existing = {
            "chunk_hash": "existing-hash",
            "content": "Use approach A.",
            "tags": '["insight"]',
        }

        result = await plugin._handle_dedup_merge(
            project_id="test-project",
            content="Never use approach A.",
            existing=existing,
            similarity=0.88,
            tags=["insight"],
            topic=None,
            source_task="task-1",
            source_playbook=None,
            scope=None,
        )

        assert result["action"] == "contested"
        assert result["contradiction"] is True

    async def test_no_contradiction_proceeds_with_merge(self, plugin):
        """When no contradiction, dedup_merge proceeds with normal merge."""
        # Mock: detect contradiction = False
        plugin._detect_contradiction = AsyncMock(return_value=False)
        plugin._merge_via_llm = AsyncMock(return_value="Merged content.")

        existing = {
            "chunk_hash": "existing-hash",
            "content": "Approach A is good for X.",
            "tags": '["insight"]',
        }

        result = await plugin._handle_dedup_merge(
            project_id="test-project",
            content="Approach A is also good for Y.",
            existing=existing,
            similarity=0.88,
            tags=["insight"],
            topic=None,
            source_task="task-1",
            source_playbook=None,
            scope=None,
        )

        assert result["action"] == "merged"
        assert "contradiction" not in result


# ---------------------------------------------------------------------------
# Integration: full _do_memory_save flow with contradiction
# ---------------------------------------------------------------------------


class TestMemorySaveContradictionFlow:
    """Integration tests for the full save flow with contradiction detection."""

    @pytest.fixture
    def plugin(self):
        """Create a fully-wired MemoryPlugin with mocked service."""
        from src.plugins.internal.memory import MemoryPlugin

        plugin = MemoryPlugin.__new__(MemoryPlugin)
        plugin._ctx = MagicMock()
        plugin._log = MagicMock()

        # Mock service with search returning a similar result
        svc = MagicMock()
        svc.available = True
        svc.search = AsyncMock(
            return_value=[
                {
                    "chunk_hash": "existing-hash",
                    "content": "The default timeout is 30 seconds.",
                    "entry_type": "document",
                    "score": 0.88,
                    "tags": '["insight"]',
                    "source": "/tmp/vault/existing.md",
                }
            ]
        )
        svc.update_document_content = AsyncMock(
            return_value={
                "chunk_hash": "existing-hash",
                "vault_path": "/tmp/vault/existing.md",
                "collection": "aq_project_test",
                "scope": "project",
                "scope_id": "test",
                "updated_at": 1000,
            }
        )
        svc.save_document = AsyncMock(
            return_value={
                "chunk_hash": "new-hash",
                "vault_path": "/tmp/vault/new.md",
                "collection": "aq_project_test",
                "scope": "project",
                "scope_id": "test",
                "topic": "",
                "tags": ["insight", "contested"],
                "source_task": "task-1",
                "source_playbook": "",
                "updated_at": 2000,
            }
        )
        plugin._service = svc
        return plugin

    async def test_save_with_contradiction_creates_contested_entry(self, plugin):
        """Full save flow detects contradiction and creates contested entry."""
        # Mock the contradiction detection to return True
        plugin._detect_contradiction = AsyncMock(return_value=True)
        # Skip topic inference
        plugin._infer_topic = AsyncMock(return_value=None)

        result = await plugin._do_memory_save(
            project_id="test-project",
            content="The default timeout is 60 seconds.",
            tags=["insight"],
            topic=None,
            source_task="task-1",
            source_playbook=None,
            scope=None,
        )

        assert result["success"] is True
        assert result["action"] == "contested"
        assert result["contradiction"] is True

    async def test_save_without_contradiction_merges(self, plugin):
        """Full save flow with no contradiction performs normal merge."""
        plugin._detect_contradiction = AsyncMock(return_value=False)
        plugin._merge_via_llm = AsyncMock(return_value="Merged content.")
        plugin._infer_topic = AsyncMock(return_value=None)

        result = await plugin._do_memory_save(
            project_id="test-project",
            content="The default timeout can be configured.",
            tags=["insight"],
            topic=None,
            source_task="task-1",
            source_playbook=None,
            scope=None,
        )

        assert result["success"] is True
        assert result["action"] == "merged"
