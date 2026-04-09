"""Tests for L2 topic detection from task context (spec §3).

Verifies that MemoryManager.detect_topics() correctly identifies knowledge
topics from task descriptions, and that build_context() integrates L2 topic
context into the MemoryContext output.
"""

import os
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest  # noqa: F401 — needed for tmp_path fixture

from src.config import MemoryConfig
from src.memory import MemoryManager
from src.models import MemoryContext


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
# detect_topics tests
# ---------------------------------------------------------------------------


class TestDetectTopics:
    """Unit tests for MemoryManager.detect_topics()."""

    def _make_manager(self, storage_root: str = "/tmp/aq-test", **overrides) -> MemoryManager:
        cfg = MemoryConfig(enabled=True, **overrides)
        return MemoryManager(cfg, storage_root=storage_root)

    async def test_detects_exact_topic_name(self, tmp_path):
        """Direct topic name in text should score highest."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        topics = await mgr.detect_topics("test-project", "refactor the deployment pipeline")
        assert "deployment" in topics

    async def test_detects_hyphenated_topic(self, tmp_path):
        """Hyphenated topic names should be detected as substrings."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        topics = await mgr.detect_topics(
            "test-project", "update the api-and-endpoints documentation"
        )
        assert "api-and-endpoints" in topics

    async def test_detects_topic_via_keyword_alias(self, tmp_path):
        """Keywords in TOPIC_KEYWORD_ALIASES should map to the right topic."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        # "docker" is an alias for "deployment"
        topics = await mgr.detect_topics("test-project", "set up docker containers")
        assert "deployment" in topics

    async def test_detects_multiple_topics(self, tmp_path):
        """Text mentioning multiple topics should return all of them."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        topics = await mgr.detect_topics(
            "test-project",
            "deploy the new API endpoint and update the architecture docs",
        )
        assert "deployment" in topics
        assert "api-and-endpoints" in topics
        assert "architecture" in topics

    async def test_respects_max_knowledge_files(self, tmp_path):
        """Should limit results to topic_max_knowledge_files."""
        mgr = self._make_manager(storage_root=str(tmp_path), topic_max_knowledge_files=2)
        topics = await mgr.detect_topics(
            "test-project",
            "deploy the new API endpoint and update the architecture docs with conventions",
        )
        assert len(topics) <= 2

    async def test_returns_empty_when_disabled(self, tmp_path):
        """Should return empty list when topic_detection_enabled is False."""
        mgr = self._make_manager(storage_root=str(tmp_path), topic_detection_enabled=False)
        topics = await mgr.detect_topics("test-project", "deployment pipeline setup")
        assert topics == []

    async def test_returns_empty_for_empty_text(self, tmp_path):
        """Should return empty list for empty/whitespace text."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        assert await mgr.detect_topics("test-project", "") == []
        assert await mgr.detect_topics("test-project", "   ") == []

    async def test_returns_empty_for_no_matches(self, tmp_path):
        """Should return empty list when no topics match."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        topics = await mgr.detect_topics("test-project", "hello world foo bar")
        assert topics == []

    async def test_case_insensitive(self, tmp_path):
        """Topic detection should be case-insensitive."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        topics = await mgr.detect_topics("test-project", "Update the DEPLOYMENT pipeline")
        assert "deployment" in topics

    async def test_includes_extra_disk_topics(self, tmp_path):
        """Topics found on disk but not in config should also be candidates."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        # Create a knowledge dir with an extra topic
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        os.makedirs(knowledge_dir, exist_ok=True)
        with open(os.path.join(knowledge_dir, "authentication.md"), "w") as f:
            f.write("# Authentication\nOAuth2 and JWT patterns.")

        topics = await mgr.detect_topics("test-project", "fix the authentication flow")
        assert "authentication" in topics

    async def test_multi_word_alias_matching(self, tmp_path):
        """Multi-word aliases like 'design pattern' should match as substrings."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        topics = await mgr.detect_topics(
            "test-project", "implement the design pattern for observers"
        )
        assert "architecture" in topics

    async def test_topic_ordering_by_score(self, tmp_path):
        """Topics with more keyword hits should rank higher."""
        mgr = self._make_manager(storage_root=str(tmp_path), topic_max_knowledge_files=5)
        # "deployment" appears as exact match + "docker" alias + "container" alias
        # "architecture" only appears via "refactor" alias
        topics = await mgr.detect_topics(
            "test-project",
            "deployment of docker container, also refactor some code",
        )
        assert topics[0] == "deployment"

    async def test_partial_hyphenated_topic_match(self, tmp_path):
        """Individual words from hyphenated topics should partially match."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        # "api" is a word in "api-and-endpoints" and also a keyword alias
        topics = await mgr.detect_topics("test-project", "build the new api")
        assert "api-and-endpoints" in topics


