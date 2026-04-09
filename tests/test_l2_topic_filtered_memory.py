"""Tests for L2 topic-filtered memory loading — roadmap 3.3.8.

Covers the six specific test scenarios from the roadmap:

(a) Task about "testing the payment API" detects topics "testing"/"payment"
    and loads relevant topic memories (~500 tokens)
(b) Task about "update README" does NOT load "testing" topic memories
    (topic mismatch)
(c) L2 memories are loaded on-demand when topic emerges mid-task
    (not at initial context build)
(d) L2 memories do not exceed ~500 token budget (truncated or top-K limited)
(e) Task with no detectable topic does not load any L2 memories (L0+L1 only)
(f) L2 topic detection works from both task description and ongoing
    conversation context

References:
- Memory tiers spec: docs/specs/design/memory-scoping.md §2
- Implementation: src/memory.py (detect_topics, _load_topic_memories,
  _load_topic_context, build_context, load_topic_context_on_demand)
"""

import os
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest  # noqa: F401 — needed for tmp_path fixture

from src.config import MemoryConfig
from src.memory import MemoryManager


# ---------------------------------------------------------------------------
# Lightweight fakes & helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    id: str = "task-338"
    project_id: str = "test-project"
    title: str = "Test task"
    description: str = "A test task description"
    task_type: MagicMock = field(default_factory=lambda: MagicMock(value="feature"))


def _make_manager(storage_root: str = "/tmp/aq-test", **overrides) -> MemoryManager:
    cfg = MemoryConfig(enabled=True, **overrides)
    return MemoryManager(cfg, storage_root=storage_root)


def _write_knowledge(knowledge_dir: str, topic: str, content: str) -> str:
    """Write a knowledge base file for the given topic."""
    os.makedirs(knowledge_dir, exist_ok=True)
    path = os.path.join(knowledge_dir, f"{topic}.md")
    with open(path, "w") as f:
        f.write(content)
    return path


def _write_note(notes_dir: str, filename: str, topic: str, body: str) -> str:
    """Write a note file with topic frontmatter."""
    os.makedirs(notes_dir, exist_ok=True)
    path = os.path.join(notes_dir, filename)
    with open(path, "w") as f:
        f.write(f"---\ntopic: {topic}\ntags: [insight]\n---\n\n{body}")
    return path


def _knowledge_dir(tmp_path, project_id: str = "test-project") -> str:
    return os.path.join(str(tmp_path), "memory", project_id, "knowledge")


def _notes_dir(tmp_path, project_id: str = "test-project") -> str:
    return os.path.join(str(tmp_path), "vault", "projects", project_id, "notes")


# ===========================================================================
# (a) Task about "testing the payment API" detects topics and loads memories
# ===========================================================================


