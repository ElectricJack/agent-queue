"""Tests for src/facts_parser — standalone facts.md parser and renderer."""

from __future__ import annotations

from src.facts_parser import diff_facts, parse_facts_file, render_facts_file


# ---------------------------------------------------------------------------
# parse_facts_file
# ---------------------------------------------------------------------------


class TestParseFacts:
    """Tests for parse_facts_file — extracting namespaced KV pairs."""

    def test_empty_string(self):
        assert parse_facts_file("") == {}

    def test_whitespace_only(self):
        assert parse_facts_file("   \n\n  \n") == {}

    def test_single_namespace(self):
        text = "## project\ntech_stack: Python\ntest_cmd: pytest tests/ -v\n"
        result = parse_facts_file(text)
        assert result == {
            "project": {
                "tech_stack": "Python",
                "test_cmd": "pytest tests/ -v",
            }
        }

    def test_multiple_namespaces(self):
        text = (
            "## project\n"
            "tech_stack: Python\n"
            "\n"
            "## conventions\n"
            "commit_style: conventional\n"
            "line_length: 100\n"
        )
        result = parse_facts_file(text)
        assert "project" in result
        assert "conventions" in result
        assert result["project"]["tech_stack"] == "Python"
        assert result["conventions"]["commit_style"] == "conventional"
        assert result["conventions"]["line_length"] == "100"

    def test_ignores_non_kv_lines(self):
        text = "## project\ntech_stack: Python\nThis is a comment without colon\n"
        result = parse_facts_file(text)
        assert result == {"project": {"tech_stack": "Python"}}

    def test_ignores_blank_lines(self):
        text = "## project\n\ntech_stack: Python\n\n\ndb: SQLite\n"
        result = parse_facts_file(text)
        assert result == {"project": {"tech_stack": "Python", "db": "SQLite"}}

    def test_ignores_orphan_lines_before_heading(self):
        text = "orphan_key: orphan_value\n## project\nkey: val\n"
        result = parse_facts_file(text)
        assert result == {"project": {"key": "val"}}

    def test_value_with_colons(self):
        """Values containing colons should be preserved after the first colon."""
        text = "## urls\napi: http://localhost:8080/api\n"
        result = parse_facts_file(text)
        assert result["urls"]["api"] == "http://localhost:8080/api"

    def test_value_with_multiple_colons(self):
        text = "## config\ntime: 12:30:45\n"
        result = parse_facts_file(text)
        assert result["config"]["time"] == "12:30:45"

    # --- Bullet prefix handling ---

    def test_bullet_dash_prefix(self):
        """Lines with ``- `` bullet prefix should have the bullet stripped."""
        text = "## project\n- tech_stack: Python\n- db: SQLite\n"
        result = parse_facts_file(text)
        assert result == {
            "project": {"tech_stack": "Python", "db": "SQLite"},
        }

    def test_bullet_asterisk_prefix(self):
        """Lines with ``* `` bullet prefix should have the bullet stripped."""
        text = "## project\n* tech_stack: Python\n"
        result = parse_facts_file(text)
        assert result["project"]["tech_stack"] == "Python"

    def test_bullet_plus_prefix(self):
        """Lines with ``+ `` bullet prefix should have the bullet stripped."""
        text = "## project\n+ tech_stack: Python\n"
        result = parse_facts_file(text)
        assert result["project"]["tech_stack"] == "Python"

    def test_mixed_bullets_and_plain(self):
        """Bullet-prefixed and plain lines can coexist under a namespace."""
        text = (
            "## project\n"
            "- tech_stack: Python\n"
            "db: SQLite\n"
            "* framework: FastAPI\n"
        )
        result = parse_facts_file(text)
        assert result == {
            "project": {
                "tech_stack": "Python",
                "db": "SQLite",
                "framework": "FastAPI",
            }
        }

    def test_bullet_with_list_value(self):
        """Spec example: ``- tech_stack: [Python 3.12, SQLAlchemy, Pygame]``."""
        text = "## project\n- tech_stack: [Python 3.12, SQLAlchemy, Pygame]\n"
        result = parse_facts_file(text)
        assert result["project"]["tech_stack"] == "[Python 3.12, SQLAlchemy, Pygame]"

    def test_bullet_with_url_value(self):
        """Bullet prefix combined with a value that has colons."""
        text = "## urls\n- api: http://localhost:8080/api\n"
        result = parse_facts_file(text)
        assert result["urls"]["api"] == "http://localhost:8080/api"

    # --- YAML frontmatter ---

    def test_yaml_frontmatter_skipped(self):
        text = (
            "---\n"
            "tags: [facts, auto-updated]\n"
            "---\n"
            "\n"
            "## project\n"
            "tech_stack: Python\n"
        )
        result = parse_facts_file(text)
        assert result == {"project": {"tech_stack": "Python"}}

    def test_frontmatter_only(self):
        text = "---\ntags: [facts]\n---\n"
        result = parse_facts_file(text)
        assert result == {}

    def test_frontmatter_with_multiple_headings(self):
        text = (
            "---\ntags: [facts]\n---\n"
            "\n"
            "# Project Facts -- Mech Fighters\n"
            "\n"
            "## project\n"
            "- tech_stack: [Python 3.12, SQLAlchemy, Pygame]\n"
            "- deploy_branch: main\n"
            "\n"
            "## conventions\n"
            "- orm_pattern: repository\n"
        )
        result = parse_facts_file(text)
        assert result == {
            "project": {
                "tech_stack": "[Python 3.12, SQLAlchemy, Pygame]",
                "deploy_branch": "main",
            },
            "conventions": {
                "orm_pattern": "repository",
            },
        }

    def test_horizontal_rule_after_frontmatter_ignored(self):
        """A ``---`` line after frontmatter closes is just a horizontal rule."""
        text = (
            "---\ntags: [facts]\n---\n"
            "\n"
            "---\n"
            "\n"
            "## project\n"
            "key: val\n"
        )
        result = parse_facts_file(text)
        assert result == {"project": {"key": "val"}}

    # --- Heading levels ---

    def test_h1_heading_not_a_namespace(self):
        """Top-level ``# Title`` headings should not create a namespace."""
        text = "# Title\n## project\nkey: val\n"
        result = parse_facts_file(text)
        assert "Title" not in result
        assert result == {"project": {"key": "val"}}

    def test_h3_heading_not_a_namespace(self):
        """Only ``## heading`` creates a namespace; ``### sub`` does not."""
        text = "## project\nkey: val\n### sub\nsub_key: sub_val\n"
        result = parse_facts_file(text)
        # ### sub is ignored as a heading; sub_key still belongs to "project"
        assert result == {"project": {"key": "val", "sub_key": "sub_val"}}

    def test_empty_namespace_heading(self):
        """A ``## `` line with no name should be ignored."""
        text = "## \nkey: val\n## project\nother: val2\n"
        result = parse_facts_file(text)
        assert result == {"project": {"other": "val2"}}

    def test_namespace_heading_preserves_case(self):
        text = "## Project\nkey: val\n## Conventions\nfoo: bar\n"
        result = parse_facts_file(text)
        assert "Project" in result
        assert "Conventions" in result

    # --- Edge cases ---

    def test_empty_value(self):
        """A key with an empty value after the colon should still be stored."""
        text = "## project\nempty_key:\n"
        result = parse_facts_file(text)
        assert result["project"]["empty_key"] == ""

    def test_empty_key_ignored(self):
        """A line like ``: value`` (empty key) should be ignored."""
        text = "## project\n: value\n"
        result = parse_facts_file(text)
        assert result == {"project": {}}

    def test_indented_kv_line(self):
        """Indented lines should still be parsed."""
        text = "## project\n    tech_stack: Python\n"
        result = parse_facts_file(text)
        assert result["project"]["tech_stack"] == "Python"

    def test_indented_bullet_line(self):
        """Indented bullet lines should still be parsed."""
        text = "## project\n    - tech_stack: Python\n"
        result = parse_facts_file(text)
        assert result["project"]["tech_stack"] == "Python"

    def test_duplicate_namespace_headings_merge(self):
        """If the same heading appears twice, entries should merge."""
        text = (
            "## project\n"
            "tech_stack: Python\n"
            "\n"
            "## project\n"
            "db: SQLite\n"
        )
        result = parse_facts_file(text)
        assert result == {
            "project": {"tech_stack": "Python", "db": "SQLite"},
        }

    def test_duplicate_key_last_wins(self):
        """If the same key appears twice under a namespace, last value wins."""
        text = "## project\nkey: old\nkey: new\n"
        result = parse_facts_file(text)
        assert result["project"]["key"] == "new"

    def test_full_spec_example(self):
        """The complete example from the spec should parse correctly."""
        text = (
            "---\n"
            "tags: [facts, auto-updated]\n"
            "---\n"
            "\n"
            "# Project Facts -- Mech Fighters\n"
            "\n"
            "## Project\n"
            "- tech_stack: [Python 3.12, SQLAlchemy, Pygame]\n"
            "- deploy_branch: main\n"
            "- test_command: pytest tests/ -v\n"
            "- repo_url: github.com/user/mech-fighters\n"
            "\n"
            "## Conventions\n"
            "- orm_pattern: repository\n"
            "- naming: snake_case\n"
            "\n"
            "## Stats\n"
            "- total_tasks_completed: 47\n"
            "- avg_task_tokens: 32000\n"
        )
        result = parse_facts_file(text)
        assert result == {
            "Project": {
                "tech_stack": "[Python 3.12, SQLAlchemy, Pygame]",
                "deploy_branch": "main",
                "test_command": "pytest tests/ -v",
                "repo_url": "github.com/user/mech-fighters",
            },
            "Conventions": {
                "orm_pattern": "repository",
                "naming": "snake_case",
            },
            "Stats": {
                "total_tasks_completed": "47",
                "avg_task_tokens": "32000",
            },
        }


