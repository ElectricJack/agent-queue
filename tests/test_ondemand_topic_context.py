"""Tests for on-demand L2 topic context loading (roadmap 3.3.6).

Verifies that MemoryManager.load_topic_context_on_demand() correctly
detects topics from arbitrary text and loads matching knowledge files
and memories mid-task.  Also tests the recall_topic_context command
on CommandHandler.
"""

import os
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest  # noqa: F401 — needed for tmp_path fixture

from src.config import MemoryConfig
from src.memory import MemoryManager


# ---------------------------------------------------------------------------
# Lightweight fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    id: str = "task-123"
    project_id: str = "test-project"
    title: str = "Add user auth"
    description: str = "Implement JWT authentication"
    task_type: MagicMock = field(default_factory=lambda: MagicMock(value="feature"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(storage_root: str = "/tmp/aq-test", **overrides) -> MemoryManager:
    cfg = MemoryConfig(enabled=True, **overrides)
    return MemoryManager(cfg, storage_root=storage_root)


def _write_knowledge(knowledge_dir: str, topic: str, content: str) -> str:
    """Helper: write a knowledge base file."""
    os.makedirs(knowledge_dir, exist_ok=True)
    path = os.path.join(knowledge_dir, f"{topic}.md")
    with open(path, "w") as f:
        f.write(content)
    return path


def _write_note(notes_dir: str, filename: str, topic: str, body: str) -> str:
    """Helper: write a note file with topic frontmatter."""
    os.makedirs(notes_dir, exist_ok=True)
    path = os.path.join(notes_dir, filename)
    with open(path, "w") as f:
        f.write(f"---\ntopic: {topic}\ntags: [insight]\n---\n\n{body}")
    return path


# ---------------------------------------------------------------------------
# load_topic_context_on_demand — core functionality
# ---------------------------------------------------------------------------


class TestLoadTopicContextOnDemand:
    """Tests for MemoryManager.load_topic_context_on_demand()."""

    async def test_detects_and_loads_from_text(self, tmp_path):
        """Should detect topics from text and load matching knowledge."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(
            knowledge_dir, "deployment", "# Deployment\nUse Docker Compose for local dev."
        )

        result = await mgr.load_topic_context_on_demand(
            "test-project", "I need to set up docker containers for deployment"
        )
        assert result["success"] is True
        assert "deployment" in result["topics"]
        assert "Docker Compose" in result["context"]
        assert result["has_knowledge"] is True

    async def test_loads_explicit_topics(self, tmp_path):
        """Should load specific topics when topics list is provided."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(
            knowledge_dir, "architecture", "# Architecture\nAsync Python with SQLAlchemy."
        )

        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "",  # text is ignored when topics are provided
            topics=["architecture"],
        )
        assert result["success"] is True
        assert "architecture" in result["topics"]
        assert "Async Python" in result["context"]

    async def test_loads_topic_memories(self, tmp_path):
        """Should load memories with matching topic frontmatter."""
        mgr = _make_manager(storage_root=str(tmp_path))
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        _write_note(notes_dir, "deploy-tip.md", "deployment", "Always check CI first.")

        result = await mgr.load_topic_context_on_demand(
            "test-project", "fix the deployment pipeline"
        )
        assert result["success"] is True
        assert "deployment" in result["topics"]
        assert "Always check CI first" in result["context"]
        assert result["has_memories"] is True

    async def test_loads_both_knowledge_and_memories(self, tmp_path):
        """Should load both knowledge files and memories together."""
        mgr = _make_manager(storage_root=str(tmp_path))

        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(knowledge_dir, "deployment", "# Deployment\nDocker Compose setup.")

        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        _write_note(notes_dir, "deploy-tip.md", "deployment", "Run smoke tests after deploy.")

        result = await mgr.load_topic_context_on_demand(
            "test-project", "fix the deployment pipeline"
        )
        assert result["success"] is True
        assert result["has_knowledge"] is True
        assert result["has_memories"] is True
        assert "Docker Compose" in result["context"]
        assert "smoke tests" in result["context"]

    async def test_excludes_already_loaded_topics(self, tmp_path):
        """Should exclude topics listed in exclude_topics."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(knowledge_dir, "deployment", "# Deployment\nDocker stuff.")

        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "fix the deployment pipeline",
            exclude_topics=["deployment"],
        )
        assert result["success"] is True
        assert result["topics"] == []
        assert result["context"] == ""
        assert "already loaded" in result["message"]

    async def test_returns_empty_when_disabled(self, tmp_path):
        """Should return empty when topic_detection_enabled=False."""
        mgr = _make_manager(storage_root=str(tmp_path), topic_detection_enabled=False)

        result = await mgr.load_topic_context_on_demand("test-project", "deployment pipeline setup")
        assert result["success"] is True
        assert result["topics"] == []
        assert result["context"] == ""
        assert "disabled" in result["message"]

    async def test_returns_empty_for_no_matches(self, tmp_path):
        """Should return empty when no topics match the text."""
        mgr = _make_manager(storage_root=str(tmp_path))

        result = await mgr.load_topic_context_on_demand("test-project", "hello world foo bar")
        assert result["success"] is True
        assert result["topics"] == []
        assert "No topics detected" in result["message"]

    async def test_topics_detected_but_no_content(self, tmp_path):
        """Should report detected topics even if no files are on disk."""
        mgr = _make_manager(storage_root=str(tmp_path))
        # Topics will be detected but no knowledge/memory files exist

        result = await mgr.load_topic_context_on_demand(
            "test-project", "fix the deployment pipeline"
        )
        assert result["success"] is True
        assert "deployment" in result["topics"]
        assert result["context"] == ""
        assert "no matching" in result["message"].lower()

    async def test_handles_errors_gracefully(self, tmp_path):
        """Should return error dict on exception, not raise."""
        mgr = _make_manager(storage_root=str(tmp_path))

        with patch.object(
            mgr, "detect_topics", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ):
            result = await mgr.load_topic_context_on_demand("test-project", "fix the deployment")

        assert result["success"] is False
        assert "boom" in result["error"]

    async def test_context_block_formatting(self, tmp_path):
        """Context block should use the same format as MemoryContext.to_context_block()."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(knowledge_dir, "deployment", "# Deployment\nDocker-based.")

        result = await mgr.load_topic_context_on_demand("test-project", "deployment pipeline")
        assert "## Topic Context (deployment)" in result["context"]
        assert "on-demand" in result["context"]

    async def test_context_block_with_memories_section(self, tmp_path):
        """Context block should include Related Memories subsection."""
        mgr = _make_manager(storage_root=str(tmp_path))
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        _write_note(notes_dir, "tip.md", "deployment", "Use canary releases.")

        result = await mgr.load_topic_context_on_demand("test-project", "deployment pipeline")
        assert "### Related Memories" in result["context"]
        assert "canary releases" in result["context"]

    async def test_explicit_topics_case_insensitive(self, tmp_path):
        """Explicit topics should be normalized to lowercase."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(knowledge_dir, "architecture", "# Architecture\nAsync Python.")

        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "",
            topics=["Architecture", "DEPLOYMENT"],
        )
        assert result["success"] is True
        # Normalized topics should be lowercase
        assert all(t == t.lower() for t in result["topics"])

    async def test_exclude_topics_case_insensitive(self, tmp_path):
        """Exclude topics should work case-insensitively."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(knowledge_dir, "deployment", "# Deployment\nDocker stuff.")

        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "fix the deployment pipeline",
            exclude_topics=["Deployment"],  # uppercase
        )
        assert result["topics"] == []

    async def test_multiple_topics_detected(self, tmp_path):
        """Should detect and load multiple topics from text."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(knowledge_dir, "deployment", "# Deployment\nDocker-based.")
        _write_knowledge(knowledge_dir, "architecture", "# Architecture\nAsync Python.")

        result = await mgr.load_topic_context_on_demand(
            "test-project", "deploy the new architecture changes"
        )
        assert result["success"] is True
        assert "deployment" in result["topics"]
        assert "architecture" in result["topics"]
        assert "Docker-based" in result["context"]
        assert "Async Python" in result["context"]

    async def test_partial_exclude(self, tmp_path):
        """Should only exclude specified topics, keeping others."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(knowledge_dir, "deployment", "# Deployment\nDocker-based.")
        _write_knowledge(knowledge_dir, "architecture", "# Architecture\nAsync Python.")

        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "deploy the new architecture changes",
            exclude_topics=["deployment"],
        )
        assert result["success"] is True
        assert "deployment" not in result["topics"]
        assert "architecture" in result["topics"]
        assert "Docker-based" not in result["context"]
        assert "Async Python" in result["context"]