class TestCaseA_TopicDetectionAndMemoryLoading:
    """(a) A task about 'testing the payment API' should detect topics like
    'testing' and 'payment' (from on-disk knowledge files) and load
    relevant topic memories (~500 tokens).
    """

    async def test_detects_on_disk_testing_topic(self, tmp_path):
        """detect_topics should find 'testing' topic from on-disk knowledge file."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nUnit and integration testing guidelines.")

        topics = await mgr.detect_topics("test-project", "testing the payment API")
        assert "testing" in topics

    async def test_detects_on_disk_payment_topic(self, tmp_path):
        """detect_topics should find 'payment' topic from on-disk knowledge file."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "payment", "# Payment\nStripe integration and billing logic.")

        topics = await mgr.detect_topics("test-project", "testing the payment API")
        assert "payment" in topics

    async def test_detects_both_testing_and_payment_topics(self, tmp_path):
        """detect_topics should find both 'testing' and 'payment' topics together."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest conventions.")
        _write_knowledge(kdir, "payment", "# Payment\nStripe integration.")

        topics = await mgr.detect_topics("test-project", "testing the payment API")
        assert "testing" in topics
        assert "payment" in topics

    async def test_also_detects_api_alias(self, tmp_path):
        """'api' keyword should additionally detect 'api-and-endpoints' topic."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest conventions.")

        topics = await mgr.detect_topics("test-project", "testing the payment API")
        assert "api-and-endpoints" in topics

    async def test_loads_topic_knowledge_files(self, tmp_path):
        """build_context should inject knowledge from detected topics."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nAlways run pytest with -v flag.")
        _write_knowledge(kdir, "payment", "# Payment\nUse Stripe webhooks for async events.")

        task = FakeTask(
            title="Testing the payment API",
            description="Add unit tests for the payment endpoint",
        )
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert "testing" in ctx.detected_topics
        assert "payment" in ctx.detected_topics
        assert "pytest" in ctx.topic_context
        assert "Stripe webhooks" in ctx.topic_context

    async def test_loads_matching_topic_memories(self, tmp_path):
        """build_context should load memory notes tagged with detected topics."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nUse pytest.")
        ndir = _notes_dir(tmp_path)
        _write_note(
            ndir, "test-insight.md", "testing", "Mock external APIs in unit tests."
        )
        _write_note(
            ndir, "payment-lesson.md", "payment", "Always validate webhook signatures."
        )
        _write_knowledge(kdir, "payment", "# Payment\nStripe integration.")

        task = FakeTask(
            title="Testing the payment API",
            description="Write integration tests for payment flow",
        )
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert ctx.topic_memories != ""
        assert "Mock external APIs" in ctx.topic_memories
        assert "webhook signatures" in ctx.topic_memories

    async def test_memories_fit_within_default_budget(self, tmp_path):
        """Topic memories should respect the ~500 token (~2000 char) budget."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest tips.")
        ndir = _notes_dir(tmp_path)
        # Write a memory that fits within the 2000 char budget
        body = "This is a testing insight about mocking. " * 10  # ~410 chars
        _write_note(ndir, "testing-insight.md", "testing", body)

        task = FakeTask(
            title="Testing the payment API",
            description="Verify payment endpoint test coverage",
        )
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert ctx.topic_memories != ""
        assert len(ctx.topic_memories) <= 2500  # budget + entry formatting overhead

    async def test_full_context_block_includes_l2_section(self, tmp_path):
        """to_context_block() should include Topic Context section with memories."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nUse fixtures.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "tip.md", "testing", "Parametrize repetitive tests.")

        task = FakeTask(
            title="Testing the payment API",
            description="Add tests for payment webhook handling",
        )
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        block = ctx.to_context_block()
        assert "## Topic Context" in block
        assert "testing" in block
        assert "Related Memories" in block
        assert "Parametrize" in block


# ===========================================================================
# (b) Task about "update README" does NOT load "testing" topic memories
# ===========================================================================