# ---------------------------------------------------------------------------
# render_facts_file
# ---------------------------------------------------------------------------


class TestRenderFacts:
    """Tests for render_facts_file — rendering KV dicts to markdown."""

    def test_empty(self):
        assert render_facts_file({}) == ""

    def test_single_namespace(self):
        data = {"project": {"tech_stack": "Python", "test_cmd": "pytest"}}
        rendered = render_facts_file(data)
        assert "## project" in rendered
        assert "tech_stack: Python" in rendered
        assert "test_cmd: pytest" in rendered

    def test_multiple_namespaces_sorted(self):
        data = {
            "project": {"a": "1"},
            "conventions": {"b": "2"},
        }
        rendered = render_facts_file(data)
        assert "## conventions" in rendered
        assert "## project" in rendered
        # Namespaces should be sorted alphabetically
        conv_idx = rendered.index("## conventions")
        proj_idx = rendered.index("## project")
        assert conv_idx < proj_idx

    def test_keys_sorted_within_namespace(self):
        data = {"ns": {"z_key": "3", "a_key": "1", "m_key": "2"}}
        rendered = render_facts_file(data)
        lines = rendered.strip().splitlines()
        kv_lines = [ln for ln in lines if ":" in ln and not ln.startswith("#")]
        assert kv_lines == ["a_key: 1", "m_key: 2", "z_key: 3"]

    def test_sections_separated_by_blank_line(self):
        data = {"alpha": {"a": "1"}, "beta": {"b": "2"}}
        rendered = render_facts_file(data)
        assert "\n\n## beta" in rendered

    def test_trailing_newline(self):
        data = {"ns": {"key": "val"}}
        rendered = render_facts_file(data)
        assert rendered.endswith("\n")

    def test_empty_namespace_renders_heading_only(self):
        data = {"empty_ns": {}}
        rendered = render_facts_file(data)
        assert "## empty_ns" in rendered


