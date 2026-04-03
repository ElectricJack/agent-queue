"""Tests for the auto-generated plan task notification formatters.

Covers both the plain-text ``format_plan_generated()`` and the rich embed
``format_plan_generated_embed()`` functions, plus the helper
``_extract_description_snippet()``.
"""

from __future__ import annotations

import discord
import pytest

from src.models import Task, TaskStatus, TaskType
from src.discord.notifications import (
    format_plan_generated,
    format_plan_generated_embed,
    _extract_description_snippet,
)
from src.discord.embeds import TASK_TYPE_EMOJIS, check_embed_size


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str = "task-001",
    title: str = "Parent Task",
    project_id: str = "my-project",
    description: str = "Build the thing",
    priority: int = 100,
    task_type: TaskType | None = None,
    is_plan_subtask: bool = False,
    parent_task_id: str | None = None,
    requires_approval: bool = False,
) -> Task:
    return Task(
        id=task_id,
        project_id=project_id,
        title=title,
        description=description,
        priority=priority,
        status=TaskStatus.DEFINED,
        task_type=task_type,
        is_plan_subtask=is_plan_subtask,
        parent_task_id=parent_task_id,
        requires_approval=requires_approval,
    )


def _make_subtasks(count: int = 3, parent_id: str = "task-001") -> list[Task]:
    """Create a list of plan subtasks for testing."""
    tasks = []
    for i in range(count):
        tasks.append(
            _make_task(
                task_id=f"sub-{i + 1:03d}",
                title=f"Step {i + 1}: Do thing {i + 1}",
                project_id="my-project",
                description=f"Detailed description for step {i + 1}.",
                priority=100 + i,
                is_plan_subtask=True,
                parent_task_id=parent_id,
            )
        )
    return tasks


# ---------------------------------------------------------------------------
# format_plan_generated (plain text)
# ---------------------------------------------------------------------------