# ---------------------------------------------------------------------------
# recall_topic_context command (CommandHandler integration)
# ---------------------------------------------------------------------------


class TestRecallTopicContextCommand:
    """Tests for CommandHandler._cmd_recall_topic_context."""

    def _make_handler(self, memory_manager=None):
        """Create a minimal mock CommandHandler for testing."""
        handler = MagicMock()
        handler._active_project_id = "test-project"
        handler.orchestrator = MagicMock()
        handler.orchestrator.memory_manager = memory_manager

        # Import the actual method and bind it
        from src.command_handler import CommandHandler

        handler._cmd_recall_topic_context = CommandHandler._cmd_recall_topic_context.__get__(
            handler
        )
        return handler

    async def test_calls_load_topic_context_on_demand(self, tmp_path):
        """Should delegate to memory_manager.load_topic_context_on_demand()."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(knowledge_dir, "deployment", "# Deployment\nDocker setup.")

        handler = self._make_handler(memory_manager=mgr)
        result = await handler._cmd_recall_topic_context(
            {
                "text": "fix the deployment pipeline",
            }
        )
        assert result["success"] is True
        assert "deployment" in result["topics"]

    async def test_uses_active_project_id(self, tmp_path):
        """Should use active_project_id when project_id not specified."""
        mgr = _make_manager(storage_root=str(tmp_path))
        handler = self._make_handler(memory_manager=mgr)
        handler._active_project_id = "my-project"

        # No knowledge files — just verify it uses the right project
        result = await handler._cmd_recall_topic_context(
            {
                "text": "fix the deployment pipeline",
            }
        )
        assert result["success"] is True

    async def test_explicit_project_id(self, tmp_path):
        """Should use provided project_id over active_project_id."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "other-project", "knowledge")
        _write_knowledge(knowledge_dir, "deployment", "# Deployment\nOther project deploy.")

        handler = self._make_handler(memory_manager=mgr)
        result = await handler._cmd_recall_topic_context(
            {
                "project_id": "other-project",
                "text": "fix the deployment pipeline",
            }
        )
        assert result["success"] is True
        assert "Other project deploy" in result.get("context", "")

    async def test_error_when_no_memory_manager(self):
        """Should return error when memory system is unavailable."""
        handler = self._make_handler(memory_manager=None)
        result = await handler._cmd_recall_topic_context(
            {
                "text": "fix deployment",
            }
        )
        assert "error" in result
        assert "not available" in result["error"]

    async def test_error_when_no_project_id(self):
        """Should return error when no project_id is available."""
        mgr = _make_manager()
        handler = self._make_handler(memory_manager=mgr)
        handler._active_project_id = None

        result = await handler._cmd_recall_topic_context(
            {
                "text": "fix deployment",
            }
        )
        assert "error" in result
        assert "project_id" in result["error"]

    async def test_error_when_no_text_or_topics(self):
        """Should return error when neither text nor topics are provided."""
        mgr = _make_manager()
        handler = self._make_handler(memory_manager=mgr)

        result = await handler._cmd_recall_topic_context({})
        assert "error" in result
        assert "text" in result["error"] or "topics" in result["error"]

    async def test_passes_explicit_topics(self, tmp_path):
        """Should pass explicit topics to load_topic_context_on_demand."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(knowledge_dir, "architecture", "# Architecture\nAsync Python.")

        handler = self._make_handler(memory_manager=mgr)
        result = await handler._cmd_recall_topic_context(
            {
                "topics": ["architecture"],
            }
        )
        assert result["success"] is True
        assert "architecture" in result["topics"]

    async def test_passes_exclude_topics(self, tmp_path):
        """Should pass exclude_topics to load_topic_context_on_demand."""
        mgr = _make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        _write_knowledge(knowledge_dir, "deployment", "# Deployment\nDocker stuff.")

        handler = self._make_handler(memory_manager=mgr)
        result = await handler._cmd_recall_topic_context(
            {
                "text": "deployment pipeline",
                "exclude_topics": ["deployment"],
            }
        )
        assert result["success"] is True
        assert result["topics"] == []


# ---------------------------------------------------------------------------
# Tool definition validation
# ---------------------------------------------------------------------------


class TestRecallTopicContextToolDefinition:
    """Tests for the recall_topic_context tool definition in tool_registry."""

    def test_tool_definition_exists(self):
        """recall_topic_context should be in _ALL_TOOL_DEFINITIONS."""
        from src.tool_registry import _ALL_TOOL_DEFINITIONS

        names = {t["name"] for t in _ALL_TOOL_DEFINITIONS}
        assert "recall_topic_context" in names

    def test_tool_in_memory_category(self):
        """recall_topic_context should be in the memory category."""
        from src.tool_registry import _TOOL_CATEGORIES

        assert _TOOL_CATEGORIES.get("recall_topic_context") == "memory"

    def test_tool_schema_valid(self):
        """Tool definition should have required schema fields."""
        from src.tool_registry import _ALL_TOOL_DEFINITIONS

        tool = next(t for t in _ALL_TOOL_DEFINITIONS if t["name"] == "recall_topic_context")
        assert "description" in tool
        assert "input_schema" in tool
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        props = schema["properties"]
        assert "text" in props
        assert "topics" in props
        assert "exclude_topics" in props
        assert "project_id" in props

    def test_tool_registered_in_registry(self):
        """ToolRegistry should include recall_topic_context."""
        from src.tool_registry import ToolRegistry as _ToolRegistry

        registry = _ToolRegistry()
        all_tools = registry.get_all_tools()
        tool_names = {t["name"] for t in all_tools}
        assert "recall_topic_context" in tool_names