# ---------------------------------------------------------------------------
# Roundtrip: parse → render → parse
# ---------------------------------------------------------------------------


class TestRoundtrip:
    """Verify parse → render → parse stability."""

    def test_basic_roundtrip(self):
        original = (
            "## conventions\n"
            "commit_style: conventional\n"
            "line_length: 100\n"
            "\n"
            "## project\n"
            "tech_stack: Python\n"
        )
        data = parse_facts_file(original)
        rendered = render_facts_file(data)
        data2 = parse_facts_file(rendered)
        assert data == data2

    def test_roundtrip_with_colons_in_values(self):
        original = "## urls\napi: http://localhost:8080/api\n"
        data = parse_facts_file(original)
        rendered = render_facts_file(data)
        data2 = parse_facts_file(rendered)
        assert data == data2

    def test_roundtrip_with_bullets(self):
        """Bullet-prefixed input roundtrips (rendered without bullets)."""
        original = "## project\n- tech_stack: Python\n- db: SQLite\n"
        data = parse_facts_file(original)
        rendered = render_facts_file(data)
        data2 = parse_facts_file(rendered)
        assert data == data2
        # Rendered form does not include bullets
        assert "- tech_stack" not in rendered
        assert "tech_stack: Python" in rendered

    def test_roundtrip_with_frontmatter(self):
        """Frontmatter is ignored on parse; rendered form omits it."""
        original = (
            "---\ntags: [facts]\n---\n"
            "\n"
            "## project\n"
            "key: val\n"
        )
        data = parse_facts_file(original)
        rendered = render_facts_file(data)
        data2 = parse_facts_file(rendered)
        assert data == data2


