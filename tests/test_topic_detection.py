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
# _load_topic_memories tests (L2 topic-filtered memories, spec §2)
# ---------------------------------------------------------------------------


class TestLoadTopicMemories:
    """Tests for MemoryManager._load_topic_memories()."""

    def _make_manager(self, storage_root: str = "/tmp/aq-test", **overrides) -> MemoryManager:
        cfg = MemoryConfig(enabled=True, **overrides)
        return MemoryManager(cfg, storage_root=storage_root)

    def _write_note(self, notes_dir: str, filename: str, topic: str, body: str) -> str:
        """Helper: write a note file with topic frontmatter."""
        os.makedirs(notes_dir, exist_ok=True)
        path = os.path.join(notes_dir, filename)
        with open(path, "w") as f:
            f.write(f"---\ntopic: {topic}\ntags: [insight]\n---\n\n{body}")
        return path

    async def test_loads_matching_memories(self, tmp_path):
        """Should load memory files whose topic matches detected topics."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        self._write_note(notes_dir, "deploy-tip.md", "deployment", "Always check CI first.")

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        assert "Always check CI first" in result
        assert "deploy-tip.md" in result

    async def test_ignores_non_matching_topic(self, tmp_path):
        """Should skip files whose topic doesn't match."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        self._write_note(notes_dir, "auth-note.md", "authentication", "Use OAuth2.")

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        assert result == ""

    async def test_ignores_files_without_topic(self, tmp_path):
        """Should skip files that have no topic field in frontmatter."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        os.makedirs(notes_dir, exist_ok=True)
        with open(os.path.join(notes_dir, "no-topic.md"), "w") as f:
            f.write("---\ntags: [note]\n---\n\nJust a plain note.")

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        assert result == ""

    async def test_ignores_files_without_frontmatter(self, tmp_path):
        """Should skip files that have no YAML frontmatter at all."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        os.makedirs(notes_dir, exist_ok=True)
        with open(os.path.join(notes_dir, "plain.md"), "w") as f:
            f.write("# Just a heading\nNo frontmatter here.")

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        assert result == ""

    async def test_case_insensitive_topic_match(self, tmp_path):
        """Topic matching should be case-insensitive."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        self._write_note(notes_dir, "deploy-tip.md", "Deployment", "Use blue-green deploys.")

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        assert "blue-green" in result

    async def test_multiple_topics(self, tmp_path):
        """Should match files for any of the detected topics."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        self._write_note(notes_dir, "deploy.md", "deployment", "Docker tip.")
        self._write_note(notes_dir, "arch.md", "architecture", "Use composition.")

        result = await mgr._load_topic_memories("test-project", ["deployment", "architecture"])
        assert "Docker tip" in result
        assert "Use composition" in result

    async def test_budget_truncation(self, tmp_path):
        """Should truncate to topic_memory_budget_chars."""
        mgr = self._make_manager(storage_root=str(tmp_path), topic_memory_budget_chars=50)
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        self._write_note(notes_dir, "long.md", "deployment", "x" * 200)

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        assert "[truncated]" in result

    async def test_max_results_limit(self, tmp_path):
        """Should respect topic_memory_max_results."""
        mgr = self._make_manager(storage_root=str(tmp_path), topic_memory_max_results=2)
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        for i in range(5):
            self._write_note(notes_dir, f"note-{i}.md", "deployment", f"Tip {i}.")

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        # Count the file references — should be at most 2
        assert result.count("**note-") <= 2

    async def test_returns_empty_when_disabled(self, tmp_path):
        """Should return empty when topic_memory_enabled is False."""
        mgr = self._make_manager(storage_root=str(tmp_path), topic_memory_enabled=False)
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        self._write_note(notes_dir, "tip.md", "deployment", "A tip.")

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        assert result == ""

    async def test_returns_empty_for_empty_topics(self, tmp_path):
        """Should return empty for empty topics list."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        result = await mgr._load_topic_memories("test-project", [])
        assert result == ""

    async def test_scans_staging_dir(self, tmp_path):
        """Should also scan the memory staging directory."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        staging_dir = os.path.join(str(tmp_path), "memory", "test-project", "staging")
        os.makedirs(staging_dir, exist_ok=True)
        with open(os.path.join(staging_dir, "staged.md"), "w") as f:
            f.write("---\ntopic: deployment\n---\n\nStaged insight about deploys.")

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        assert "Staged insight" in result

    async def test_scans_memory_root_files(self, tmp_path):
        """Should scan top-level .md files in the memory dir."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        memory_dir = os.path.join(str(tmp_path), "memory", "test-project")
        os.makedirs(memory_dir, exist_ok=True)
        with open(os.path.join(memory_dir, "insight.md"), "w") as f:
            f.write("---\ntopic: architecture\n---\n\nPrefer composition over inheritance.")

        result = await mgr._load_topic_memories("test-project", ["architecture"])
        assert "composition" in result

    async def test_excludes_profile_and_factsheet(self, tmp_path):
        """Should skip profile.md and factsheet.md."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        memory_dir = os.path.join(str(tmp_path), "memory", "test-project")
        os.makedirs(memory_dir, exist_ok=True)
        with open(os.path.join(memory_dir, "profile.md"), "w") as f:
            f.write("---\ntopic: architecture\n---\n\nProject profile.")
        with open(os.path.join(memory_dir, "factsheet.md"), "w") as f:
            f.write("---\ntopic: architecture\n---\n\nProject factsheet.")

        result = await mgr._load_topic_memories("test-project", ["architecture"])
        assert result == ""

    async def test_strips_frontmatter_from_output(self, tmp_path):
        """Output should not contain YAML frontmatter."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        self._write_note(notes_dir, "tip.md", "deployment", "Use canary releases.")

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        assert "topic:" not in result
        assert "tags:" not in result
        assert "Use canary releases" in result

    async def test_newest_files_first(self, tmp_path):
        """Should order results with newest files first."""
        import time

        mgr = self._make_manager(storage_root=str(tmp_path))
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        self._write_note(notes_dir, "old.md", "deployment", "Old tip.")
        time.sleep(0.05)
        self._write_note(notes_dir, "new.md", "deployment", "New tip.")

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        # New should appear before old
        new_pos = result.index("New tip")
        old_pos = result.index("Old tip")
        assert new_pos < old_pos


# ---------------------------------------------------------------------------
# MemoryContext model tests (topic_memories field)
# ---------------------------------------------------------------------------


class TestMemoryContextTopicMemories:
    """Tests for the topic_memories field in MemoryContext."""

    def test_topic_memories_in_output(self):
        """topic_memories should appear in context block."""
        ctx = MemoryContext(
            topic_memories="- **deploy-tip.md:** Always check CI first.",
            detected_topics=["deployment"],
        )
        block = ctx.to_context_block()
        assert "Related Memories" in block
        assert "Always check CI first" in block

    def test_topic_memories_combined_with_context(self):
        """Both topic_context and topic_memories should appear in the L2 section."""
        ctx = MemoryContext(
            topic_context="### deployment\nDocker Compose for local dev.",
            topic_memories="- **tip.md:** Always run smoke tests after deploy.",
            detected_topics=["deployment"],
        )
        block = ctx.to_context_block()
        assert "## Topic Context (deployment)" in block
        assert "Docker Compose" in block
        assert "Related Memories" in block
        assert "smoke tests" in block

    def test_topic_memories_only_no_knowledge(self):
        """L2 section should render with only topic_memories (no knowledge files)."""
        ctx = MemoryContext(
            topic_memories="- **arch.md:** Use composition over inheritance.",
            detected_topics=["architecture"],
        )
        block = ctx.to_context_block()
        assert "## Topic Context (architecture)" in block
        assert "composition over inheritance" in block

    def test_is_empty_includes_topic_memories(self):
        """is_empty should return False when only topic_memories is set."""
        ctx = MemoryContext(topic_memories="something")
        assert not ctx.is_empty

    def test_empty_topic_memories_not_in_output(self):
        """Empty topic_memories should not produce a Related Memories section."""
        ctx = MemoryContext(profile="profile text")
        block = ctx.to_context_block()
        assert "Related Memories" not in block


# ---------------------------------------------------------------------------
# build_context integration (topic_memories)
# ---------------------------------------------------------------------------


class TestBuildContextTopicMemoriesIntegration:
    """Tests for L2 topic-filtered memory injection in build_context()."""

    def _make_manager(self, storage_root: str = "/tmp/aq-test", **overrides) -> MemoryManager:
        cfg = MemoryConfig(enabled=True, index_knowledge=True, **overrides)
        return MemoryManager(cfg, storage_root=storage_root)

    async def test_build_context_injects_topic_memories(self, tmp_path):
        """build_context should load memories with matching topic frontmatter."""
        mgr = self._make_manager(storage_root=str(tmp_path))

        # Create a note with topic frontmatter
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        os.makedirs(notes_dir, exist_ok=True)
        with open(os.path.join(notes_dir, "deploy-insight.md"), "w") as f:
            f.write("---\ntopic: deployment\n---\n\nAlways validate config before deploy.")

        task = FakeTask(
            title="Fix deployment pipeline",
            description="The Docker build is failing on CI",
        )

        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert ctx.topic_memories != ""
        assert "deployment" in ctx.detected_topics
        assert "validate config" in ctx.topic_memories

    async def test_build_context_no_memories_when_disabled(self, tmp_path):
        """build_context should skip topic memories when disabled."""
        mgr = self._make_manager(storage_root=str(tmp_path), topic_memory_enabled=False)

        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        os.makedirs(notes_dir, exist_ok=True)
        with open(os.path.join(notes_dir, "tip.md"), "w") as f:
            f.write("---\ntopic: deployment\n---\n\nSome tip.")

        task = FakeTask(
            title="Fix deployment pipeline",
            description="Docker build failing",
        )

        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert ctx.topic_memories == ""

    async def test_build_context_both_knowledge_and_memories(self, tmp_path):
        """build_context should load both knowledge files and topic memories."""
        mgr = self._make_manager(storage_root=str(tmp_path))

        # Knowledge file
        knowledge_dir = os.path.join(str(tmp_path), "memory", "test-project", "knowledge")
        os.makedirs(knowledge_dir, exist_ok=True)
        with open(os.path.join(knowledge_dir, "deployment.md"), "w") as f:
            f.write("# Deployment\nUse Docker Compose for local dev.")

        # Note with topic
        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        os.makedirs(notes_dir, exist_ok=True)
        with open(os.path.join(notes_dir, "deploy-insight.md"), "w") as f:
            f.write("---\ntopic: deployment\n---\n\nRun smoke tests after deploy.")

        task = FakeTask(
            title="Fix deployment pipeline",
            description="Docker build failing",
        )

        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert "Docker Compose" in ctx.topic_context
        assert "smoke tests" in ctx.topic_memories
        assert "deployment" in ctx.detected_topics

    async def test_build_context_topic_memory_error_does_not_block(self, tmp_path):
        """Errors in topic memory loading should not block context building."""
        mgr = self._make_manager(storage_root=str(tmp_path))
        task = FakeTask(
            title="Fix deployment",
            description="Docker broken",
        )

        with (
            patch.object(
                mgr,
                "_load_topic_memories",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
            patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None),
        ):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        # Should still return a valid context
        assert ctx.topic_memories == ""
        assert isinstance(ctx, MemoryContext)

    async def test_build_context_topic_memories_in_full_output(self, tmp_path):
        """Topic memories should appear in the assembled context block."""
        mgr = self._make_manager(storage_root=str(tmp_path))

        notes_dir = os.path.join(str(tmp_path), "vault", "projects", "test-project", "notes")
        os.makedirs(notes_dir, exist_ok=True)
        with open(os.path.join(notes_dir, "arch-insight.md"), "w") as f:
            f.write("---\ntopic: architecture\n---\n\nPrefer async-first patterns for all I/O.")

        task = FakeTask(
            title="Refactor the architecture",
            description="Restructure the module layout",
        )

        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        block = ctx.to_context_block()
        assert "Topic Context" in block
        assert "Related Memories" in block
        assert "async-first" in block


# ---------------------------------------------------------------------------
# _parse_frontmatter_topic tests
# ---------------------------------------------------------------------------


class TestParseFrontmatterTopic:
    """Tests for MemoryManager._parse_frontmatter_topic()."""

    def _make_manager(self, storage_root: str = "/tmp/aq-test") -> MemoryManager:
        cfg = MemoryConfig(enabled=True)
        return MemoryManager(cfg, storage_root=storage_root)

    def test_extracts_topic(self):
        mgr = self._make_manager()
        content = "---\ntopic: deployment\ntags: [note]\n---\n\nBody."
        assert mgr._parse_frontmatter_topic(content) == "deployment"

    def test_normalizes_case(self):
        mgr = self._make_manager()
        content = "---\ntopic: Architecture\n---\n\nBody."
        assert mgr._parse_frontmatter_topic(content) == "architecture"

    def test_returns_none_without_frontmatter(self):
        mgr = self._make_manager()
        content = "# Just a heading\nNo frontmatter."
        assert mgr._parse_frontmatter_topic(content) is None

    def test_returns_none_without_topic_field(self):
        mgr = self._make_manager()
        content = "---\ntags: [note]\n---\n\nBody."
        assert mgr._parse_frontmatter_topic(content) is None

    def test_returns_none_for_empty_topic(self):
        mgr = self._make_manager()
        content = "---\ntopic: \n---\n\nBody."
        assert mgr._parse_frontmatter_topic(content) is None

    def test_returns_none_for_malformed_yaml(self):
        mgr = self._make_manager()
        content = "---\n: : invalid yaml [[\n---\n\nBody."
        assert mgr._parse_frontmatter_topic(content) is None


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

    def test_topic_memory_defaults(self):
        cfg = MemoryConfig()
        assert cfg.topic_memory_enabled is True
        assert cfg.topic_memory_budget_chars == 2000
        assert cfg.topic_memory_max_results == 5

    def test_disable_via_config(self):
        cfg = MemoryConfig(topic_detection_enabled=False)
        assert cfg.topic_detection_enabled is False

    def test_disable_topic_memory_via_config(self):
        cfg = MemoryConfig(topic_memory_enabled=False)
        assert cfg.topic_memory_enabled is False
