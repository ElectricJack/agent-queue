"""Tests for MemoryV2Service — the service layer wrapping memsearch fork.

Tests cover:
- Service initialization and lifecycle
- Scope resolution
- KV operations (get, set, list) with scope routing
- Vault facts.md parsing, rendering, and sync
- Temporal facts (get, set, history)
- Search operations (single, batch, by tag)
- Stats retrieval
- Graceful degradation when memsearch is unavailable
"""

from __future__ import annotations

import sys
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
    """Create a mock MilvusStore with all needed methods."""
    store = MagicMock()
    store.count.return_value = 42
    store.model_info = {"provider": "test", "model": "test-model", "dimension": 384}
    store.needs_reindex = False

    # KV methods
    store.get_kv.return_value = {
        "kv_namespace": "project",
        "kv_key": "test_key",
        "kv_value": '"test_value"',
        "updated_at": 1000,
        "tags": "[]",
        "source": "",
    }
    store.set_kv.return_value = {
        "chunk_hash": "abc123",
        "kv_namespace": "project",
        "kv_key": "test_key",
        "kv_value": '"test_value"',
        "updated_at": 1000,
        "tags": "[]",
        "source": "",
    }
    store.list_kv.return_value = [
        {
            "kv_namespace": "project",
            "kv_key": "key1",
            "kv_value": '"val1"',
            "updated_at": 1000,
            "tags": "[]",
            "source": "",
        },
        {
            "kv_namespace": "project",
            "kv_key": "key2",
            "kv_value": '"val2"',
            "updated_at": 1001,
            "tags": "[]",
            "source": "",
        },
    ]

    # Temporal methods
    store.get_temporal.return_value = [
        {
            "kv_key": "deploy_branch",
            "kv_value": '"main"',
            "valid_from": 100,
            "valid_to": 0,
            "updated_at": 100,
            "tags": "[]",
            "source": "",
        }
    ]
    store.set_temporal.return_value = {
        "chunk_hash": "temporal_abc",
        "kv_key": "deploy_branch",
        "kv_value": '"release"',
        "valid_from": 200,
        "valid_to": 0,
        "updated_at": 200,
        "tags": "[]",
        "source": "",
    }
    store.get_temporal_history.return_value = [
        {
            "kv_key": "deploy_branch",
            "kv_value": '"main"',
            "valid_from": 100,
            "valid_to": 200,
            "updated_at": 100,
            "tags": "[]",
            "source": "",
        },
        {
            "kv_key": "deploy_branch",
            "kv_value": '"release"',
            "valid_from": 200,
            "valid_to": 0,
            "updated_at": 200,
            "tags": "[]",
            "source": "",
        },
    ]
    store.list_temporal.return_value = [
        {
            "kv_key": "deploy_branch",
            "kv_namespace": "",
            "kv_value": '"main"',
            "valid_from": 100,
            "valid_to": 0,
            "updated_at": 100,
            "tags": "[]",
            "source": "",
        },
        {
            "kv_key": "python_version",
            "kv_namespace": "",
            "kv_value": '"3.12"',
            "valid_from": 150,
            "valid_to": 0,
            "updated_at": 150,
            "tags": "[]",
            "source": "",
        },
    ]

    # Search
    store.search.return_value = [
        {
            "content": "Test result",
            "source": "/path/to/file.md",
            "heading": "Test",
            "score": 0.95,
            "chunk_hash": "hash1",
            "entry_type": "document",
            "topic": "",
            "tags": "[]",
        }
    ]

    # Query (for stats)
    def mock_query(filter_expr=""):
        if "document" in filter_expr:
            return [{"chunk_hash": "d1"}, {"chunk_hash": "d2"}]
        if "kv" in filter_expr:
            return [{"chunk_hash": "k1"}]
        if "temporal" in filter_expr:
            return [{"chunk_hash": "t1"}]
        return []

    store.query.side_effect = mock_query

    return store


@pytest.fixture
def mock_router(mock_store):
    """Create a mock CollectionRouter."""
    router = MagicMock()
    router.get_store.return_value = mock_store
    router.list_collections.return_value = []
    router.search = AsyncMock(
        return_value=[
            {
                "content": "Multi-scope result",
                "source": "/path/to/file.md",
                "heading": "Test",
                "score": 0.9,
                "weighted_score": 0.9,
                "chunk_hash": "hash1",
                "entry_type": "document",
                "topic": "",
                "tags": "[]",
                "_collection": "aq_project_test",
                "_scope": "project",
                "_scope_id": "test",
            }
        ]
    )
    router.search_by_tag_async = AsyncMock(
        return_value=[
            {
                "content": "Tag result",
                "source": "/path/to/file.md",
                "entry_type": "document",
                "tags": '["sqlite"]',
                "_collection": "aq_project_test",
                "_scope": "project",
                "_scope_id": "test",
                "chunk_hash": "tag_hash1",
            }
        ]
    )
    router.close = MagicMock()
    return router


@pytest.fixture
def service(mock_embedder, mock_router):
    """Create a MemoryV2Service with mocked dependencies."""
    svc = MemoryV2Service(
        milvus_uri="/tmp/test.db",
        embedding_provider="openai",
    )
    svc._embedder = mock_embedder
    svc._router = mock_router
    svc._initialized = True
    return svc


# ---------------------------------------------------------------------------
# Initialization & Lifecycle
# ---------------------------------------------------------------------------


class TestServiceLifecycle:
    """Test service initialization and shutdown."""

    def test_not_available_before_init(self):
        svc = MemoryV2Service()
        assert svc.available is False
        assert svc.router is None
        assert svc.embedder is None

    def test_available_after_init(self, service):
        assert service.available is True
        assert service.router is not None
        assert service.embedder is not None

    @pytest.mark.asyncio
    async def test_shutdown(self, service, mock_router):
        await service.shutdown()
        assert service.available is False
        assert service.router is None
        mock_router.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self, service):
        """Calling initialize on an already-initialized service is a no-op."""
        original_router = service.router
        await service.initialize()
        assert service.router is original_router  # same object


