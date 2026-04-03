"""Tests for the prompt template manager.

Covers:
  - YAML frontmatter parsing
  - Template loading from files
  - Variable schema parsing
  - Mustache-style {{variable}} rendering
  - PromptManager listing, filtering, and searching
  - Category and tag filtering
  - Edge cases (no frontmatter, invalid YAML, missing files)
"""

import os
import textwrap

import pytest

from src.prompt_manager import (
    CATEGORIES,
    PromptManager,
    PromptTemplate,
    PromptVariable,
    load_template,
    parse_frontmatter,
    render_template,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def prompts_dir(tmp_path):
    """Create a temporary prompts directory with subdirectories."""
    for cat in CATEGORIES:
        (tmp_path / cat).mkdir()
    (tmp_path / "_examples").mkdir()
    return tmp_path


@pytest.fixture
def sample_template_content():
    """Standard template content with frontmatter and body."""
    return textwrap.dedent("""\
        ---
        name: test-prompt
        description: A test prompt for unit testing
        category: task
        variables:
          - name: task_title
            description: The title of the task
            required: true
          - name: workspace_path
            description: Path to workspace
            required: false
            default: /tmp/workspace
        tags: [test, example]
        version: 2
        author: tester
        ---

        # {{task_title}}

        Working in `{{workspace_path}}`.

        Do the thing.
    """)


@pytest.fixture
def sample_template_file(prompts_dir, sample_template_content):
    """Write a sample template file and return its path."""
    fpath = prompts_dir / "task" / "test-prompt.md"
    fpath.write_text(sample_template_content)
    return fpath


@pytest.fixture
def populated_prompts_dir(prompts_dir):
    """Create a prompts directory with multiple templates across categories."""
    # System template
    (prompts_dir / "system" / "chat-agent.md").write_text(
        textwrap.dedent("""\
        ---
        name: chat-agent
        description: System prompt for the chat bot
        category: system
        variables:
          - name: workspace_dir
            description: Root workspace directory
            required: true
        tags: [system, chat]
        version: 1
        ---

        You are AgentQueue. Workspaces: {{workspace_dir}}
    """)
    )

    # Task templates
    (prompts_dir / "task" / "plan-generation.md").write_text(
        textwrap.dedent("""\
        ---
        name: plan-generation
        description: Prompt for generating implementation plans
        category: task
        variables:
          - name: task_title
            description: Task title
            required: true
          - name: max_steps
            description: Maximum steps
            required: false
            default: "4"
        tags: [task, planning]
        version: 1
        ---

        # Task: {{task_title}}

        Generate a plan with up to {{max_steps}} phases.
    """)
    )

    (prompts_dir / "task" / "execution.md").write_text(
        textwrap.dedent("""\
        ---
        name: execution
        description: Prompt for direct task execution
        category: task
        tags: [task, execution]
        version: 1
        ---

        Execute the task directly. No planning.
    """)
    )

    # Hooks template
    (prompts_dir / "hooks" / "test-watcher.md").write_text(
        textwrap.dedent("""\
        ---
        name: test-watcher
        description: Hook for watching test results
        category: hooks
        variables:
          - name: step_0
            description: Test output
            required: true
        tags: [hooks, testing]
        version: 1
        ---

        Test results:
        ```
        {{step_0}}
        ```
    """)
    )

    # README should be skipped
    (prompts_dir / "README.md").write_text("# Prompts\nThis is a README.")

    return prompts_dir


@pytest.fixture
def manager(populated_prompts_dir):
    """PromptManager with populated templates."""
    return PromptManager(str(populated_prompts_dir))


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_valid_frontmatter(self):
        content = "---\nname: test\ndescription: hello\n---\nBody here."
        fm, body = parse_frontmatter(content)
        assert fm["name"] == "test"
        assert fm["description"] == "hello"
        assert body == "Body here."

    def test_no_frontmatter(self):
        content = "Just plain markdown content."
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_empty_frontmatter(self):
        content = "---\n\n---\nBody after empty frontmatter."
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == "Body after empty frontmatter."

    def test_complex_frontmatter(self):
        content = textwrap.dedent("""\
            ---
            name: complex
            variables:
              - name: var1
                required: true
              - name: var2
                default: hello
            tags: [a, b, c]
            ---
            Body.
        """)
        fm, body = parse_frontmatter(content)
        assert fm["name"] == "complex"
        assert len(fm["variables"]) == 2
        assert fm["tags"] == ["a", "b", "c"]

    def test_invalid_yaml(self):
        content = "---\n{{invalid: yaml: [}\n---\nBody."
        fm, body = parse_frontmatter(content)
        # Should fall back gracefully
        assert fm == {}

    def test_frontmatter_not_dict(self):
        content = "---\n- just a list\n- not a dict\n---\nBody."
        fm, body = parse_frontmatter(content)
        assert fm == {}


# ---------------------------------------------------------------------------
# load_template
# ---------------------------------------------------------------------------


class TestLoadTemplate:
    def test_load_valid_template(self, sample_template_file):
        tmpl = load_template(str(sample_template_file))
        assert tmpl is not None
        assert tmpl.name == "test-prompt"
        assert tmpl.description == "A test prompt for unit testing"
        assert tmpl.category == "task"
        assert tmpl.version == 2
        assert tmpl.author == "tester"
        assert len(tmpl.variables) == 2
        assert tmpl.tags == ["test", "example"]
        assert "{{task_title}}" in tmpl.body
        assert tmpl.size_bytes > 0
        assert tmpl.file_name == "test-prompt.md"

    def test_load_template_infers_name_from_filename(self, prompts_dir):
        fpath = prompts_dir / "custom" / "my-prompt.md"
        fpath.write_text("---\ndescription: no name field\n---\nBody.")
        tmpl = load_template(str(fpath))
        assert tmpl is not None
        assert tmpl.name == "my-prompt"

    def test_load_template_infers_category_from_directory(self, prompts_dir):
        fpath = prompts_dir / "hooks" / "auto-review.md"
        fpath.write_text("---\nname: auto-review\n---\nReview things.")
        tmpl = load_template(str(fpath))
        assert tmpl is not None
        assert tmpl.category == "hooks"

    def test_load_template_without_frontmatter(self, prompts_dir):
        fpath = prompts_dir / "custom" / "plain.md"
        fpath.write_text("# Just Markdown\n\nNo frontmatter here.")
        tmpl = load_template(str(fpath))
        assert tmpl is not None
        assert tmpl.name == "plain"
        assert tmpl.body == "# Just Markdown\n\nNo frontmatter here."

    def test_load_nonexistent_file(self):
        tmpl = load_template("/nonexistent/path.md")
        assert tmpl is None

    def test_variable_parsing(self, sample_template_file):
        tmpl = load_template(str(sample_template_file))
        assert len(tmpl.variables) == 2

        task_var = tmpl.variables[0]
        assert task_var.name == "task_title"
        assert task_var.required is True
        assert task_var.default == ""

        ws_var = tmpl.variables[1]
        assert ws_var.name == "workspace_path"
        assert ws_var.required is False
        assert ws_var.default == "/tmp/workspace"

    def test_required_variables_property(self, sample_template_file):
        tmpl = load_template(str(sample_template_file))
        req = tmpl.required_variables
        assert len(req) == 1
        assert req[0].name == "task_title"

    def test_optional_variables_property(self, sample_template_file):
        tmpl = load_template(str(sample_template_file))
        opt = tmpl.optional_variables
        assert len(opt) == 1
        assert opt[0].name == "workspace_path"

    def test_to_dict(self, sample_template_file):
        tmpl = load_template(str(sample_template_file))
        d = tmpl.to_dict()
        assert d["name"] == "test-prompt"
        assert d["category"] == "task"
        assert len(d["variables"]) == 2
        assert d["variables"][0]["name"] == "task_title"
        assert d["variables"][0]["required"] is True


# ---------------------------------------------------------------------------
# render_template
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def test_basic_rendering(self, sample_template_file):
        tmpl = load_template(str(sample_template_file))
        result = render_template(tmpl, {"task_title": "Fix Bug #42"})
        assert "# Fix Bug #42" in result
        # workspace_path should use default
        assert "`/tmp/workspace`" in result

    def test_all_variables_provided(self, sample_template_file):
        tmpl = load_template(str(sample_template_file))
        result = render_template(
            tmpl,
            {
                "task_title": "My Task",
                "workspace_path": "/home/dev/project",
            },
        )
        assert "# My Task" in result
        assert "`/home/dev/project`" in result

    def test_missing_optional_uses_default(self, sample_template_file):
        tmpl = load_template(str(sample_template_file))
        result = render_template(tmpl, {"task_title": "Test"})
        assert "/tmp/workspace" in result

    def test_missing_required_leaves_placeholder(self, sample_template_file):
        tmpl = load_template(str(sample_template_file))
        result = render_template(tmpl, {})
        assert "{{task_title}}" in result

    def test_strict_mode_raises_on_missing_required(self, sample_template_file):
        tmpl = load_template(str(sample_template_file))
        with pytest.raises(ValueError, match="Required variable 'task_title'"):
            render_template(tmpl, {}, strict=True)

    def test_extra_variables_are_substituted(self, sample_template_file):
        """Variables not declared in schema can still be substituted."""
        tmpl = load_template(str(sample_template_file))
        # Add an undeclared variable to the body
        tmpl.body += "\n{{extra_var}}"
        result = render_template(
            tmpl,
            {
                "task_title": "Test",
                "extra_var": "EXTRA_VALUE",
            },
        )
        assert "EXTRA_VALUE" in result

    def test_no_variables_returns_body_unchanged(self, prompts_dir):
        fpath = prompts_dir / "custom" / "static.md"
        fpath.write_text("---\nname: static\n---\nNo variables here.")
        tmpl = load_template(str(fpath))
        result = render_template(tmpl)
        assert result == "No variables here."


# ---------------------------------------------------------------------------
# PromptManager
# ---------------------------------------------------------------------------


class TestPromptManager:
    def test_list_all_templates(self, manager):
        templates = manager.list_templates()
        names = [t.name for t in templates]
        assert "chat-agent" in names
        assert "plan-generation" in names
        assert "execution" in names
        assert "test-watcher" in names
        assert len(templates) == 4

    def test_list_skips_readme(self, manager):
        templates = manager.list_templates()
        names = [t.file_name for t in templates]
        assert "README.md" not in names

    def test_filter_by_category(self, manager):
        task_templates = manager.list_templates(category="task")
        assert all(t.category == "task" for t in task_templates)
        assert len(task_templates) == 2

        system_templates = manager.list_templates(category="system")
        assert all(t.category == "system" for t in system_templates)
        assert len(system_templates) == 1

    def test_filter_by_tag(self, manager):
        planning = manager.list_templates(tag="planning")
        assert len(planning) == 1
        assert planning[0].name == "plan-generation"

        testing = manager.list_templates(tag="testing")
        assert len(testing) == 1
        assert testing[0].name == "test-watcher"

    def test_get_template_by_name(self, manager):
        tmpl = manager.get_template("chat-agent")
        assert tmpl is not None
        assert tmpl.name == "chat-agent"
        assert tmpl.category == "system"

    def test_get_template_by_filename(self, manager):
        tmpl = manager.get_template("chat-agent.md")
        assert tmpl is not None
        assert tmpl.name == "chat-agent"

    def test_get_template_not_found(self, manager):
        tmpl = manager.get_template("nonexistent")
        assert tmpl is None

    def test_get_categories(self, manager):
        cats = manager.get_categories()
        cat_dict = {c["category"]: c["count"] for c in cats}
        assert cat_dict["task"] == 2
        assert cat_dict["system"] == 1
        assert cat_dict["hooks"] == 1

    def test_get_all_tags(self, manager):
        tags = manager.get_all_tags()
        assert "system" in tags
        assert "chat" in tags
        assert "planning" in tags
        assert "testing" in tags
        assert "hooks" in tags

    def test_render_by_name(self, manager):
        result = manager.render("chat-agent", {"workspace_dir": "/home/dev"})
        assert result is not None
        assert "/home/dev" in result

    def test_render_not_found(self, manager):
        result = manager.render("nonexistent")
        assert result is None

    def test_empty_directory(self, tmp_path):
        pm = PromptManager(str(tmp_path / "empty"))
        assert pm.list_templates() == []
        assert pm.get_template("anything") is None
        assert pm.get_categories() == []
        assert pm.get_all_tags() == []

    def test_ensure_directory(self, tmp_path):
        pm = PromptManager(str(tmp_path / "new_prompts"))
        pm.ensure_directory()
        assert os.path.isdir(str(tmp_path / "new_prompts"))
        for cat in CATEGORIES:
            assert os.path.isdir(str(tmp_path / "new_prompts" / cat))

    def test_templates_sorted_by_category_then_name(self, manager):
        templates = manager.list_templates()
        categories = [t.category for t in templates]
        # Should be sorted: hooks, system, task
        assert categories == sorted(categories)