# ---------------------------------------------------------------------------
# diff_facts
# ---------------------------------------------------------------------------


class TestDiffFacts:
    """Tests for diff_facts — computing deltas between parsed states."""

    def test_empty_to_empty(self):
        upserts, deletes = diff_facts({}, {})
        assert upserts == {}
        assert deletes == {}

    def test_empty_to_populated(self):
        new = {"project": {"key": "val"}}
        upserts, deletes = diff_facts({}, new)
        assert upserts == {"project": {"key": "val"}}
        assert deletes == {}

    def test_populated_to_empty(self):
        old = {"project": {"key": "val"}}
        upserts, deletes = diff_facts(old, {})
        assert upserts == {}
        assert deletes == {"project": ["key"]}

    def test_no_change(self):
        data = {"project": {"key": "val"}}
        upserts, deletes = diff_facts(data, data)
        assert upserts == {}
        assert deletes == {}

    def test_value_changed(self):
        old = {"project": {"key": "old"}}
        new = {"project": {"key": "new"}}
        upserts, deletes = diff_facts(old, new)
        assert upserts == {"project": {"key": "new"}}
        assert deletes == {}

    def test_key_added(self):
        old = {"project": {"a": "1"}}
        new = {"project": {"a": "1", "b": "2"}}
        upserts, deletes = diff_facts(old, new)
        assert upserts == {"project": {"b": "2"}}
        assert deletes == {}

    def test_key_removed(self):
        old = {"project": {"a": "1", "b": "2"}}
        new = {"project": {"a": "1"}}
        upserts, deletes = diff_facts(old, new)
        assert upserts == {}
        assert deletes == {"project": ["b"]}

    def test_namespace_added(self):
        old = {"project": {"a": "1"}}
        new = {"project": {"a": "1"}, "conventions": {"b": "2"}}
        upserts, deletes = diff_facts(old, new)
        assert upserts == {"conventions": {"b": "2"}}
        assert deletes == {}

    def test_namespace_removed(self):
        old = {"project": {"a": "1"}, "conventions": {"b": "2"}}
        new = {"project": {"a": "1"}}
        upserts, deletes = diff_facts(old, new)
        assert upserts == {}
        assert deletes == {"conventions": ["b"]}

    def test_mixed_changes(self):
        old = {
            "project": {"a": "1", "b": "2"},
            "conventions": {"c": "3"},
        }
        new = {
            "project": {"a": "changed", "d": "4"},
            "stats": {"e": "5"},
        }
        upserts, deletes = diff_facts(old, new)
        assert upserts == {
            "project": {"a": "changed", "d": "4"},
            "stats": {"e": "5"},
        }
        assert deletes == {
            "project": ["b"],
            "conventions": ["c"],
        }