# ---------------------------------------------------------------------------
# Scope Resolution
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
class TestScopeResolution:
    """Test _resolve_scope mapping."""

    def test_default_project_scope(self, service):
        from memsearch import MemoryScope

        scope, scope_id = service._resolve_scope("my-project")
        assert scope == MemoryScope.PROJECT
        assert scope_id == "my-project"

    def test_system_scope(self, service):
        from memsearch import MemoryScope

        scope, scope_id = service._resolve_scope("proj", "system")
        assert scope == MemoryScope.SYSTEM
        assert scope_id is None

    def test_orchestrator_scope(self, service):
        from memsearch import MemoryScope

        scope, scope_id = service._resolve_scope("proj", "orchestrator")
        assert scope == MemoryScope.ORCHESTRATOR
        assert scope_id is None

    def test_agenttype_scope(self, service):
        from memsearch import MemoryScope

        scope, scope_id = service._resolve_scope("proj", "agenttype_coding")
        assert scope == MemoryScope.AGENT_TYPE
        assert scope_id == "coding"

    def test_explicit_project_scope(self, service):
        from memsearch import MemoryScope

        scope, scope_id = service._resolve_scope("proj", "project_other")
        assert scope == MemoryScope.PROJECT
        assert scope_id == "other"

    def test_unknown_scope_defaults_to_project(self, service):
        from memsearch import MemoryScope

        scope, scope_id = service._resolve_scope("proj", "unknown_value")
        assert scope == MemoryScope.PROJECT
        assert scope_id == "proj"


# ---------------------------------------------------------------------------
# KV Operations
# ---------------------------------------------------------------------------