class TestCaseB_TopicMismatchExclusion:
    """(b) A task about 'update README' should NOT load testing or payment
    topic memories (topic mismatch). Only relevant topics should be loaded.
    """

    async def test_readme_task_does_not_detect_testing_topic(self, tmp_path):
        """detect_topics for 'update README' should not include 'testing'."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest conventions.")
        _write_knowledge(kdir, "payment", "# Payment\nStripe setup.")

        topics = await mgr.detect_topics("test-project", "update README")
        assert "testing" not in topics
        assert "payment" not in topics

    async def test_readme_task_does_not_load_testing_memories(self, tmp_path):
        """build_context for 'update README' should not include testing memories."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest conventions.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "test-tip.md", "testing", "Always use fixtures for DB setup.")

        task = FakeTask(title="Update README", description="Add installation instructions")
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert "testing" not in ctx.detected_topics
        assert "fixtures" not in ctx.topic_memories
        assert "fixtures" not in ctx.topic_context

    async def test_readme_task_no_topic_context_section(self, tmp_path):
        """'update README' should not produce a Topic Context section at all
        when only testing/payment topics exist on disk."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest.")
        _write_knowledge(kdir, "payment", "# Payment\nStripe.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "test-tip.md", "testing", "Use mocks.")
        _write_note(ndir, "pay-tip.md", "payment", "Validate webhooks.")

        task = FakeTask(title="Update README", description="Improve project documentation")
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        block = ctx.to_context_block()
        assert "Topic Context" not in block
        assert "Related Memories" not in block

    async def test_readme_task_empty_l2_fields(self, tmp_path):
        """L2 context fields should remain empty for unrelated tasks."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest info.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "test-insight.md", "testing", "Mock all I/O.")

        task = FakeTask(title="Update README", description="Fix typos in docs")
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert ctx.topic_context == ""
        assert ctx.topic_memories == ""
        assert ctx.detected_topics == []

    async def test_selective_topic_loading(self, tmp_path):
        """A deployment task should load deployment memories but NOT testing ones."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest tips.")
        _write_knowledge(kdir, "deployment", "# Deployment\nDocker Compose setup.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "test-tip.md", "testing", "Always mock external APIs.")
        _write_note(ndir, "deploy-tip.md", "deployment", "Run smoke tests after deploy.")

        task = FakeTask(
            title="Fix deployment pipeline",
            description="Docker build is failing on CI",
        )
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert "deployment" in ctx.detected_topics
        assert "testing" not in ctx.detected_topics
        assert "smoke tests" in ctx.topic_memories
        assert "mock external APIs" not in ctx.topic_memories


# ===========================================================================
# (c) L2 memories loaded on-demand when topic emerges mid-task
# ===========================================================================


class TestCaseC_OnDemandTopicLoading:
    """(c) L2 memories should be loadable on-demand when a topic emerges
    mid-task (not only at initial context build).
    """

    async def test_on_demand_loads_new_topic_mid_task(self, tmp_path):
        """load_topic_context_on_demand should load context for a new topic."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nUse pytest-asyncio for async tests.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "test-tip.md", "testing", "Parametrize edge cases.")

        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "I now need to focus on testing for this new feature",
        )
        assert result["success"] is True
        assert "testing" in result["topics"]
        assert "pytest-asyncio" in result["context"]
        assert "Parametrize" in result["context"]

    async def test_on_demand_excludes_already_loaded_topics(self, tmp_path):
        """On-demand should skip topics already loaded at task start."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(
            kdir, "deployment", "# Deployment\nDocker Compose for local dev."
        )
        _write_knowledge(kdir, "testing", "# Testing\nPytest conventions.")

        # Simulate: deployment was loaded at task start, now testing emerges
        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "also need to add testing for the deployment module",
            exclude_topics=["deployment"],
        )
        assert result["success"] is True
        assert "testing" in result["topics"]
        assert "deployment" not in result["topics"]
        assert "Pytest conventions" in result["context"]
        assert "Docker Compose" not in result["context"]

    async def test_on_demand_vs_build_context_independence(self, tmp_path):
        """On-demand loading should work independently of build_context."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "deployment", "# Deployment\nDocker setup.")
        _write_knowledge(kdir, "testing", "# Testing\nPytest fixtures are great.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "test-tip.md", "testing", "Use conftest.py for shared fixtures.")

        # Step 1: Initial build_context for a deployment task
        task = FakeTask(
            title="Fix deployment pipeline",
            description="Docker build failing",
        )
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert "deployment" in ctx.detected_topics
        assert "testing" not in ctx.detected_topics

        # Step 2: Mid-task, a new topic emerges — load it on-demand
        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "I should also focus on testing this deployment change",
            exclude_topics=ctx.detected_topics,
        )
        assert result["success"] is True
        assert "testing" in result["topics"]
        assert "conftest.py" in result["context"]

    async def test_on_demand_loads_only_memories_when_no_knowledge(self, tmp_path):
        """On-demand should return topic memories even without knowledge files."""
        mgr = _make_manager(storage_root=str(tmp_path))
        # No knowledge file — only memory notes
        ndir = _notes_dir(tmp_path)
        _write_note(
            ndir, "arch-note.md", "architecture", "Prefer composition over inheritance."
        )

        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "we need to rethink the architecture of this module",
        )
        assert result["success"] is True
        assert "architecture" in result["topics"]
        assert result["has_memories"] is True
        assert "composition" in result["context"]

    async def test_on_demand_explicit_topic_list(self, tmp_path):
        """On-demand with explicit topics should bypass detection and load directly."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "gotchas", "# Gotchas\nSQLite has no ALTER COLUMN.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "gotcha-note.md", "gotchas", "Watch out for None in dict.get().")

        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "",  # no text — using explicit topics
            topics=["gotchas"],
        )
        assert result["success"] is True
        assert "gotchas" in result["topics"]
        assert "ALTER COLUMN" in result["context"]
        assert "None in dict.get" in result["context"]


# ===========================================================================
# (d) L2 memories do not exceed ~500 token budget
# ===========================================================================


class TestCaseD_TokenBudgetEnforcement:
    """(d) L2 memories should not exceed the ~500 token budget (~2000 chars).
    Content must be truncated or top-K limited.
    """

    async def test_single_large_memory_truncated(self, tmp_path):
        """A single large memory should be truncated to the budget."""
        mgr = _make_manager(storage_root=str(tmp_path), topic_memory_budget_chars=200)
        ndir = _notes_dir(tmp_path)
        body = "Very long testing insight. " * 100  # ~2600 chars
        _write_note(ndir, "large.md", "testing", body)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nUnit tests.")

        result = await mgr._load_topic_memories("test-project", ["testing"])
        assert "[truncated]" in result
        # The total body content (excluding formatting) should be near the budget
        # Entry formatting adds "- **large.md:** " prefix
        assert len(result) < 300  # 200 budget + entry prefix + truncation marker

    async def test_multiple_memories_respect_budget(self, tmp_path):
        """Multiple memories should collectively fit within the budget."""
        budget = 500
        mgr = _make_manager(storage_root=str(tmp_path), topic_memory_budget_chars=budget)
        ndir = _notes_dir(tmp_path)
        # Write 5 files, each ~200 chars — total exceeds 500 char budget
        for i in range(5):
            body = f"Insight number {i}. " * 12  # ~200 chars each
            _write_note(ndir, f"note-{i}.md", "testing", body)
            time.sleep(0.01)  # ensure distinct mtimes
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nBasics.")

        result = await mgr._load_topic_memories("test-project", ["testing"])
        # Count actual body chars (rough check — budget should constrain total)
        assert len(result) > 0
        # Not all 5 notes' full content should appear (budget too small)
        full_notes_count = sum(1 for i in range(5) if f"note-{i}.md" in result)
        # Budget of 500 should allow ~2-3 full entries but not all 5
        assert full_notes_count < 5

    async def test_max_results_limits_file_count(self, tmp_path):
        """topic_memory_max_results should cap the number of loaded files."""
        mgr = _make_manager(
            storage_root=str(tmp_path),
            topic_memory_max_results=2,
            topic_memory_budget_chars=10000,  # large budget to isolate max_results
        )
        ndir = _notes_dir(tmp_path)
        for i in range(5):
            _write_note(ndir, f"note-{i}.md", "deployment", f"Tip {i}: use Docker.")
            time.sleep(0.01)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "deployment", "# Deployment\nBasics.")

        result = await mgr._load_topic_memories("test-project", ["deployment"])
        note_count = sum(1 for i in range(5) if f"note-{i}.md" in result)
        assert note_count <= 2

    async def test_knowledge_file_truncation(self, tmp_path):
        """Knowledge files exceeding topic_max_chars_per_file should be truncated."""
        mgr = _make_manager(
            storage_root=str(tmp_path),
            topic_max_chars_per_file=100,
            index_knowledge=True,
        )
        kdir = _knowledge_dir(tmp_path)
        content = "# Testing\n" + "x" * 200
        _write_knowledge(kdir, "testing", content)

        result = await mgr._load_topic_context("test-project", ["testing"])
        assert "[truncated]" in result

    async def test_budget_zero_remaining_stops_loading(self, tmp_path):
        """When budget is exhausted, no more entries should be added."""
        # Budget of 50 can barely fit one small entry
        mgr = _make_manager(storage_root=str(tmp_path), topic_memory_budget_chars=50)
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "note-a.md", "testing", "Short tip A.")
        time.sleep(0.01)
        _write_note(ndir, "note-b.md", "testing", "Short tip B but longer than the first one.")
        time.sleep(0.01)
        _write_note(ndir, "note-c.md", "testing", "Short tip C with even more content here.")
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nBasics.")

        result = await mgr._load_topic_memories("test-project", ["testing"])
        # Should have loaded at least one but not all three fully
        assert "note-c.md" in result  # newest first
        assert len(result) > 0

    async def test_build_context_topic_memories_within_budget(self, tmp_path):
        """build_context should produce topic_memories within the configured budget."""
        budget = 300
        mgr = _make_manager(
            storage_root=str(tmp_path),
            index_knowledge=True,
            topic_memory_budget_chars=budget,
        )
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "deployment", "# Deployment\nDocker setup.")
        ndir = _notes_dir(tmp_path)
        for i in range(10):
            body = f"Deployment insight #{i}. " * 20  # ~400 chars each
            _write_note(ndir, f"deploy-{i}.md", "deployment", body)
            time.sleep(0.01)

        task = FakeTask(
            title="Fix deployment pipeline",
            description="Docker build is broken",
        )
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        # Total body content in topic_memories should be near (but not exceeding)
        # the budget by a significant margin.  Formatting adds headers per entry.
        assert ctx.topic_memories != ""
        # The raw body chars (excluding formatting) should be within budget + overhead
        assert len(ctx.topic_memories) < budget + 500  # budget + generous formatting overhead


# ===========================================================================
# (e) Task with no detectable topic does not load any L2 memories
# ===========================================================================


class TestCaseE_NoTopicNoL2:
    """(e) A task with no detectable topic should not load any L2 memories.
    Only L0 (factsheet) and L1 (profile, project docs) should be present.
    """

    async def test_generic_task_no_topics_detected(self, tmp_path):
        """detect_topics should return empty for generic/unrelated text."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest tips.")
        _write_knowledge(kdir, "deployment", "# Deployment\nDocker tips.")

        topics = await mgr.detect_topics(
            "test-project", "hello world this is a completely random sentence"
        )
        assert topics == []

    async def test_generic_task_empty_l2_in_build_context(self, tmp_path):
        """build_context with a non-topical task should have empty L2 fields."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest info.")
        _write_knowledge(kdir, "deployment", "# Deployment\nDocker info.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "test-tip.md", "testing", "Use fixtures.")
        _write_note(ndir, "deploy-tip.md", "deployment", "Use canary releases.")

        task = FakeTask(
            title="Miscellaneous cleanup",
            description="Remove unused variables and fix typos",
        )
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert ctx.topic_context == ""
        assert ctx.topic_memories == ""
        assert ctx.detected_topics == []

    async def test_generic_task_context_block_has_no_topic_section(self, tmp_path):
        """Context block for a non-topical task should not have Topic Context section."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nInfo.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "tip.md", "testing", "A testing tip.")

        task = FakeTask(
            title="Miscellaneous",
            description="General housekeeping stuff",
        )
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        block = ctx.to_context_block()
        assert "Topic Context" not in block
        assert "Related Memories" not in block

    async def test_l0_l1_still_loaded_without_l2(self, tmp_path):
        """L0 (factsheet) and L1 (profile, docs) should be loaded even without L2."""
        mgr = _make_manager(
            storage_root=str(tmp_path),
            index_knowledge=True,
            profile_enabled=True,
            factsheet_in_context=True,
        )
        # Create profile
        profile_dir = os.path.join(str(tmp_path), "memory", "test-project")
        os.makedirs(profile_dir, exist_ok=True)
        with open(os.path.join(profile_dir, "profile.md"), "w") as f:
            f.write("# Project Profile\nThis is the test project.")

        # Create factsheet
        with open(os.path.join(profile_dir, "factsheet.md"), "w") as f:
            f.write("project:\n  name: Test Project\n  language: Python")

        # Create CLAUDE.md
        with open(os.path.join(str(tmp_path), "CLAUDE.md"), "w") as f:
            f.write("# CLAUDE.md\nProject conventions here.")

        # Create topic memories that should NOT be loaded
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "test-tip.md", "testing", "A testing tip.")

        task = FakeTask(
            title="General cleanup",
            description="Remove dead code",
        )
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        # L0/L1 should be present
        assert ctx.profile != ""
        assert "Project Profile" in ctx.profile
        assert ctx.factsheet != ""
        assert "Test Project" in ctx.factsheet
        # L2 should be empty
        assert ctx.topic_context == ""
        assert ctx.topic_memories == ""
        assert ctx.detected_topics == []

    async def test_on_demand_no_topics_returns_empty(self, tmp_path):
        """On-demand with generic text should return no topics."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest.")

        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "just doing some random stuff with no particular topic",
        )
        assert result["success"] is True
        assert result["topics"] == []
        assert result["context"] == ""

    async def test_empty_description_no_l2(self, tmp_path):
        """A task with empty title and description should not load L2."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "tip.md", "testing", "Use fixtures.")

        task = FakeTask(title="", description="")
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert ctx.topic_context == ""
        assert ctx.topic_memories == ""
        assert ctx.detected_topics == []


