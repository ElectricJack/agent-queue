"""Tests for override injection into the agent's system prompt.

Roadmap 3.2.3 — injects override content alongside the base profile
in the agent's system prompt.  The override is loaded from
``vault/projects/{project_id}/overrides/{agent_type}.md`` and placed
right after the L0 role (base profile) in the prompt assembly order.

See ``docs/specs/design/memory-scoping.md`` §5 for the override model spec.

Tests cover:
- Loading override files from the vault filesystem
- Graceful handling when no override exists
- Graceful handling for unreadable / empty files
- Integration with PromptBuilder.set_override_content()
- End-to-end: override content appears in the final prompt
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_OVERRIDE = textwrap.dedent("""\
    ---
    tags: [override, coding, mech-fighters]
    agent_type: coding
    ---

    # Coding Agent Overrides — Mech Fighters

    This project uses a custom ECS framework. Do not use inheritance for
    game entities — always use composition via the component system.

    Prefer integration tests that spin up the full game loop over unit
    tests of individual components. The component system has too many
    implicit interactions for isolated unit tests to catch real bugs.
""")


# ---------------------------------------------------------------------------
# Minimal stubs for orchestrator testing
# ---------------------------------------------------------------------------


@dataclass
class FakeConfig:
    data_dir: str
    vault_root: str


@dataclass
class FakeProfile:
    id: str
    system_prompt_suffix: str = "You are a coding agent."


@dataclass
class FakeTask:
    id: str = "task-001"
    project_id: str = "mech-fighters"
    description: str = "Implement combat system."
    branch_name: str = ""
    is_plan_subtask: bool = False
    parent_task_id: str | None = None
    requires_approval: bool = False
    attachments: list | None = None


@dataclass
class FakeProject:
    id: str = "mech-fighters"
    name: str = "Mech Fighters"
    repo_url: str = ""
    repo_default_branch: str = "main"


# ---------------------------------------------------------------------------
# _load_project_override tests
# ---------------------------------------------------------------------------


class TestLoadProjectOverride:
    """Test Orchestrator._load_project_override()."""

    def _make_orchestrator_stub(self, vault_root: str):
        """Create a minimal orchestrator-like object with _load_project_override."""
        # Import the actual method and bind it to a stub
        from src.orchestrator import Orchestrator

        stub = MagicMock(spec=Orchestrator)
        stub.config = FakeConfig(data_dir=vault_root, vault_root=vault_root)
        # Bind the real method to the stub
        stub._load_project_override = Orchestrator._load_project_override.__get__(stub, type(stub))
        return stub

    @pytest.mark.asyncio
    async def test_loads_existing_override(self, tmp_path):
        """Returns file content when override file exists."""
        vault_root = str(tmp_path)
        override_dir = tmp_path / "projects" / "mech-fighters" / "overrides"
        override_dir.mkdir(parents=True)
        (override_dir / "coding.md").write_text(SAMPLE_OVERRIDE, encoding="utf-8")

        stub = self._make_orchestrator_stub(vault_root)
        result = await stub._load_project_override("mech-fighters", "coding")

        assert result is not None
        assert "custom ECS framework" in result
        assert "composition via the component system" in result

    @pytest.mark.asyncio
    async def test_returns_none_when_no_file(self, tmp_path):
        """Returns None when override file does not exist."""
        vault_root = str(tmp_path)
        stub = self._make_orchestrator_stub(vault_root)
        result = await stub._load_project_override("mech-fighters", "coding")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_file(self, tmp_path):
        """Returns None when override file is empty."""
        vault_root = str(tmp_path)
        override_dir = tmp_path / "projects" / "mech-fighters" / "overrides"
        override_dir.mkdir(parents=True)
        (override_dir / "coding.md").write_text("", encoding="utf-8")

        stub = self._make_orchestrator_stub(vault_root)
        result = await stub._load_project_override("mech-fighters", "coding")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_whitespace_only_file(self, tmp_path):
        """Returns None when override file contains only whitespace."""
        vault_root = str(tmp_path)
        override_dir = tmp_path / "projects" / "mech-fighters" / "overrides"
        override_dir.mkdir(parents=True)
        (override_dir / "coding.md").write_text("   \n\n  ", encoding="utf-8")

        stub = self._make_orchestrator_stub(vault_root)
        result = await stub._load_project_override("mech-fighters", "coding")
        assert result is None

    @pytest.mark.asyncio
    async def test_different_agent_types(self, tmp_path):
        """Loads correct file for different agent types."""
        vault_root = str(tmp_path)
        override_dir = tmp_path / "projects" / "my-app" / "overrides"
        override_dir.mkdir(parents=True)
        (override_dir / "coding.md").write_text("# Coding overrides", encoding="utf-8")
        (override_dir / "reviewer.md").write_text("# Reviewer overrides", encoding="utf-8")

        stub = self._make_orchestrator_stub(vault_root)

        coding = await stub._load_project_override("my-app", "coding")
        reviewer = await stub._load_project_override("my-app", "reviewer")

        assert coding is not None and "Coding overrides" in coding
        assert reviewer is not None and "Reviewer overrides" in reviewer

    @pytest.mark.asyncio
    async def test_different_projects(self, tmp_path):
        """Loads correct file for different projects."""
        vault_root = str(tmp_path)
        for proj in ("proj-a", "proj-b"):
            d = tmp_path / "projects" / proj / "overrides"
            d.mkdir(parents=True)
            (d / "coding.md").write_text(f"# Overrides for {proj}", encoding="utf-8")

        stub = self._make_orchestrator_stub(vault_root)

        a = await stub._load_project_override("proj-a", "coding")
        b = await stub._load_project_override("proj-b", "coding")

        assert a is not None and "proj-a" in a
        assert b is not None and "proj-b" in b

    @pytest.mark.asyncio
    async def test_handles_read_error_gracefully(self, tmp_path):
        """Returns None and logs warning on read error."""
        vault_root = str(tmp_path)
        override_dir = tmp_path / "projects" / "mech-fighters" / "overrides"
        override_dir.mkdir(parents=True)
        override_file = override_dir / "coding.md"
        override_file.write_text(SAMPLE_OVERRIDE, encoding="utf-8")

        stub = self._make_orchestrator_stub(vault_root)

        # Patch open to raise an error
        with patch("builtins.open", side_effect=OSError("permission denied")):
            result = await stub._load_project_override("mech-fighters", "coding")

        assert result is None


# ---------------------------------------------------------------------------
# End-to-end: override injection via PromptBuilder
# ---------------------------------------------------------------------------


class TestOverrideInjectionEndToEnd:
    """Test override content flowing from file → PromptBuilder → final prompt."""

    def test_override_appears_in_prompt_after_l0_role(self, tmp_path):
        """Override content is injected between L0 role and context blocks."""
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder(prompts_dir=tmp_path)
        builder.set_l0_role("You are a coding agent.")
        builder.set_override_content(SAMPLE_OVERRIDE)
        builder.add_context("task", "## Task\nImplement combat system.")

        prompt = builder.build_task_prompt()

        # Override content (frontmatter stripped) is present
        assert "Coding Agent Overrides" in prompt
        assert "custom ECS framework" in prompt
        assert "composition via the component system" in prompt

        # Frontmatter is stripped
        assert "tags: [override" not in prompt
        assert "agent_type: coding" not in prompt

        # Ordering: L0 role → override → task
        role_pos = prompt.index("You are a coding agent.")
        override_pos = prompt.index("custom ECS framework")
        task_pos = prompt.index("Implement combat system.")
        assert role_pos < override_pos < task_pos

    def test_no_override_means_no_extra_section(self, tmp_path):
        """When no override exists, the prompt has no override section."""
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder(prompts_dir=tmp_path)
        builder.set_l0_role("You are a coding agent.")
        builder.add_context("task", "## Task\nFix a bug.")

        prompt = builder.build_task_prompt()

        # Only L0 role + task, no override layer
        sections = prompt.split("\n\n---\n\n")
        assert len(sections) == 2
        assert "coding agent" in sections[0]
        assert "Fix a bug" in sections[1]

    def test_override_without_frontmatter(self, tmp_path):
        """Override file without frontmatter works fine."""
        from src.prompt_builder import PromptBuilder

        override_md = "# My Overrides\n\nAlways use tabs, never spaces."

        builder = PromptBuilder(prompts_dir=tmp_path)
        builder.set_l0_role("You are a coding agent.")
        builder.set_override_content(override_md)
        builder.add_context("task", "## Task\nFix formatting.")

        prompt = builder.build_task_prompt()

        assert "Always use tabs, never spaces." in prompt
        # Three sections: role, override, task
        sections = prompt.split("\n\n---\n\n")
        assert len(sections) == 3