# ---------------------------------------------------------------------------
# _load_topic_context tests
# ---------------------------------------------------------------------------


class TestLoadTopicContext:
    """Tests for MemoryManager._load_topic_context()."""

    def _make_manager(self, storage_root: str = "/tmp/aq-test", **overrides) -> MemoryManager:
        cfg = MemoryConfig(enabled=True, **overrides)
        return MemoryManager(cfg, storage_root=storage_root)

    async def test_loads_knowledge_files(self, tmp_path):
        """Should load and format knowledge topic files."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        os.makedirs(knowledge_dir, exist_ok=True)

        with open(os.path.join(knowledge_dir, "deployment.md"), "w") as f:
            f.write("# Deployment\nWe use Docker Compose for local dev.")

        result = await mgr._load_topic_context("test-project", ["deployment"])
        assert "### deployment" in result
        assert "Docker Compose" in result

    async def test_strips_frontmatter(self, tmp_path):
        """Should strip YAML frontmatter from knowledge files."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        os.makedirs(knowledge_dir, exist_ok=True)

        with open(os.path.join(knowledge_dir, "architecture.md"), "w") as f:
            f.write(
                "---\ntags: [architecture]\nlast_updated: 2026-04-01\n---\n\n"
                "# Architecture\nAsync-first Python with SQLAlchemy."
            )

        result = await mgr._load_topic_context("test-project", ["architecture"])
        assert "tags:" not in result
        assert "Architecture" in result

    async def test_truncates_large_files(self, tmp_path):
        """Should truncate files exceeding topic_max_chars_per_file."""
        mgr = self._make_manager(storage_root=str(tmp_path), topic_max_chars_per_file=100)
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        os.makedirs(knowledge_dir, exist_ok=True)

        with open(os.path.join(knowledge_dir, "conventions.md"), "w") as f:
            f.write("# Conventions\n" + "x" * 200)

        result = await mgr._load_topic_context("test-project", ["conventions"])
        assert "[truncated]" in result

    async def test_skips_missing_topics(self, tmp_path):
        """Should skip topics that don't have files on disk."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        result = await mgr._load_topic_context("test-project", ["nonexistent"])
        assert result == ""

    async def test_empty_topics_list(self, tmp_path):
        """Should return empty string for empty topics list."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        result = await mgr._load_topic_context("test-project", [])
        assert result == ""

    async def test_multiple_topics(self, tmp_path):
        """Should concatenate multiple topic files."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        os.makedirs(knowledge_dir, exist_ok=True)

        with open(os.path.join(knowledge_dir, "deployment.md"), "w") as f:
            f.write("# Deployment\nDocker-based.")
        with open(os.path.join(knowledge_dir, "conventions.md"), "w") as f:
            f.write("# Conventions\nUse ruff.")

        result = await mgr._load_topic_context("test-project", ["deployment", "conventions"])
        assert "### deployment" in result
        assert "### conventions" in result
        assert "Docker-based" in result
        assert "Use ruff" in result


# ---------------------------------------------------------------------------
# MemoryContext model tests
# ---------------------------------------------------------------------------


class TestMemoryContextTopicTier:
    """Tests for the topic_context field in MemoryContext."""

    def test_topic_context_in_output(self):
        """topic_context should appear in context block."""
        ctx = MemoryContext(
            topic_context="### deployment\nDocker Compose for local dev.",
            detected_topics=["deployment"],
        )
        block = ctx.to_context_block()
        assert "## Topic Context (deployment)" in block
        assert "Docker Compose" in block

    def test_topic_context_ordering(self):
        """topic_context should appear after project_docs and before notes."""
        ctx = MemoryContext(
            profile="My profile",
            project_docs="### CLAUDE.md\nProject conventions",
            topic_context="### deployment\nDocker-based.",
            detected_topics=["deployment"],
            notes="Some notes",
        )
        block = ctx.to_context_block()
        docs_pos = block.index("## Project Documentation")
        topic_pos = block.index("## Topic Context")
        notes_pos = block.index("## Relevant Notes")
        assert docs_pos < topic_pos < notes_pos

    def test_multiple_detected_topics_in_header(self):
        """Header should list all detected topics."""
        ctx = MemoryContext(
            topic_context="### deployment\n...\n\n### architecture\n...",
            detected_topics=["deployment", "architecture"],
        )
        block = ctx.to_context_block()
        assert "## Topic Context (deployment, architecture)" in block

    def test_is_empty_includes_topic_context(self):
        """is_empty should return False when only topic_context is set."""
        ctx = MemoryContext(topic_context="something")
        assert not ctx.is_empty

    def test_empty_topic_context_not_in_output(self):
        """Empty topic_context should not produce a section in output."""
        ctx = MemoryContext(profile="profile text")
        block = ctx.to_context_block()
        assert "Topic Context" not in block

    def test_pre_loaded_message(self):
        """Output should mention pre-loading based on detected topics."""
        ctx = MemoryContext(
            topic_context="### gotchas\nWatch out for None checks.",
            detected_topics=["gotchas"],
        )
        block = ctx.to_context_block()
        assert "pre-loaded based on topics detected" in block


# ---------------------------------------------------------------------------
# build_context integration (L2 tier injection)
# ---------------------------------------------------------------------------


class TestBuildContextTopicIntegration:
    """Tests for L2 topic context injection in build_context()."""

    def _make_manager(self, storage_root: str = "/tmp/aq-test", **overrides) -> MemoryManager:
        cfg = MemoryConfig(enabled=True, index_knowledge=True, **overrides)
        return MemoryManager(cfg, storage_root=storage_root)

    async def test_build_context_injects_topic_context(self, tmp_path):
        """build_context should detect topics and inject L2 content."""
        mgr = self._make_manager(storage_root=str(tmp_path))

        # Create a knowledge topic file
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        os.makedirs(knowledge_dir, exist_ok=True)
        with open(os.path.join(knowledge_dir, "deployment.md"), "w") as f:
            f.write("# Deployment\nUse Docker Compose for local dev.\nCI/CD via GitHub Actions.")

        task = FakeTask(
            title="Fix deployment pipeline",
            description="The Docker build is failing on CI",
        )

        # Mock get_instance to avoid MemSearch dependency
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert ctx.topic_context != ""
        assert "deployment" in ctx.detected_topics
        assert "Docker Compose" in ctx.topic_context

    async def test_build_context_no_topic_when_disabled(self, tmp_path):
        """build_context should skip L2 when topic_detection_enabled=False."""
        mgr = self._make_manager(storage_root=str(tmp_path), topic_detection_enabled=False)

        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        os.makedirs(knowledge_dir, exist_ok=True)
        with open(os.path.join(knowledge_dir, "deployment.md"), "w") as f:
            f.write("# Deployment\nDocker stuff.")

        task = FakeTask(
            title="Fix deployment pipeline",
            description="Docker build failing",
        )

        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert ctx.topic_context == ""
        assert ctx.detected_topics == []

    async def test_build_context_no_topic_when_no_knowledge_dir(self, tmp_path):
        """build_context should gracefully handle missing knowledge dir."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        task = FakeTask(
            title="Fix deployment pipeline",
            description="Docker build failing",
        )

        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert ctx.topic_context == ""

    async def test_build_context_topic_error_does_not_block(self, tmp_path):
        """L2 topic detection errors should not block context building."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        task = FakeTask(
            title="Fix deployment",
            description="Docker broken",
        )

        # Make detect_topics raise an exception
        with (
            patch.object(
                mgr, "detect_topics", new_callable=AsyncMock, side_effect=RuntimeError("boom")
            ),
            patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None),
        ):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        # Should still return a valid context, just without topic content
        assert ctx.topic_context == ""
        assert isinstance(ctx, MemoryContext)

    async def test_build_context_topic_in_full_output(self, tmp_path):
        """Topic context should appear in the assembled context block."""
        mgr = self._make_manager(storage_root=str(tmp_path))

        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        os.makedirs(knowledge_dir, exist_ok=True)
        with open(os.path.join(knowledge_dir, "architecture.md"), "w") as f:
            f.write("# Architecture\nAsync Python with SQLAlchemy Core.")

        task = FakeTask(
            title="Refactor the architecture",
            description="Restructure the module layout for better separation",
        )

        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        block = ctx.to_context_block()
        assert "Topic Context" in block
        assert "architecture" in block
        assert "Async Python" in block


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


class TestTopicDetectionConfig:
    """Tests for topic detection config defaults."""

    def test_default_config(self):
        cfg = MemoryConfig()
        assert cfg.topic_detection_enabled is True
        assert cfg.topic_max_knowledge_files == 3
        assert cfg.topic_max_chars_per_file == 2000

    def test_disable_via_config(self):
        cfg = MemoryConfig(topic_detection_enabled=False)
        assert cfg.topic_detection_enabled is False