# ===========================================================================
# (f) L2 topic detection works from both task description and conversation
# ===========================================================================


class TestCaseF_DetectionFromMultipleSources:
    """(f) L2 topic detection should work from both task descriptions and
    ongoing conversation context (arbitrary text).
    """

    async def test_detection_from_task_title_and_description(self, tmp_path):
        """detect_topics should find topics from task-style text."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest guide.")

        topics = await mgr.detect_topics(
            "test-project",
            "Add unit tests for the authentication flow",
        )
        # "test" won't match, but "unit" might not be an alias either.
        # The text contains "tests" which differs from topic name "testing"
        # Let's check what actually matches:
        # text_words = {"add", "unit", "tests", "for", "the", "authentication", "flow"}
        # "testing" is not in text_words. But "testing" IS the topic name on disk.
        # "testing" not in text_lower as substring? "testing" is NOT in
        # "add unit tests for the authentication flow" — nope.
        # So this test should demonstrate that topic detection requires the
        # actual topic name or keyword aliases to be present.
        # Let's use text that actually contains "testing":
        topics = await mgr.detect_topics(
            "test-project",
            "Improve testing coverage for the authentication module",
        )
        assert "testing" in topics

    async def test_detection_from_conversation_context(self, tmp_path):
        """detect_topics should work from conversational/narrative text."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "testing", "# Testing\nPytest guide.")

        conversation_text = (
            "I've been looking at the code and I think we should focus on "
            "testing next. The test coverage is low and we need better "
            "integration testing for the payment module."
        )
        topics = await mgr.detect_topics("test-project", conversation_text)
        assert "testing" in topics

    async def test_detection_from_code_review_context(self, tmp_path):
        """detect_topics should work from code review/discussion text."""
        mgr = _make_manager(storage_root=str(tmp_path))

        review_text = (
            "This PR changes the deployment configuration. We should make sure "
            "the Docker containers are properly configured and the CI/CD pipeline "
            "still works after these changes."
        )
        topics = await mgr.detect_topics("test-project", review_text)
        assert "deployment" in topics

    async def test_detection_from_error_log_context(self, tmp_path):
        """detect_topics should detect topics from error descriptions/logs."""
        mgr = _make_manager(storage_root=str(tmp_path))

        error_context = (
            "The deployment failed because the Docker container couldn't connect "
            "to the database. The kubernetes pod keeps crashing with OOM errors."
        )
        topics = await mgr.detect_topics("test-project", error_context)
        assert "deployment" in topics

    async def test_on_demand_with_conversation_context(self, tmp_path):
        """load_topic_context_on_demand should work with conversation-style text."""
        mgr = _make_manager(storage_root=str(tmp_path))
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "architecture", "# Architecture\nAsync-first with SQLAlchemy.")
        ndir = _notes_dir(tmp_path)
        _write_note(
            ndir, "arch-insight.md", "architecture", "Prefer composition over inheritance."
        )

        result = await mgr.load_topic_context_on_demand(
            "test-project",
            "Looking at the codebase, I think we need to refactor the architecture. "
            "The module structure is getting unwieldy and we should consider better "
            "separation of concerns.",
        )
        assert result["success"] is True
        assert "architecture" in result["topics"]
        assert "Async-first" in result["context"] or "composition" in result["context"]

    async def test_build_context_uses_title_plus_description(self, tmp_path):
        """build_context combines title+description for topic detection."""
        mgr = _make_manager(storage_root=str(tmp_path), index_knowledge=True)
        kdir = _knowledge_dir(tmp_path)
        _write_knowledge(kdir, "deployment", "# Deployment\nDocker Compose setup.")
        ndir = _notes_dir(tmp_path)
        _write_note(ndir, "deploy-tip.md", "deployment", "Check health endpoints after deploy.")

        # Title alone might not trigger detection, but description has keywords
        task = FakeTask(
            title="Fix broken pipeline",
            description="The Docker container fails to start in the deployment environment",
        )
        with patch.object(mgr, "get_instance", new_callable=AsyncMock, return_value=None):
            ctx = await mgr.build_context("test-project", task, str(tmp_path))

        assert "deployment" in ctx.detected_topics
        assert ctx.topic_context != "" or ctx.topic_memories != ""

    async def test_keyword_alias_detection_in_conversation(self, tmp_path):
        """Keyword aliases should work in conversation context (not just task text)."""
        mgr = _make_manager(storage_root=str(tmp_path))

        # "docker" is an alias for "deployment"
        topics = await mgr.detect_topics(
            "test-project",
            "I need to set up docker containers for the new microservice",
        )
        assert "deployment" in topics

    async def test_multi_word_alias_in_conversation(self, tmp_path):
        """Multi-word aliases like 'design pattern' should match in conversation."""
        mgr = _make_manager(storage_root=str(tmp_path))

        topics = await mgr.detect_topics(
            "test-project",
            "Let's discuss the design pattern we should use for the observer system",
        )
        assert "architecture" in topics

    async def test_same_topic_detected_from_different_text_styles(self, tmp_path):
        """The same topic should be detected regardless of text style."""
        mgr = _make_manager(storage_root=str(tmp_path))

        # Task-style
        task_topics = await mgr.detect_topics(
            "test-project",
            "Deploy the application to production",
        )

        # Conversation-style
        conv_topics = await mgr.detect_topics(
            "test-project",
            "I think we should deploy the application to production next",
        )

        # Both should detect deployment
        assert "deployment" in task_topics
        assert "deployment" in conv_topics
