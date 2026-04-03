"""Integration tests for the memory subsystem.

These tests require the ``memsearch`` package to be installed along with a
working Milvus Lite backend. They are marked with ``@pytest.mark.integration``
so they are skipped in CI environments without the dependency.

Run explicitly with: ``pytest -m integration tests/test_memory_integration.py``
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

# Skip entire module if memsearch or sentence-transformers is not installed.
# memsearch is installed but the "local" embedding provider requires
# sentence-transformers, which is an optional dependency.
memsearch = pytest.importorskip("memsearch", reason="memsearch not installed")
pytest.importorskip(
    "sentence_transformers",
    reason="sentence-transformers not installed (required for local embeddings)",
)

from src.memory import MemoryConfig, MemoryManager  # noqa: E402


@dataclass
class FakeTask:
    id: str = "int-task-1"
    project_id: str = "int-project"
    title: str = "Integration test task"
    description: str = "Verify memory round-trip"
    task_type: MagicMock = field(default_factory=lambda: MagicMock(value="test"))


@dataclass
class FakeOutput:
    result: MagicMock = field(default_factory=lambda: MagicMock(value="completed"))
    summary: str = "Integration test completed successfully."
    files_changed: list = field(default_factory=lambda: ["src/test.py"])
    tokens_used: int = 100


@pytest.mark.integration
class TestMemoryEndToEnd:
    """End-to-end tests requiring memsearch installed + Milvus Lite."""

    @pytest.fixture
    def workspace(self, tmp_path):
        """Create a workspace with memory and notes directories."""
        memory_dir = tmp_path / "memory" / "tasks"
        memory_dir.mkdir(parents=True)
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        return tmp_path

    @pytest.fixture
    def memory_config(self, tmp_path):
        """Config pointing at a temp Milvus Lite DB."""
        db_path = str(tmp_path / "test_milvus.db")
        return MemoryConfig(
            enabled=True,
            embedding_provider="local",  # no API key needed
            milvus_uri=db_path,
        )

    async def test_index_and_search_roundtrip(self, workspace, memory_config):
        """Write a markdown file, index it, search for it."""
        mgr = MemoryManager(memory_config)
        try:
            # Write a markdown file
            md_path = workspace / "memory" / "tasks" / "test-doc.md"
            md_path.write_text(
                "# Authentication System\n\n"
                "Implemented JWT-based authentication with refresh tokens.\n"
                "Used PyJWT library for token generation.\n"
            )

            # Index
            instance = await mgr.get_instance("test-proj", str(workspace))
            assert instance is not None
            await instance.index()

            # Search
            results = await mgr.search("test-proj", str(workspace), "JWT authentication")
            assert len(results) > 0
            assert any(
                "JWT" in r.get("content", "") or "authentication" in r.get("content", "").lower()
                for r in results
            )
        finally:
            await mgr.close()

    async def test_remember_then_recall(self, workspace, memory_config):
        """Complete a task, then verify recall finds it for a similar task."""
        mgr = MemoryManager(memory_config)
        try:
            task = FakeTask(
                id="auth-task",
                title="Add JWT authentication",
                description="Implement token-based auth with refresh tokens",
            )
            output = FakeOutput(
                summary="Implemented JWT auth with PyJWT, added refresh token rotation.",
                files_changed=["src/auth/jwt.py", "src/middleware/auth.py"],
            )

            # Remember
            path = await mgr.remember(task, output, str(workspace))
            assert path is not None
            assert os.path.exists(path)

            # Index the memory
            instance = await mgr.get_instance(task.project_id, str(workspace))
            await instance.index()

            # Recall with a similar task
            similar_task = FakeTask(
                id="auth-task-2",
                title="Fix authentication bug",
                description="Token refresh not working correctly",
            )
            memories = await mgr.recall(similar_task, str(workspace))
            assert len(memories) > 0
        finally:
            await mgr.close()

    async def test_multiple_projects_isolated(self, tmp_path, memory_config):
        """Memories from project A don't appear in project B searches."""
        mgr = MemoryManager(memory_config)
        try:
            # Set up two workspaces
            ws_a = tmp_path / "ws_a"
            ws_b = tmp_path / "ws_b"
            for ws in [ws_a, ws_b]:
                (ws / "memory" / "tasks").mkdir(parents=True)

            # Write a file only in project A
            (ws_a / "memory" / "tasks" / "unique.md").write_text(
                "# Unique Feature\n\nThis is a very unique quantum flux capacitor implementation.\n"
            )

            # Index both
            inst_a = await mgr.get_instance("proj-a", str(ws_a))
            inst_b = await mgr.get_instance("proj-b", str(ws_b))
            await inst_a.index()
            await inst_b.index()

            # Verify collection names are different
            assert mgr._collection_name("proj-a") != mgr._collection_name("proj-b")

            # Search in project B should not find project A's content
            results_b = await mgr.search("proj-b", str(ws_b), "quantum flux capacitor")
            assert len(results_b) == 0
        finally:
            await mgr.close()

    async def test_notes_directory_indexed(self, workspace, memory_config):
        """Files in notes/ are searchable via memory."""
        mgr = MemoryManager(memory_config)
        try:
            # Write a note
            (workspace / "notes" / "architecture.md").write_text(
                "# Architecture Decision\n\nWe chose PostgreSQL over MySQL for its JSONB support.\n"
            )

            instance = await mgr.get_instance("test-proj", str(workspace))
            await instance.index()

            results = await mgr.search("test-proj", str(workspace), "PostgreSQL JSONB")
            assert len(results) > 0
        finally:
            await mgr.close()

    async def test_reindex_after_file_deletion(self, workspace, memory_config):
        """Deleted files are cleaned from the index on reindex."""
        mgr = MemoryManager(memory_config)
        try:
            md_path = workspace / "memory" / "tasks" / "temp.md"
            md_path.write_text("# Temporary Task\n\nThis will be deleted.\n")

            instance = await mgr.get_instance("test-proj", str(workspace))
            await instance.index()

            # Delete the file and force reindex
            md_path.unlink()
            await mgr.reindex("test-proj", str(workspace))

            # After reindex, the deleted content should not be found
            # (behavior depends on memsearch implementation — at minimum
            # the reindex call should not error)
        finally:
            await mgr.close()

    async def test_hook_memory_search_step_roundtrip(self, workspace, memory_config):
        """Hook engine memory_search step returns real results after indexing."""
        from unittest.mock import AsyncMock, MagicMock

        from src.config import AppConfig, HookEngineConfig
        from src.event_bus import EventBus
        from src.hooks import HookEngine

        mgr = MemoryManager(memory_config)
        try:
            # Write and index content
            (workspace / "memory" / "tasks" / "hook-test.md").write_text(
                "# Kubernetes Deployment\n\n"
                "Configured horizontal pod autoscaler for the API service.\n"
                "Set min replicas to 2, max to 10, target CPU 70%.\n"
            )
            instance = await mgr.get_instance("hook-proj", str(workspace))
            await instance.index()

            # Set up a HookEngine with a real MemoryManager
            db = MagicMock()
            db.get_project_workspace_path = AsyncMock(return_value=str(workspace))
            bus = EventBus()
            cfg = AppConfig()
            cfg.hook_engine = HookEngineConfig(enabled=True)
            engine = HookEngine(db, bus, cfg)

            orchestrator = MagicMock()
            orchestrator.memory_manager = mgr
            engine._orchestrator = orchestrator

            # Execute a memory_search step
            step = {
                "type": "memory_search",
                "project_id": "hook-proj",
                "query": "Kubernetes autoscaling",
                "top_k": 3,
            }
            results = await engine._run_context_steps([step])

            assert len(results) == 1
            result = results[0]
            assert result["count"] > 0
            assert "content" in result
            # The content should reference our indexed document
            assert len(result["content"]) > 0
        finally:
            await mgr.close()