class TestFormatPlanGenerated:
    def test_basic_output_contains_key_info(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        result = format_plan_generated(parent, subs)

        assert "Plan Generated" in result
        assert "3 Tasks Created" in result
        assert "`task-001`" in result
        assert "Parent Task" in result
        assert "`my-project`" in result

    def test_single_task_singular(self):
        parent = _make_task()
        subs = _make_subtasks(1)
        result = format_plan_generated(parent, subs)

        assert "1 Task Created" in result
        # Should not say "Tasks"
        assert "1 Tasks" not in result

    def test_chain_shown_when_chained(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        result = format_plan_generated(parent, subs, chained=True)

        assert "→" in result
        assert "`sub-001`" in result
        assert "`sub-003`" in result

    def test_chain_hidden_when_not_chained(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        result = format_plan_generated(parent, subs, chained=False)

        assert "→" not in result

    def test_workspace_shown_when_provided(self):
        parent = _make_task()
        subs = _make_subtasks(2)
        result = format_plan_generated(parent, subs, workspace_path="/home/user/workspace")

        assert "`/home/user/workspace`" in result

    def test_workspace_hidden_when_none(self):
        parent = _make_task()
        subs = _make_subtasks(2)
        result = format_plan_generated(parent, subs, workspace_path=None)

        assert "Workspace" not in result

    def test_task_type_emoji_shown(self):
        parent = _make_task()
        subs = [
            _make_task(
                task_id="sub-001",
                title="Fix the bug",
                task_type=TaskType.BUGFIX,
                is_plan_subtask=True,
            )
        ]
        result = format_plan_generated(parent, subs)

        assert TASK_TYPE_EMOJIS["bugfix"] in result

    def test_all_subtask_ids_listed(self):
        parent = _make_task()
        subs = _make_subtasks(5)
        result = format_plan_generated(parent, subs)

        for sub in subs:
            assert f"`{sub.id}`" in result

    def test_priority_shown_per_task(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        result = format_plan_generated(parent, subs)

        assert "priority: 100" in result
        assert "priority: 101" in result
        assert "priority: 102" in result


# ---------------------------------------------------------------------------
# format_plan_generated_embed (rich embed)
# ---------------------------------------------------------------------------


class TestFormatPlanGeneratedEmbed:
    def test_returns_discord_embed(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        embed = format_plan_generated_embed(parent, subs)

        assert isinstance(embed, discord.Embed)

    def test_embed_title_contains_count(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        embed = format_plan_generated_embed(parent, subs)

        assert "3 New Tasks" in embed.title

    def test_embed_title_singular(self):
        parent = _make_task()
        subs = _make_subtasks(1)
        embed = format_plan_generated_embed(parent, subs)

        assert "1 New Task" in embed.title
        assert "Tasks" not in embed.title

    def test_embed_description_mentions_parent(self):
        parent = _make_task()
        subs = _make_subtasks(2)
        embed = format_plan_generated_embed(parent, subs)

        assert "task-001" in embed.description
        assert "completed with an implementation plan" in embed.description

    def test_embed_has_parent_task_field(self):
        parent = _make_task()
        subs = _make_subtasks(2)
        embed = format_plan_generated_embed(parent, subs)

        field_names = [f.name for f in embed.fields]
        assert "Parent Task" in field_names

        parent_field = next(f for f in embed.fields if f.name == "Parent Task")
        assert "task-001" in parent_field.value

    def test_embed_has_project_field(self):
        parent = _make_task()
        subs = _make_subtasks(2)
        embed = format_plan_generated_embed(parent, subs)

        field_names = [f.name for f in embed.fields]
        assert "Project" in field_names

        project_field = next(f for f in embed.fields if f.name == "Project")
        assert "my-project" in project_field.value

    def test_embed_has_workspace_field_when_provided(self):
        parent = _make_task()
        subs = _make_subtasks(2)
        embed = format_plan_generated_embed(
            parent, subs, workspace_path="/home/user/projects/myapp"
        )

        field_names = [f.name for f in embed.fields]
        assert "Workspace" in field_names

    def test_embed_no_workspace_field_when_none(self):
        parent = _make_task()
        subs = _make_subtasks(2)
        embed = format_plan_generated_embed(parent, subs, workspace_path=None)

        field_names = [f.name for f in embed.fields]
        assert "Workspace" not in field_names

    def test_embed_has_subtask_separator(self):
        parent = _make_task()
        subs = _make_subtasks(2)
        embed = format_plan_generated_embed(parent, subs)

        field_names = [f.name for f in embed.fields]
        assert any("Subtasks" in name for name in field_names)

    def test_embed_has_step_fields(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        embed = format_plan_generated_embed(parent, subs)

        field_names = [f.name for f in embed.fields]
        assert any("Step 1/3" in name for name in field_names)
        assert any("Step 2/3" in name for name in field_names)
        assert any("Step 3/3" in name for name in field_names)

    def test_step_fields_contain_task_ids(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        embed = format_plan_generated_embed(parent, subs)

        # Find step fields (skip header fields and separator)
        step_fields = [f for f in embed.fields if f.name.startswith("Step") or "Step" in f.name]
        for idx, field in enumerate(step_fields):
            expected_id = f"sub-{idx + 1:03d}"
            assert expected_id in field.value

    def test_step_fields_contain_priority(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        embed = format_plan_generated_embed(parent, subs)

        step_fields = [f for f in embed.fields if "Step" in f.name]
        for idx, field in enumerate(step_fields):
            assert str(100 + idx) in field.value

    def test_chained_tasks_show_dependency(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        embed = format_plan_generated_embed(parent, subs, chained=True)

        # Step 2 should depend on step 1
        step2_fields = [f for f in embed.fields if "Step 2" in f.name]
        assert len(step2_fields) == 1
        assert "Depends on" in step2_fields[0].value
        assert "sub-001" in step2_fields[0].value

        # Step 1 should NOT have a dependency
        step1_fields = [f for f in embed.fields if "Step 1" in f.name]
        assert len(step1_fields) == 1
        assert "Depends on" not in step1_fields[0].value

    def test_unchained_tasks_no_dependency(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        embed = format_plan_generated_embed(parent, subs, chained=False)

        step_fields = [f for f in embed.fields if "Step" in f.name]
        for field in step_fields:
            assert "Depends on" not in field.value

    def test_chain_diagram_in_description(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        embed = format_plan_generated_embed(parent, subs, chained=True)

        assert "Execution order" in embed.description
        assert "→" in embed.description

    def test_no_chain_diagram_when_unchained(self):
        parent = _make_task()
        subs = _make_subtasks(3)
        embed = format_plan_generated_embed(parent, subs, chained=False)

        assert "Execution order" not in embed.description

    def test_task_type_emoji_in_step_field(self):
        parent = _make_task()
        sub = _make_task(
            task_id="sub-001",
            title="Add tests",
            task_type=TaskType.TEST,
            is_plan_subtask=True,
        )
        embed = format_plan_generated_embed(parent, [sub])

        step_fields = [f for f in embed.fields if "Step" in f.name]
        assert len(step_fields) == 1
        assert TASK_TYPE_EMOJIS["test"] in step_fields[0].name

    def test_approval_flag_shown(self):
        parent = _make_task()
        sub = _make_task(
            task_id="sub-001",
            title="Deploy to prod",
            requires_approval=True,
            is_plan_subtask=True,
        )
        embed = format_plan_generated_embed(parent, [sub])

        step_fields = [f for f in embed.fields if "Step" in f.name]
        assert "Requires approval" in step_fields[0].value

    def test_embed_color_is_teal(self):
        parent = _make_task()
        subs = _make_subtasks(2)
        embed = format_plan_generated_embed(parent, subs)

        # Teal color = 0x1ABC9C
        assert embed.color.value == 0x1ABC9C

    def test_embed_within_size_limits(self):
        """Ensure embed doesn't exceed Discord's 6000-char total limit."""
        parent = _make_task(
            title="A" * 200,
            description="B" * 500,
        )
        subs = []
        for i in range(5):  # Max steps
            subs.append(
                _make_task(
                    task_id=f"sub-{i + 1:03d}",
                    title=f"Long step title {'X' * 100}",
                    description=f"Very detailed description {'Y' * 300} for step {i + 1}.",
                    priority=100 + i,
                    task_type=TaskType.FEATURE,
                    is_plan_subtask=True,
                    requires_approval=(i == 4),
                )
            )
        embed = format_plan_generated_embed(
            parent, subs, workspace_path="/very/long/workspace/path/here"
        )

        is_valid, total_chars = check_embed_size(embed)
        assert is_valid, f"Embed exceeds 6000 chars: {total_chars}"

    def test_embed_has_timestamp(self):
        parent = _make_task()
        subs = _make_subtasks(1)
        embed = format_plan_generated_embed(parent, subs)

        assert embed.timestamp is not None

    def test_embed_has_footer(self):
        parent = _make_task()
        subs = _make_subtasks(1)
        embed = format_plan_generated_embed(parent, subs)

        assert embed.footer is not None
        assert "AgentQueue" in embed.footer.text

    def test_workspace_path_truncated_for_long_paths(self):
        parent = _make_task()
        subs = _make_subtasks(1)
        long_path = "/home/user/very/deep/nested/workspace/directory/project"
        embed = format_plan_generated_embed(parent, subs, workspace_path=long_path)

        ws_field = next(f for f in embed.fields if f.name == "Workspace")
        # Should be truncated to last 2 components
        assert "directory/project" in ws_field.value
        assert "…/" in ws_field.value

    def test_description_snippet_shown_for_subtasks(self):
        parent = _make_task()
        sub = _make_task(
            task_id="sub-001",
            title="Implement feature",
            description="This implements the core authentication flow using JWT tokens.",
            is_plan_subtask=True,
        )
        embed = format_plan_generated_embed(parent, [sub])

        step_fields = [f for f in embed.fields if "Step" in f.name]
        assert "authentication flow" in step_fields[0].value


# ---------------------------------------------------------------------------
# _extract_description_snippet
# ---------------------------------------------------------------------------


class TestExtractDescriptionSnippet:
    def test_returns_first_substantive_line(self):
        desc = "# Header\n\nThis is the real content."
        assert _extract_description_snippet(desc) == "This is the real content."

    def test_skips_blank_lines(self):
        desc = "\n\n\nActual content here."
        assert _extract_description_snippet(desc) == "Actual content here."

    def test_skips_headers(self):
        desc = "# Title\n## Section\nReal text."
        assert _extract_description_snippet(desc) == "Real text."

    def test_skips_horizontal_rules(self):
        desc = "---\nContent after rule."
        assert _extract_description_snippet(desc) == "Content after rule."

    def test_skips_boilerplate_prefixes(self):
        desc = "Parent task: blah\nPlan context: stuff\nActual description."
        assert _extract_description_snippet(desc) == "Actual description."

    def test_truncates_long_lines(self):
        desc = "A" * 200
        result = _extract_description_snippet(desc, max_len=50)
        assert len(result) == 50
        assert result.endswith("…")

    def test_returns_empty_for_only_headers(self):
        desc = "# Title\n## Section\n### Subsection"
        assert _extract_description_snippet(desc) == ""

    def test_returns_empty_for_empty_string(self):
        assert _extract_description_snippet("") == ""

    def test_short_line_not_truncated(self):
        desc = "Short line."
        assert _extract_description_snippet(desc) == "Short line."