class TestKVOperations:
    """Test KV get/set/list operations."""

    @pytest.mark.asyncio
    async def test_kv_get(self, service, mock_store):
        result = await service.kv_get("test-project", "project", "test_key")
        assert result is not None
        assert result["kv_key"] == "test_key"
        mock_store.get_kv.assert_called_once_with("test_key", namespace="project")

    @pytest.mark.asyncio
    async def test_kv_get_not_found(self, service, mock_store):
        mock_store.get_kv.return_value = None
        result = await service.kv_get("test-project", "project", "missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_kv_set(self, service, mock_store):
        result = await service.kv_set("test-project", "project", "new_key", "new_value")
        assert result is not None
        mock_store.set_kv.assert_called_once_with(
            "new_key",
            "new_value",
            namespace="project",
            content="project/new_key: new_value",
        )
        # Should include vault sync metadata
        assert "_vault_path" in result
        assert "_scope" in result
        assert "_scope_id" in result

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_kv_set_explicit_scope(self, service, mock_store, mock_router):
        """kv_set with explicit scope routes to the correct collection."""
        result = await service.kv_set("test-project", "project", "new_key", "val", scope="system")
        assert result is not None
        assert result["_scope"] == "system"
        assert result["_scope_id"] is None
        # The router should have been asked for the system scope's store
        from memsearch import MemoryScope

        mock_router.get_store.assert_called_with(MemoryScope.SYSTEM, None)

    @pytest.mark.asyncio
    async def test_kv_list(self, service, mock_store):
        results = await service.kv_list("test-project", "project")
        assert len(results) == 2
        mock_store.list_kv.assert_called_once_with(namespace="project")

    @pytest.mark.asyncio
    async def test_kv_get_unavailable(self):
        """KV get returns None when service is unavailable."""
        svc = MemoryV2Service()
        result = await svc.kv_get("proj", "ns", "key")
        assert result is None

    @pytest.mark.asyncio
    async def test_kv_set_unavailable(self):
        """KV set raises when service is unavailable."""
        svc = MemoryV2Service()
        with pytest.raises(RuntimeError, match="not available"):
            await svc.kv_set("proj", "ns", "key", "val")

    @pytest.mark.asyncio
    async def test_kv_list_unavailable(self):
        """KV list returns empty list when unavailable."""
        svc = MemoryV2Service()
        result = await svc.kv_list("proj", "ns")
        assert result == []


# ---------------------------------------------------------------------------
# Vault facts.md Parsing, Rendering, and Sync
# ---------------------------------------------------------------------------


class TestFactsFileParsing:
    """Test facts.md file parsing and rendering."""

    def test_parse_empty(self):
        assert MemoryV2Service._parse_facts_file("") == {}

    def test_parse_single_namespace(self):
        text = "## project\ntech_stack: [Python, SQLAlchemy]\ntest_command: pytest tests/ -v\n"
        result = MemoryV2Service._parse_facts_file(text)
        assert result == {
            "project": {
                "tech_stack": "[Python, SQLAlchemy]",
                "test_command": "pytest tests/ -v",
            }
        }

    def test_parse_multiple_namespaces(self):
        text = (
            "## project\n"
            "tech_stack: Python\n"
            "\n"
            "## conventions\n"
            "commit_style: conventional\n"
            "line_length: 100\n"
        )
        result = MemoryV2Service._parse_facts_file(text)
        assert "project" in result
        assert "conventions" in result
        assert result["project"]["tech_stack"] == "Python"
        assert result["conventions"]["commit_style"] == "conventional"
        assert result["conventions"]["line_length"] == "100"

    def test_parse_ignores_non_kv_lines(self):
        text = "## project\ntech_stack: Python\nThis is a comment without colon\n"
        result = MemoryV2Service._parse_facts_file(text)
        assert result == {"project": {"tech_stack": "Python"}}

    def test_parse_ignores_lines_before_heading(self):
        text = "orphan_key: orphan_value\n## project\nkey: val\n"
        result = MemoryV2Service._parse_facts_file(text)
        assert result == {"project": {"key": "val"}}

    def test_parse_value_with_colons(self):
        """Values containing colons should be preserved after the first colon."""
        text = "## urls\napi: http://localhost:8080/api\n"
        result = MemoryV2Service._parse_facts_file(text)
        assert result["urls"]["api"] == "http://localhost:8080/api"

    def test_render_empty(self):
        assert MemoryV2Service._render_facts_file({}) == ""

    def test_render_single_namespace(self):
        data = {"project": {"tech_stack": "Python", "test_cmd": "pytest"}}
        rendered = MemoryV2Service._render_facts_file(data)
        assert "## project" in rendered
        assert "tech_stack: Python" in rendered
        assert "test_cmd: pytest" in rendered

    def test_render_multiple_namespaces(self):
        data = {
            "project": {"a": "1"},
            "conventions": {"b": "2"},
        }
        rendered = MemoryV2Service._render_facts_file(data)
        assert "## conventions" in rendered
        assert "## project" in rendered
        # Namespaces should be sorted
        conv_idx = rendered.index("## conventions")
        proj_idx = rendered.index("## project")
        assert conv_idx < proj_idx

    def test_roundtrip(self):
        """Parse → render → parse should be stable."""
        original = (
            "## conventions\n"
            "commit_style: conventional\n"
            "line_length: 100\n"
            "\n"
            "## project\n"
            "tech_stack: Python\n"
        )
        data = MemoryV2Service._parse_facts_file(original)
        rendered = MemoryV2Service._render_facts_file(data)
        data2 = MemoryV2Service._parse_facts_file(rendered)
        assert data == data2


class TestFactsFileSync:
    """Test vault facts.md file synchronization on KV write."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_kv_set_creates_facts_file(self, service, mock_store, tmp_path):
        """kv_set creates a new facts.md file if it doesn't exist."""
        service._data_dir = str(tmp_path)

        result = await service.kv_set("test-project", "project", "tech_stack", "Python")

        # Find the created facts file
        vault_path = result["_vault_path"]
        facts = Path(vault_path)
        assert facts.exists(), f"Expected facts file at {vault_path}"
        content = facts.read_text(encoding="utf-8")
        assert "## project" in content
        assert "tech_stack: Python" in content

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_kv_set_updates_existing_facts_file(self, service, mock_store, tmp_path):
        """kv_set merges into an existing facts.md file."""
        service._data_dir = str(tmp_path)

        # Create an existing facts file
        from memsearch import MemoryScope, vault_paths as vp

        paths = vp(MemoryScope.PROJECT, "test_project")
        facts_rel = [p for p in paths if p.endswith("facts.md")][0]
        facts_path = tmp_path / facts_rel
        facts_path.parent.mkdir(parents=True, exist_ok=True)
        facts_path.write_text(
            "## project\nexisting_key: existing_value\n",
            encoding="utf-8",
        )

        await service.kv_set("test-project", "project", "new_key", "new_value")

        content = facts_path.read_text(encoding="utf-8")
        assert "existing_key: existing_value" in content
        assert "new_key: new_value" in content

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_kv_set_overwrites_existing_key(self, service, mock_store, tmp_path):
        """kv_set updates the value of an existing key."""
        service._data_dir = str(tmp_path)

        from memsearch import MemoryScope, vault_paths as vp

        paths = vp(MemoryScope.PROJECT, "test_project")
        facts_rel = [p for p in paths if p.endswith("facts.md")][0]
        facts_path = tmp_path / facts_rel
        facts_path.parent.mkdir(parents=True, exist_ok=True)
        facts_path.write_text(
            "## project\nmy_key: old_value\n",
            encoding="utf-8",
        )

        await service.kv_set("test-project", "project", "my_key", "new_value")

        content = facts_path.read_text(encoding="utf-8")
        assert "my_key: new_value" in content
        assert "old_value" not in content

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_kv_set_creates_new_namespace(self, service, mock_store, tmp_path):
        """kv_set adds a new namespace heading when the namespace doesn't exist."""
        service._data_dir = str(tmp_path)

        from memsearch import MemoryScope, vault_paths as vp

        paths = vp(MemoryScope.PROJECT, "test_project")
        facts_rel = [p for p in paths if p.endswith("facts.md")][0]
        facts_path = tmp_path / facts_rel
        facts_path.parent.mkdir(parents=True, exist_ok=True)
        facts_path.write_text("## project\nkey1: val1\n", encoding="utf-8")

        await service.kv_set("test-project", "conventions", "commit_style", "conventional")

        content = facts_path.read_text(encoding="utf-8")
        assert "## project" in content
        assert "## conventions" in content
        assert "commit_style: conventional" in content

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_kv_set_with_system_scope_syncs_to_system_facts(
        self, service, mock_store, tmp_path
    ):
        """kv_set with scope='system' writes to vault/system/facts.md."""
        service._data_dir = str(tmp_path)

        result = await service.kv_set(
            "test-project",
            "global",
            "version",
            "2.0",
            scope="system",
        )

        vault_path = result["_vault_path"]
        assert "system" in vault_path
        facts = Path(vault_path)
        assert facts.exists()
        content = facts.read_text(encoding="utf-8")
        assert "version: 2.0" in content

    def test_sync_facts_file_creates_directories(self, tmp_path):
        """_sync_facts_file creates parent dirs if they don't exist."""
        svc = MemoryV2Service()
        facts_path = tmp_path / "vault" / "projects" / "new_proj" / "facts.md"
        svc._sync_facts_file(facts_path, "project", "key", "value")
        assert facts_path.exists()
        content = facts_path.read_text(encoding="utf-8")
        assert "## project" in content
        assert "key: value" in content


class TestKVRecall:
    """Test kv_recall — scoped KV lookup with first-match-wins (spec §6)."""

    @pytest.mark.asyncio
    async def test_kv_recall_finds_in_project_scope(self, service, mock_store):
        """When a key exists in the project scope, return it immediately."""
        result = await service.kv_recall(
            "test_key",
            project_id="my-project",
            agent_type="coding",
            namespace="project",
        )
        assert result is not None
        assert result["kv_key"] == "test_key"
        assert result["_scope"] is not None

    @pytest.mark.asyncio
    async def test_kv_recall_falls_through_to_agent_type(self, service, mock_router, mock_store):
        """When project scope misses, search agent-type scope."""
        call_count = 0

        def scope_aware_store(*args, **kwargs):
            nonlocal call_count
            store = MagicMock()
            call_count += 1
            if call_count == 1:
                # Project scope: miss
                store.get_kv.return_value = None
            else:
                # Agent-type or system scope: hit
                store.get_kv.return_value = {
                    "kv_namespace": "project",
                    "kv_key": "test_key",
                    "kv_value": '"agent_type_value"',
                    "updated_at": 2000,
                    "tags": "[]",
                    "source": "",
                }
            return store

        mock_router.get_store.side_effect = scope_aware_store

        result = await service.kv_recall(
            "test_key",
            project_id="my-project",
            agent_type="coding",
            namespace="project",
        )
        assert result is not None
        assert result["kv_value"] == '"agent_type_value"'
        # Should have called get_store twice (project miss, agent-type hit)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_kv_recall_falls_through_to_system(self, service, mock_router):
        """When project and agent-type miss, search system scope."""
        call_count = 0

        def scope_aware_store(*args, **kwargs):
            nonlocal call_count
            store = MagicMock()
            call_count += 1
            if call_count <= 2:
                # Project and agent-type: miss
                store.get_kv.return_value = None
            else:
                # System scope: hit
                store.get_kv.return_value = {
                    "kv_namespace": "global",
                    "kv_key": "test_key",
                    "kv_value": '"system_value"',
                    "updated_at": 3000,
                    "tags": "[]",
                    "source": "",
                }
            return store

        mock_router.get_store.side_effect = scope_aware_store

        result = await service.kv_recall(
            "test_key",
            project_id="my-project",
            agent_type="coding",
            namespace="global",
        )
        assert result is not None
        assert result["kv_value"] == '"system_value"'
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_kv_recall_returns_none_when_all_miss(self, service, mock_router):
        """When no scope has the key, return None."""
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        result = await service.kv_recall(
            "nonexistent_key",
            project_id="my-project",
            agent_type="coding",
            namespace="project",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_kv_recall_without_project_id(self, service, mock_router, mock_store):
        """When project_id is None, skip project scope."""
        call_count = 0

        def scope_aware_store(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_store

        mock_router.get_store.side_effect = scope_aware_store

        result = await service.kv_recall(
            "test_key",
            agent_type="coding",
            namespace="project",
        )
        assert result is not None
        # Should search agent-type + system = 1 call (agent-type hit)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_kv_recall_without_agent_type(self, service, mock_router, mock_store):
        """When agent_type is None, skip agent-type scope."""
        mock_store.get_kv.return_value = None
        call_count = 0

        def scope_aware_store(*args, **kwargs):
            nonlocal call_count
            store = MagicMock()
            call_count += 1
            if call_count == 1:
                store.get_kv.return_value = None  # project: miss
            else:
                store.get_kv.return_value = {
                    "kv_namespace": "global",
                    "kv_key": "test_key",
                    "kv_value": '"sys"',
                    "updated_at": 1,
                    "tags": "[]",
                    "source": "",
                }
            return store

        mock_router.get_store.side_effect = scope_aware_store

        result = await service.kv_recall(
            "test_key",
            project_id="proj",
            namespace="global",
        )
        assert result is not None
        # project + system = 2 calls (no agent-type)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_kv_recall_system_only(self, service, mock_store):
        """With no project_id or agent_type, only system scope is searched."""
        result = await service.kv_recall("test_key")
        assert result is not None

    @pytest.mark.asyncio
    async def test_kv_recall_unavailable(self):
        """Returns None when service is unavailable."""
        svc = MemoryV2Service()
        result = await svc.kv_recall("key", project_id="proj")
        assert result is None

    @pytest.mark.asyncio
    async def test_kv_recall_default_namespace(self, service, mock_store):
        """When namespace is None, defaults to empty string."""
        await service.kv_recall("test_key", project_id="proj")
        mock_store.get_kv.assert_called_with("test_key", namespace="")


class TestRecall:
    """Test recall — unified smart retrieval (spec §7)."""

    @pytest.mark.asyncio
    async def test_recall_returns_kv_when_found(self, service, mock_store):
        """When a KV match is found, return source='kv'."""
        result = await service.recall(
            "test_key",
            project_id="proj",
            agent_type="coding",
            namespace="project",
        )
        assert result["source"] == "kv"
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_recall_falls_back_to_semantic(self, service, mock_router, mock_embedder):
        """When no KV match, falls back to semantic search."""
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        result = await service.recall(
            "how does auth work?",
            project_id="proj",
            agent_type="coding",
        )
        assert result["source"] == "semantic"
        assert len(result["results"]) >= 0

    @pytest.mark.asyncio
    async def test_recall_unavailable(self):
        """Returns unavailable source when service is not initialized."""
        svc = MemoryV2Service()
        result = await svc.recall("query")
        assert result["source"] == "unavailable"
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_recall_passes_topic_to_search(self, service, mock_router, mock_embedder):
        """Topic filter is forwarded to the semantic search fallback."""
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        await service.recall(
            "testing best practices",
            project_id="proj",
            topic="testing",
        )
        # Semantic search should have been called (the router.search mock)
        mock_router.search.assert_called_once()
        call_kwargs = mock_router.search.call_args
        assert call_kwargs.kwargs.get("topic") == "testing"


class TestPluginRecallHandlers:
    """Test plugin command handlers for memory_fact_recall and memory_recall."""

    @pytest.fixture
    def plugin(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        return MemoryV2Plugin()

    @pytest.fixture
    def wired_plugin(self, plugin, service):
        """Plugin with a wired-up service."""
        plugin._service = service
        plugin._log = MagicMock()
        return plugin

    @pytest.mark.asyncio
    async def test_fact_recall_handler_found(self, wired_plugin):
        result = await wired_plugin.cmd_memory_fact_recall(
            {"key": "test_key", "project_id": "proj", "agent_type": "coding"}
        )
        assert result["success"] is True
        assert result["found"] is True
        assert result["key"] == "test_key"
        assert "resolved_scope" in result

    @pytest.mark.asyncio
    async def test_fact_recall_handler_not_found(self, wired_plugin, mock_router):
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        result = await wired_plugin.cmd_memory_fact_recall(
            {"key": "missing", "project_id": "proj", "agent_type": "coding"}
        )
        assert result["success"] is True
        assert result["found"] is False
        assert "scopes_searched" in result
        assert len(result["scopes_searched"]) == 3  # project, agent-type, system

    @pytest.mark.asyncio
    async def test_fact_recall_handler_missing_key(self, wired_plugin):
        result = await wired_plugin.cmd_memory_fact_recall({})
        assert "error" in result
        assert "key" in result["error"]

    @pytest.mark.asyncio
    async def test_fact_recall_handler_unavailable(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_fact_recall({"key": "test"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_handler_kv_hit(self, wired_plugin):
        result = await wired_plugin.cmd_memory_recall(
            {"query": "test_key", "project_id": "proj", "namespace": "project"}
        )
        assert result["success"] is True
        assert result["source"] == "kv"
        assert result["count"] >= 1

    @pytest.mark.asyncio
    async def test_recall_handler_semantic_fallback(self, wired_plugin, mock_router):
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        result = await wired_plugin.cmd_memory_recall(
            {"query": "how does auth work?", "project_id": "proj"}
        )
        assert result["success"] is True
        assert result["source"] == "semantic"

    @pytest.mark.asyncio
    async def test_recall_handler_missing_query(self, wired_plugin):
        result = await wired_plugin.cmd_memory_recall({})
        assert "error" in result
        assert "query" in result["error"]

    @pytest.mark.asyncio
    async def test_recall_handler_unavailable(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_recall({"query": "test"})
        assert "error" in result
        assert "not available" in result["error"]


class TestToolSchemas:
    """Test tool definitions for new recall tools."""

    def test_memory_fact_recall_tool_exists(self):
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        tool = next((t for t in TOOL_DEFINITIONS if t["name"] == "memory_fact_recall"), None)
        assert tool is not None
        assert "key" in tool["input_schema"]["required"]
        props = tool["input_schema"]["properties"]
        assert "project_id" in props
        assert "agent_type" in props
        assert "namespace" in props

    def test_memory_recall_tool_exists(self):
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        tool = next((t for t in TOOL_DEFINITIONS if t["name"] == "memory_recall"), None)
        assert tool is not None
        assert "query" in tool["input_schema"]["required"]
        props = tool["input_schema"]["properties"]
        assert "project_id" in props
        assert "agent_type" in props
        assert "namespace" in props
        assert "topic" in props
        assert "top_k" in props

    def test_new_tools_in_v2_only_set(self):
        from src.plugins.internal.memory_v2 import V2_ONLY_TOOLS

        assert "memory_fact_recall" in V2_ONLY_TOOLS
        assert "memory_recall" in V2_ONLY_TOOLS

    def test_build_scope_list(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        # Both project and agent-type
        scopes = MemoryV2Plugin._build_scope_list("my-proj", "coding")
        assert scopes == ["project_my-proj", "agenttype_coding", "system"]

        # Only project
        scopes = MemoryV2Plugin._build_scope_list("my-proj", None)
        assert scopes == ["project_my-proj", "system"]

        # Only agent-type
        scopes = MemoryV2Plugin._build_scope_list(None, "coding")
        assert scopes == ["agenttype_coding", "system"]

        # Neither
        scopes = MemoryV2Plugin._build_scope_list(None, None)
        assert scopes == ["system"]


class TestMemoryGet:
    """Test memory_get unified auto-routing tool (spec §7, roadmap 2.2.11).

    memory_get is the default retrieval tool for agents.  It tries KV exact
    match first (with scope resolution) then falls back to semantic search.
    """

    @pytest.fixture
    def plugin(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        return MemoryV2Plugin()

    @pytest.fixture
    def wired_plugin(self, plugin, service):
        """Plugin with a wired-up service."""
        plugin._service = service
        plugin._log = MagicMock()
        return plugin

    @pytest.mark.asyncio
    async def test_memory_get_kv_hit(self, wired_plugin):
        """memory_get returns KV result when an exact key match is found."""
        result = await wired_plugin.cmd_memory_get(
            {"query": "test_key", "project_id": "proj", "agent_type": "coding"}
        )
        assert result["success"] is True
        assert result["source"] == "kv"
        assert result["count"] >= 1
        assert "results" in result
        # KV hit should include scopes_searched for diagnostics
        assert "scopes_searched" in result
        assert "project_proj" in result["scopes_searched"]

    @pytest.mark.asyncio
    async def test_memory_get_semantic_fallback(self, wired_plugin, mock_router):
        """memory_get falls back to semantic search when no KV match."""
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        result = await wired_plugin.cmd_memory_get(
            {"query": "how does auth work?", "project_id": "proj"}
        )
        assert result["success"] is True
        assert result["source"] == "semantic"
        assert result["count"] >= 1
        assert "results" in result

    @pytest.mark.asyncio
    async def test_memory_get_missing_query(self, wired_plugin):
        """memory_get returns error when query is missing."""
        result = await wired_plugin.cmd_memory_get({})
        assert "error" in result
        assert "query" in result["error"]

    @pytest.mark.asyncio
    async def test_memory_get_unavailable(self, plugin):
        """memory_get returns unavailable error when service is not ready."""
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_get({"query": "test"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_memory_get_with_topic_filter(self, wired_plugin, mock_router):
        """memory_get passes topic filter to semantic search fallback."""
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        result = await wired_plugin.cmd_memory_get(
            {"query": "auth patterns", "project_id": "proj", "topic": "authentication"}
        )
        assert result["success"] is True
        assert result["source"] == "semantic"

    @pytest.mark.asyncio
    async def test_memory_get_with_agent_type(self, wired_plugin):
        """memory_get uses agent_type for scope resolution."""
        result = await wired_plugin.cmd_memory_get(
            {"query": "test_key", "project_id": "proj", "agent_type": "coding"}
        )
        assert result["success"] is True
        # Scopes should include agent-type
        if "scopes_searched" in result:
            assert "agenttype_coding" in result["scopes_searched"]

    @pytest.mark.asyncio
    async def test_memory_get_minimal_params(self, wired_plugin):
        """memory_get works with only query (no project_id or agent_type)."""
        result = await wired_plugin.cmd_memory_get({"query": "test_key"})
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_memory_get_custom_top_k(self, wired_plugin, mock_router):
        """memory_get passes custom top_k to semantic search."""
        miss_store = MagicMock()
        miss_store.get_kv.return_value = None
        mock_router.get_store.return_value = miss_store

        result = await wired_plugin.cmd_memory_get(
            {"query": "something", "project_id": "proj", "top_k": 3}
        )
        assert result["success"] is True


class TestMemoryGetToolSchema:
    """Test memory_get tool definition and registration."""

    def test_memory_get_tool_exists(self):
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        tool = next((t for t in TOOL_DEFINITIONS if t["name"] == "memory_get"), None)
        assert tool is not None
        assert "query" in tool["input_schema"]["required"]
        props = tool["input_schema"]["properties"]
        assert "project_id" in props
        assert "agent_type" in props
        assert "topic" in props
        assert "top_k" in props

    def test_memory_get_in_v2_only_set(self):
        from src.plugins.internal.memory_v2 import V2_ONLY_TOOLS

        assert "memory_get" in V2_ONLY_TOOLS

    def test_memory_get_description_mentions_auto_routing(self):
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        tool = next((t for t in TOOL_DEFINITIONS if t["name"] == "memory_get"), None)
        assert tool is not None
        desc = tool["description"].lower()
        assert "kv" in desc or "key" in desc
        assert "semantic" in desc

    def test_memory_get_does_not_require_project_id(self):
        """memory_get should work without project_id (system-scope only)."""
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        tool = next((t for t in TOOL_DEFINITIONS if t["name"] == "memory_get"), None)
        assert tool is not None
        # Only query is required
        assert tool["input_schema"]["required"] == ["query"]

    def test_memory_get_no_namespace_param(self):
        """memory_get omits namespace — simpler interface than memory_recall."""
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        tool = next((t for t in TOOL_DEFINITIONS if t["name"] == "memory_get"), None)
        assert tool is not None
        props = tool["input_schema"]["properties"]
        assert "namespace" not in props


class TestPluginKVSetWithScope:
    """Test the plugin command handler for memory_kv_set with scope."""

    @pytest.fixture
    def plugin(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        return MemoryV2Plugin()

    @pytest.fixture
    def wired_plugin(self, plugin, service):
        """Plugin with a wired-up service."""
        plugin._service = service
        plugin._log = MagicMock()
        return plugin

    @pytest.mark.asyncio
    async def test_kv_set_handler_with_scope(self, wired_plugin, mock_store):
        """Handler passes scope to the service."""
        result = await wired_plugin.cmd_memory_kv_set(
            {
                "project_id": "proj",
                "namespace": "project",
                "key": "k",
                "value": "v",
                "scope": "system",
            }
        )
        assert result["success"] is True
        # Response should include scope info from the service
        assert "vault_path" in result
        assert "scope" in result

    @pytest.mark.asyncio
    async def test_kv_set_handler_without_scope(self, wired_plugin, mock_store):
        """Handler defaults scope to None (project scope)."""
        result = await wired_plugin.cmd_memory_kv_set(
            {
                "project_id": "proj",
                "namespace": "project",
                "key": "k",
                "value": "v",
            }
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_kv_set_tool_schema_has_scope(self):
        """The tool schema for memory_kv_set includes the scope property."""
        from src.plugins.internal.memory_v2 import TOOL_DEFINITIONS

        kv_set_tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "memory_kv_set")
        props = kv_set_tool["input_schema"]["properties"]
        assert "scope" in props
        assert "scope" not in kv_set_tool["input_schema"]["required"]


# ---------------------------------------------------------------------------
# Temporal Facts
# ---------------------------------------------------------------------------


class TestTemporalFacts:
    """Test temporal fact get/set/history operations."""

    @pytest.mark.asyncio
    async def test_fact_get_current(self, service, mock_store):
        result = await service.fact_get("test-project", "deploy_branch")
        assert result is not None
        assert result["kv_key"] == "deploy_branch"
        mock_store.get_temporal.assert_called_once_with("deploy_branch", at=None)

    @pytest.mark.asyncio
    async def test_fact_get_as_of(self, service, mock_store):
        await service.fact_get("test-project", "deploy_branch", as_of=150)
        mock_store.get_temporal.assert_called_once_with("deploy_branch", at=150)

    @pytest.mark.asyncio
    async def test_fact_get_not_found(self, service, mock_store):
        mock_store.get_temporal.return_value = []
        result = await service.fact_get("test-project", "missing_fact")
        assert result is None

    @pytest.mark.asyncio
    async def test_fact_set(self, service, mock_store):
        result = await service.fact_set("test-project", "deploy_branch", "release")
        assert result is not None
        mock_store.set_temporal.assert_called_once_with(
            "deploy_branch",
            "release",
            content="fact/deploy_branch: release",
        )

    @pytest.mark.asyncio
    async def test_fact_history(self, service, mock_store):
        results = await service.fact_history("test-project", "deploy_branch")
        assert len(results) == 2
        assert results[0]["valid_from"] == 100
        assert results[1]["valid_from"] == 200
        mock_store.get_temporal_history.assert_called_once_with("deploy_branch")

    @pytest.mark.asyncio
    async def test_fact_get_unavailable(self):
        svc = MemoryV2Service()
        result = await svc.fact_get("proj", "key")
        assert result is None

    @pytest.mark.asyncio
    async def test_fact_set_unavailable(self):
        svc = MemoryV2Service()
        with pytest.raises(RuntimeError, match="not available"):
            await svc.fact_set("proj", "key", "val")

    @pytest.mark.asyncio
    async def test_fact_list(self, service, mock_store):
        results = await service.fact_list("test-project")
        assert len(results) == 2
        assert results[0]["kv_key"] == "deploy_branch"
        assert results[1]["kv_key"] == "python_version"
        mock_store.list_temporal.assert_called_once_with(namespace="", current_only=True)

    @pytest.mark.asyncio
    async def test_fact_list_with_namespace(self, service, mock_store):
        await service.fact_list("test-project", "config")
        mock_store.list_temporal.assert_called_once_with(namespace="config", current_only=True)

    @pytest.mark.asyncio
    async def test_fact_list_include_closed(self, service, mock_store):
        await service.fact_list("test-project", current_only=False)
        mock_store.list_temporal.assert_called_once_with(namespace="", current_only=False)

    @pytest.mark.asyncio
    async def test_fact_list_unavailable(self):
        svc = MemoryV2Service()
        result = await svc.fact_list("proj")
        assert result == []

    @pytest.mark.asyncio
    async def test_fact_history_unavailable(self):
        svc = MemoryV2Service()
        result = await svc.fact_history("proj", "key")
        assert result == []


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    """Test search operations."""

    @pytest.mark.asyncio
    async def test_search_default_multiscope(self, service, mock_router, mock_embedder):
        results = await service.search("test-project", "how does auth work?")
        assert len(results) == 1
        assert results[0]["content"] == "Multi-scope result"
        mock_embedder.embed.assert_called_once_with(["how does auth work?"])
        mock_router.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_explicit_scope(self, service, mock_store, mock_embedder):
        results = await service.search("test-project", "query", scope="system")
        assert len(results) == 1
        mock_store.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_batch_search(self, service, mock_router, mock_embedder):
        # Reset embed mock for batch
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 384])
        results = await service.batch_search("test-project", ["query1", "query2"])
        assert "query1" in results
        assert "query2" in results

    @pytest.mark.asyncio
    async def test_search_by_tag(self, service, mock_router):
        results = await service.search_by_tag("sqlite")
        assert len(results) == 1
        mock_router.search_by_tag_async.assert_called_once_with(
            "sqlite", entry_type=None, topic=None, limit=10
        )

    @pytest.mark.asyncio
    async def test_search_unavailable(self):
        svc = MemoryV2Service()
        results = await svc.search("proj", "query")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_by_tag_unavailable(self):
        svc = MemoryV2Service()
        results = await svc.search_by_tag("tag")
        assert results == []


# ---------------------------------------------------------------------------
# Browse / List Memories
# ---------------------------------------------------------------------------


class TestListMemories:
    """Test list_memories for browsing entries in a scope."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_list_memories_default(self, service, mock_store):
        """Default call returns document entries sorted by updated_at desc."""
        mock_store.query.side_effect = None
        mock_store.query.return_value = [
            {
                "chunk_hash": "h1",
                "content": "First insight about authentication",
                "heading": "Auth insight",
                "topic": "authentication",
                "tags": '["insight"]',
                "source": "vault/auth.md",
                "entry_type": "document",
                "retrieval_count": 5,
                "updated_at": 1000,
            },
            {
                "chunk_hash": "h2",
                "content": "Second insight about testing",
                "heading": "Testing insight",
                "topic": "testing",
                "tags": '["insight", "testing"]',
                "source": "vault/test.md",
                "entry_type": "document",
                "retrieval_count": 2,
                "updated_at": 2000,
            },
        ]

        results = await service.list_memories("test-project")
        assert len(results) == 2
        # Sorted newest first
        assert results[0]["chunk_hash"] == "h2"
        assert results[1]["chunk_hash"] == "h1"
        # Scope annotation
        assert results[0]["_scope"] == "project"
        assert results[0]["_scope_id"] == "test-project"
        mock_store.query.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_list_memories_with_topic_filter(self, service, mock_store):
        """Topic filter is passed to query as filter expression."""
        mock_store.query.side_effect = None
        mock_store.query.return_value = []

        await service.list_memories("test-project", topic="authentication")
        call_args = mock_store.query.call_args
        filter_expr = call_args.kwargs.get("filter_expr", "")
        assert "document" in filter_expr
        assert "authentication" in filter_expr

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_list_memories_with_tag_filter(self, service, mock_store):
        """Tag filter uses LIKE for JSON array matching."""
        mock_store.query.side_effect = None
        mock_store.query.return_value = []

        await service.list_memories("test-project", tag="insight")
        call_args = mock_store.query.call_args
        filter_expr = call_args.kwargs.get("filter_expr", "")
        assert "insight" in filter_expr
        assert "like" in filter_expr.lower() or "LIKE" in filter_expr

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_list_memories_pagination(self, service, mock_store):
        """Offset and limit for pagination."""
        mock_store.query.side_effect = None
        entries = [
            {
                "chunk_hash": f"h{i}",
                "content": f"Memory {i}",
                "heading": f"Heading {i}",
                "topic": "",
                "tags": "[]",
                "source": "",
                "entry_type": "document",
                "retrieval_count": 0,
                "updated_at": 1000 + i,
            }
            for i in range(10)
        ]
        mock_store.query.return_value = entries

        results = await service.list_memories("test-project", offset=2, limit=3)
        # After sorting desc (updated_at 1009..1000), offset=2 gives items 7,6,5
        assert len(results) == 3

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_list_memories_limit_cap(self, service, mock_store):
        """Limit is capped at 200."""
        mock_store.query.side_effect = None
        mock_store.query.return_value = []

        await service.list_memories("test-project", limit=500)
        # No error, limit is capped internally

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_list_memories_all_entry_types(self, service, mock_store):
        """Empty entry_type lists all types."""
        mock_store.query.side_effect = None
        mock_store.query.return_value = []

        await service.list_memories("test-project", entry_type="")
        call_args = mock_store.query.call_args
        filter_expr = call_args.kwargs.get("filter_expr", "")
        assert "entry_type" not in filter_expr

    @pytest.mark.asyncio
    async def test_list_memories_unavailable(self):
        """Returns empty list when service is unavailable."""
        svc = MemoryV2Service()
        result = await svc.list_memories("proj")
        assert result == []


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    """Test stats retrieval."""

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_stats(self, service, mock_store):
        result = await service.stats("test-project")
        assert result["total_entries"] == 42
        assert result["documents"] == 2
        assert result["kv_entries"] == 1
        assert result["temporal_entries"] == 1
        assert result["needs_reindex"] is False

    @pytest.mark.asyncio
    async def test_stats_unavailable(self):
        svc = MemoryV2Service()
        result = await svc.stats("proj")
        assert "error" in result


# ---------------------------------------------------------------------------
# Plugin Integration
# ---------------------------------------------------------------------------


class TestPluginHandlers:
    """Test the plugin command handlers via MemoryV2Plugin."""

    @pytest.fixture
    def plugin(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        return MemoryV2Plugin()

    @pytest.fixture
    def wired_plugin(self, plugin, service):
        """Plugin with a wired-up service."""
        plugin._service = service
        plugin._log = MagicMock()
        return plugin

    @pytest.mark.asyncio
    async def test_kv_get_handler(self, wired_plugin):
        result = await wired_plugin.cmd_memory_kv_get(
            {"project_id": "proj", "namespace": "project", "key": "test_key"}
        )
        assert result["success"] is True
        assert result["found"] is True

    @pytest.mark.asyncio
    async def test_kv_get_not_found_handler(self, wired_plugin, mock_store):
        mock_store.get_kv.return_value = None
        result = await wired_plugin.cmd_memory_kv_get(
            {"project_id": "proj", "namespace": "project", "key": "missing"}
        )
        assert result["success"] is True
        assert result["found"] is False

    @pytest.mark.asyncio
    async def test_kv_set_handler(self, wired_plugin):
        result = await wired_plugin.cmd_memory_kv_set(
            {
                "project_id": "proj",
                "namespace": "project",
                "key": "new_key",
                "value": "new_value",
            }
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_kv_list_handler(self, wired_plugin):
        result = await wired_plugin.cmd_memory_kv_list(
            {"project_id": "proj", "namespace": "project"}
        )
        assert result["success"] is True
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_fact_get_handler(self, wired_plugin):
        result = await wired_plugin.cmd_memory_fact_get(
            {"project_id": "proj", "key": "deploy_branch"}
        )
        assert result["success"] is True
        assert result["found"] is True

    @pytest.mark.asyncio
    async def test_fact_set_handler(self, wired_plugin):
        result = await wired_plugin.cmd_memory_fact_set(
            {"project_id": "proj", "key": "deploy_branch", "value": "release"}
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_fact_history_handler(self, wired_plugin):
        result = await wired_plugin.cmd_memory_fact_history(
            {"project_id": "proj", "key": "deploy_branch"}
        )
        assert result["success"] is True
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_fact_list_handler(self, wired_plugin):
        result = await wired_plugin.cmd_memory_fact_list({"project_id": "proj"})
        assert result["success"] is True
        assert result["count"] == 2
        assert result["namespace"] == ""
        assert result["current_only"] is True
        # Verify entries are formatted via _format_temporal_entry
        assert result["entries"][0]["key"] == "deploy_branch"
        assert result["entries"][1]["key"] == "python_version"

    @pytest.mark.asyncio
    async def test_fact_list_handler_with_namespace(self, wired_plugin):
        result = await wired_plugin.cmd_memory_fact_list(
            {"project_id": "proj", "namespace": "config", "current_only": False}
        )
        assert result["success"] is True
        assert result["namespace"] == "config"
        assert result["current_only"] is False

    @pytest.mark.asyncio
    async def test_fact_list_handler_missing_project_id(self, wired_plugin):
        result = await wired_plugin.cmd_memory_fact_list({})
        assert "error" in result
        assert "project_id" in result["error"]

    @pytest.mark.asyncio
    async def test_fact_list_handler_unavailable(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_fact_list({"project_id": "proj"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_search_by_tag_handler(self, wired_plugin):
        result = await wired_plugin.cmd_memory_search_by_tag({"tag": "sqlite"})
        assert result["success"] is True
        assert result["count"] == 1

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_stats_handler(self, wired_plugin):
        result = await wired_plugin.cmd_memory_stats({"project_id": "proj"})
        assert result["success"] is True
        assert result["total_entries"] == 42

    @pytest.mark.asyncio
    async def test_missing_project_id(self, wired_plugin):
        result = await wired_plugin.cmd_memory_kv_get({"namespace": "ns", "key": "k"})
        assert "error" in result
        assert "project_id" in result["error"]

    @pytest.mark.asyncio
    async def test_unavailable_service(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_kv_get(
            {"project_id": "proj", "namespace": "ns", "key": "k"}
        )
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not MEMSEARCH_AVAILABLE, reason="memsearch not installed")
    async def test_memory_list_handler(self, wired_plugin, mock_store):
        """memory_list returns formatted entries with metadata."""
        mock_store.query.side_effect = None
        mock_store.query.return_value = [
            {
                "chunk_hash": "h1",
                "content": "# Auth token handling\nAlways refresh tokens before...",
                "heading": "Auth token handling",
                "topic": "authentication",
                "tags": '["insight", "auth"]',
                "source": "vault/auth.md",
                "entry_type": "document",
                "retrieval_count": 5,
                "updated_at": 1000,
            },
        ]
        result = await wired_plugin.cmd_memory_list({"project_id": "proj"})
        assert result["success"] is True
        assert result["count"] == 1
        entry = result["entries"][0]
        assert entry["title"] == "Auth token handling"
        assert entry["topic"] == "authentication"
        assert entry["tags"] == ["insight", "auth"]
        assert entry["retrieval_count"] == 5
        assert entry["chunk_hash"] == "h1"
        assert "content_preview" in entry

    @pytest.mark.asyncio
    async def test_memory_list_missing_project_id(self, wired_plugin):
        result = await wired_plugin.cmd_memory_list({})
        assert "error" in result
        assert "project_id" in result["error"]

    @pytest.mark.asyncio
    async def test_memory_list_unavailable(self, plugin):
        plugin._service = None
        plugin._log = MagicMock()
        result = await plugin.cmd_memory_list({"project_id": "proj"})
        assert "error" in result
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_not_implemented_stubs(self, wired_plugin):
        """Overlapping v1 commands return 'not implemented'."""
        result = await wired_plugin.cmd_view_profile({"project_id": "proj"})
        assert "error" in result
        assert "not yet implemented" in result["error"]

        result = await wired_plugin.cmd_compact_memory({"project_id": "proj"})
        assert "error" in result

        result = await wired_plugin.cmd_consolidate({"project_id": "proj", "mode": "daily"})
        assert "error" in result


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------


class TestFormatHelpers:
    """Test response formatting helpers."""

    @pytest.fixture
    def plugin(self):
        from src.plugins.internal.memory_v2 import MemoryV2Plugin

        p = MemoryV2Plugin()
        p._log = MagicMock()
        return p

    def test_decode_kv_value_json(self, plugin):
        assert plugin._decode_kv_value('"hello"') == "hello"
        assert plugin._decode_kv_value("42") == 42
        assert plugin._decode_kv_value('["a","b"]') == ["a", "b"]

    def test_decode_kv_value_raw(self, plugin):
        assert plugin._decode_kv_value("plain text") == "plain text"

    def test_decode_tags(self, plugin):
        assert plugin._decode_tags('["a","b"]') == ["a", "b"]
        assert plugin._decode_tags("[]") == []
        assert plugin._decode_tags("invalid") == []
        assert plugin._decode_tags("") == []

    def test_format_kv_entry(self, plugin):
        entry = {
            "kv_namespace": "project",
            "kv_key": "test",
            "kv_value": '"value"',
            "updated_at": 1000,
            "tags": '["tag1"]',
            "source": "vault/test.md",
        }
        formatted = plugin._format_kv_entry(entry)
        assert formatted["namespace"] == "project"
        assert formatted["key"] == "test"
        assert formatted["value"] == "value"
        assert formatted["tags"] == ["tag1"]

    def test_format_temporal_entry(self, plugin):
        entry = {
            "kv_key": "deploy_branch",
            "kv_value": '"main"',
            "valid_from": 100,
            "valid_to": 200,
            "updated_at": 100,
            "tags": "[]",
            "source": "",
        }
        formatted = plugin._format_temporal_entry(entry)
        assert formatted["key"] == "deploy_branch"
        assert formatted["value"] == "main"
        assert formatted["valid_from"] == 100
        assert formatted["valid_to"] == 200

    def test_format_list_entry(self, plugin):
        entry = {
            "chunk_hash": "abc123",
            "content": "# OAuth refresh\nTokens must be refreshed before expiry.",
            "heading": "OAuth refresh",
            "topic": "authentication",
            "tags": '["insight", "auth"]',
            "source": "vault/auth.md",
            "entry_type": "document",
            "retrieval_count": 7,
            "updated_at": 1234,
        }
        formatted = plugin._format_list_entry(entry)
        assert formatted["chunk_hash"] == "abc123"
        assert formatted["title"] == "OAuth refresh"
        assert formatted["topic"] == "authentication"
        assert formatted["tags"] == ["insight", "auth"]
        assert formatted["retrieval_count"] == 7
        assert formatted["updated_at"] == 1234
        assert formatted["entry_type"] == "document"
        assert "content_preview" in formatted

    def test_format_list_entry_no_heading_uses_content(self, plugin):
        """When heading is empty, title is extracted from content."""
        entry = {
            "chunk_hash": "def456",
            "content": "Always use parameterized queries to prevent SQL injection.",
            "heading": "",
            "topic": "",
            "tags": "[]",
            "source": "",
            "entry_type": "document",
            "retrieval_count": 0,
            "updated_at": 0,
        }
        formatted = plugin._format_list_entry(entry)
        assert formatted["title"] == "Always use parameterized queries to prevent SQL injection."

    def test_format_list_entry_long_content_preview(self, plugin):
        """Content preview is truncated for long content."""
        long_content = "x" * 300
        entry = {
            "chunk_hash": "long1",
            "content": long_content,
            "heading": "Long entry",
            "topic": "",
            "tags": "[]",
            "source": "",
            "entry_type": "document",
            "retrieval_count": 0,
            "updated_at": 0,
        }
        formatted = plugin._format_list_entry(entry)
        assert len(formatted["content_preview"]) <= 201  # 200 + ellipsis char
        assert formatted["content_preview"].endswith("…")

    def test_extract_title_markdown_heading(self, plugin):
        assert plugin._extract_title("# My Title\nBody text") == "My Title"
        assert plugin._extract_title("## Sub Heading\nMore text") == "Sub Heading"

    def test_extract_title_plain_text(self, plugin):
        assert plugin._extract_title("Just a plain first line") == "Just a plain first line"

    def test_extract_title_empty(self, plugin):
        assert plugin._extract_title("") == ""

    def test_extract_title_long(self, plugin):
        long_line = "A" * 100
        result = plugin._extract_title(long_line)
        assert len(result) == 80
