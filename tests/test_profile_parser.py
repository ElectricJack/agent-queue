"""Tests for profile_parser — markdown profile parsing.

Covers:
- YAML frontmatter extraction (id, name, tags, extras)
- Section splitting at ## boundaries
- JSON extraction from Config, Tools, MCP Servers sections
- Text extraction from Role, Rules, Reflection sections
- English section extractor (raw markdown preservation)
- Error handling for invalid JSON
- Type validation (JSON must be objects, not arrays)
- Edge cases: empty files, missing sections, multiple JSON blocks
- Full round-trip with the spec example from docs/specs/design/profiles.md §2
- Conversion to AgentProfile-compatible dict (section labels, individual fields)
"""

from __future__ import annotations

from src.profiles.parser import (
    CONFIG_KNOWN_KEYS,
    KNOWN_SECTIONS,
    PROMPT_SECTIONS,
    STRUCTURED_SECTIONS,
    TOOLS_KNOWN_KEYS,
    VALID_PERMISSION_MODES,
    _extract_json_block,
    _extract_prompt_text,
    _parse_section,
    _split_sections,
    _validate_config,
    _validate_mcp_servers,
    _validate_tools,
    get_registry_tool_names,
    parse_frontmatter,
    parse_profile,
    parsed_profile_to_agent_profile,
)

# ---------------------------------------------------------------------------
# Full spec example from docs/specs/design/profiles.md §2
# ---------------------------------------------------------------------------

SPEC_EXAMPLE = """\
---
id: coding
name: Coding Agent
tags: [profile, agent-type]
---

# Coding Agent

## Role
You are a software engineering agent. You write, modify, and debug code
within a project workspace. You follow project conventions, write tests,
and commit clean, working code.

## Config
```json
{
  "model": "claude-sonnet-4-6",
  "permission_mode": "auto",
  "max_tokens_per_task": 100000
}
```

## Tools
```json
{
  "allowed": ["shell", "file_read", "file_write", "git", "vibecop_scan", "vibecop_check"],
  "denied": []
}
```

## MCP Servers
```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}
  }
}
```

## Rules
- Always run existing tests before committing
- Never commit secrets, .env files, or credentials
- Prefer small, focused commits over large ones
- If tests fail after your changes, fix them before moving on
- Check for and respect any project-specific overrides

## Reflection
After completing a task, consider:
- Did I encounter any surprising behavior worth remembering?
- Did I resolve an error that might recur? If so, save the pattern.
- Is there a convention in this project I should note for next time?
"""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify section category constants."""

    def test_structured_sections(self):
        assert "config" in STRUCTURED_SECTIONS
        assert "tools" in STRUCTURED_SECTIONS
        assert "mcp servers" in STRUCTURED_SECTIONS
        assert "install" in STRUCTURED_SECTIONS
        assert len(STRUCTURED_SECTIONS) == 4

    def test_prompt_sections(self):
        assert "role" in PROMPT_SECTIONS
        assert "rules" in PROMPT_SECTIONS
        assert "reflection" in PROMPT_SECTIONS
        assert len(PROMPT_SECTIONS) == 3

    def test_known_sections_is_union(self):
        assert KNOWN_SECTIONS == STRUCTURED_SECTIONS | PROMPT_SECTIONS


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    """Test YAML frontmatter extraction."""

    def test_basic_frontmatter(self):
        text = "---\nid: coding\nname: Coding Agent\ntags: [profile]\n---\n\n# Title\n"
        fm, remaining = parse_frontmatter(text)
        assert fm.id == "coding"
        assert fm.name == "Coding Agent"
        assert fm.tags == ["profile"]
        assert "# Title" in remaining

    def test_frontmatter_with_extra_keys(self):
        text = "---\nid: test\nname: Test\ncustom_key: custom_value\n---\nBody"
        fm, remaining = parse_frontmatter(text)
        assert fm.id == "test"
        assert fm.extra == {"custom_key": "custom_value"}

    def test_no_frontmatter(self):
        text = "# Just a title\n\nSome content"
        fm, remaining = parse_frontmatter(text)
        assert fm.id == ""
        assert fm.name == ""
        assert remaining == text

    def test_empty_string(self):
        fm, remaining = parse_frontmatter("")
        assert fm.id == ""
        assert remaining == ""

    def test_frontmatter_no_closing_delimiter(self):
        text = "---\nid: broken\nname: Broken"
        fm, remaining = parse_frontmatter(text)
        assert fm.id == ""
        assert remaining == text

    def test_frontmatter_tags_not_list(self):
        text = "---\nid: x\nname: X\ntags: single-tag\n---\nBody"
        fm, _ = parse_frontmatter(text)
        assert fm.tags == ["single-tag"]

    def test_frontmatter_tags_empty(self):
        text = "---\nid: x\nname: X\ntags:\n---\nBody"
        fm, _ = parse_frontmatter(text)
        assert fm.tags == []

    def test_frontmatter_missing_id(self):
        text = "---\nname: No ID\n---\nBody"
        fm, _ = parse_frontmatter(text)
        assert fm.id == ""
        assert fm.name == "No ID"

    def test_frontmatter_invalid_yaml(self):
        text = "---\n: [invalid yaml\n---\nBody"
        fm, remaining = parse_frontmatter(text)
        assert fm.id == ""
        assert remaining == text

    def test_frontmatter_non_dict(self):
        text = "---\n- just a list\n---\nBody"
        fm, remaining = parse_frontmatter(text)
        assert fm.id == ""
        assert remaining == text

    def test_remaining_content_after_frontmatter(self):
        text = "---\nid: test\nname: Test\n---\n\nContent here"
        _, remaining = parse_frontmatter(text)
        assert "Content here" in remaining


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------


class TestSplitSections:
    """Test markdown section splitting at ## boundaries."""

    def test_single_section(self):
        text = "## Config\nSome content\n"
        sections = _split_sections(text)
        assert len(sections) == 2  # pre-section ("") + Config
        assert sections[1][0] == "Config"
        assert "Some content" in sections[1][1]

    def test_multiple_sections(self):
        text = "## Role\nRole text\n\n## Config\nConfig text\n\n## Tools\nTools text\n"
        sections = _split_sections(text)
        headings = [h for h, _ in sections if h]
        assert headings == ["Role", "Config", "Tools"]

    def test_content_before_first_heading(self):
        text = "# Title\n\nSome preamble\n\n## Config\nConfig text\n"
        sections = _split_sections(text)
        assert sections[0][0] == ""
        assert "Title" in sections[0][1]
        assert sections[1][0] == "Config"

    def test_h3_not_treated_as_section(self):
        text = "## Config\nSome content\n### Subsection\nSub content\n"
        sections = _split_sections(text)
        headings = [h for h, _ in sections if h]
        assert headings == ["Config"]
        assert "Subsection" in sections[1][1]

    def test_empty_text(self):
        sections = _split_sections("")
        assert len(sections) == 1
        assert sections[0] == ("", "")

    def test_mcp_servers_heading(self):
        text = "## MCP Servers\nServer config\n"
        sections = _split_sections(text)
        assert sections[1][0] == "MCP Servers"

    def test_preserves_section_body(self):
        text = "## Role\nLine 1\nLine 2\nLine 3\n"
        sections = _split_sections(text)
        body = sections[1][1]
        assert "Line 1" in body
        assert "Line 2" in body
        assert "Line 3" in body


# ---------------------------------------------------------------------------
# JSON block extraction
# ---------------------------------------------------------------------------


class TestExtractJsonBlock:
    """Test JSON code block extraction from section text."""

    def test_basic_json_block(self):
        text = '```json\n{"key": "value"}\n```\n'
        json_str, remaining = _extract_json_block(text)
        assert json_str == '{"key": "value"}'
        assert "```" not in remaining

    def test_no_json_block(self):
        text = "Just some text without any code blocks."
        json_str, remaining = _extract_json_block(text)
        assert json_str is None
        assert remaining == text

    def test_json_block_with_surrounding_text(self):
        text = 'Some prose before\n```json\n{"a": 1}\n```\nSome prose after\n'
        json_str, remaining = _extract_json_block(text)
        assert json_str == '{"a": 1}'
        assert "Some prose before" in remaining
        assert "Some prose after" in remaining

    def test_multiline_json_block(self):
        text = '```json\n{\n  "model": "claude",\n  "mode": "auto"\n}\n```\n'
        json_str, _ = _extract_json_block(text)
        assert '"model": "claude"' in json_str
        assert '"mode": "auto"' in json_str

    def test_non_json_code_block_ignored(self):
        text = "```python\nprint('hello')\n```\n"
        json_str, remaining = _extract_json_block(text)
        assert json_str is None
        assert remaining == text


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------


class TestParseSection:
    """Test individual section parsing."""

    def test_config_section_with_json(self):
        body = '```json\n{"model": "claude-sonnet-4-6"}\n```\n'
        section, errors = _parse_section("Config", body)
        assert not errors
        assert section.json_data == {"model": "claude-sonnet-4-6"}
        assert section.heading == "Config"

    def test_tools_section_with_json(self):
        body = '```json\n{"allowed": ["shell", "git"], "denied": []}\n```\n'
        section, errors = _parse_section("Tools", body)
        assert not errors
        assert section.json_data["allowed"] == ["shell", "git"]
        assert section.json_data["denied"] == []

    def test_mcp_servers_section_with_json(self):
        body = '```json\n{"gh": {"command": "npx", "args": ["-y", "server-github"]}}\n```\n'
        section, errors = _parse_section("MCP Servers", body)
        assert not errors
        assert "gh" in section.json_data
        assert section.json_data["gh"]["command"] == "npx"

    def test_role_section_captures_text(self):
        body = "You are a coding agent.\nYou write clean code.\n"
        section, errors = _parse_section("Role", body)
        assert not errors
        assert "coding agent" in section.text
        assert section.json_data is None

    def test_rules_section_captures_text(self):
        body = "- Always run tests\n- Never commit secrets\n"
        section, errors = _parse_section("Rules", body)
        assert not errors
        assert "Always run tests" in section.text
        assert "Never commit secrets" in section.text

    def test_reflection_section_captures_text(self):
        body = "After completing a task, consider what you learned.\n"
        section, errors = _parse_section("Reflection", body)
        assert not errors
        assert "consider what you learned" in section.text

    def test_structured_section_invalid_json(self):
        body = '```json\n{"broken": }\n```\n'
        section, errors = _parse_section("Config", body)
        assert len(errors) == 1
        assert "Invalid JSON" in errors[0]
        assert "## Config" in errors[0]
        assert section.json_data is None

    def test_structured_section_no_json_block(self):
        body = "No JSON here, just text.\n"
        section, errors = _parse_section("Config", body)
        assert not errors
        assert section.json_data is None
        assert section.text == "No JSON here, just text."

    def test_unrecognized_section(self):
        body = "Custom section content.\n"
        section, errors = _parse_section("Custom", body)
        assert not errors
        assert section.text == "Custom section content."


# ---------------------------------------------------------------------------
# Full profile parsing
# ---------------------------------------------------------------------------


class TestParseProfile:
    """Test the main parse_profile function."""

    def test_spec_example(self):
        """Parse the complete example from the spec."""
        result = parse_profile(SPEC_EXAMPLE)
        assert result.is_valid, f"Errors: {result.errors}"

        # Frontmatter
        assert result.frontmatter.id == "coding"
        assert result.frontmatter.name == "Coding Agent"
        assert result.frontmatter.tags == ["profile", "agent-type"]

        # Config
        assert result.config["model"] == "claude-sonnet-4-6"
        assert result.config["permission_mode"] == "auto"
        assert result.config["max_tokens_per_task"] == 100000

        # Tools
        assert "shell" in result.tools["allowed"]
        assert "file_read" in result.tools["allowed"]
        assert "git" in result.tools["allowed"]
        assert result.tools["denied"] == []

        # MCP Servers
        assert "github" in result.mcp_servers
        assert result.mcp_servers_legacy["github"]["command"] == "npx"
        assert "-y" in result.mcp_servers_legacy["github"]["args"]
        assert result.mcp_servers_legacy["github"]["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"

        # Role
        assert "software engineering agent" in result.role

        # Rules
        assert "Always run existing tests" in result.rules
        assert "Never commit secrets" in result.rules

        # Reflection
        assert "surprising behavior" in result.reflection

    def test_empty_input(self):
        result = parse_profile("")
        assert result.is_valid
        assert result.config == {}
        assert result.tools == {}
        assert result.mcp_servers == []
        assert result.role == ""

    def test_whitespace_only(self):
        result = parse_profile("   \n\n   \n")
        assert result.is_valid

    def test_config_only(self):
        text = '## Config\n```json\n{"model": "opus"}\n```\n'
        result = parse_profile(text)
        assert result.is_valid
        assert result.config == {"model": "opus"}

    def test_tools_only(self):
        text = '## Tools\n```json\n{"allowed": ["Read"], "denied": ["Write"]}\n```\n'
        result = parse_profile(text)
        assert result.is_valid
        assert result.tools["allowed"] == ["Read"]
        assert result.tools["denied"] == ["Write"]

    def test_mcp_servers_only(self):
        text = '## MCP Servers\n```json\n{"linter": {"command": "eslint"}}\n```\n'
        result = parse_profile(text)
        assert result.is_valid
        assert result.mcp_servers_legacy["linter"]["command"] == "eslint"

    def test_role_only(self):
        text = "## Role\nYou are a reviewer.\n"
        result = parse_profile(text)
        assert result.is_valid
        assert result.role == "You are a reviewer."

    def test_frontmatter_only(self):
        text = "---\nid: minimal\nname: Minimal\n---\n"
        result = parse_profile(text)
        assert result.is_valid
        assert result.frontmatter.id == "minimal"
        assert result.frontmatter.name == "Minimal"

    def test_invalid_json_reports_error(self):
        text = '## Config\n```json\n{"broken": }\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert len(result.errors) == 1
        assert "Invalid JSON" in result.errors[0]
        assert result.config == {}

    def test_multiple_invalid_sections(self):
        text = "## Config\n```json\n{bad}\n```\n\n## Tools\n```json\n{also bad}\n```\n"
        result = parse_profile(text)
        assert not result.is_valid
        assert len(result.errors) == 2

    def test_config_json_not_object(self):
        text = '## Config\n```json\n["not", "an", "object"]\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("must be an object" in e for e in result.errors)

    def test_tools_json_not_object(self):
        text = '## Tools\n```json\n["not", "an", "object"]\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("must be an object" in e for e in result.errors)

    def test_mcp_servers_list_of_strings_is_new_format(self):
        # Lists of strings are now the canonical shape (registry names).
        text = '## MCP Servers\n```json\n["alpha", "beta"]\n```\n'
        result = parse_profile(text)
        assert result.is_valid
        assert result.mcp_servers == ["alpha", "beta"]
        assert result.mcp_servers_legacy is None

    def test_mcp_servers_json_neither_list_nor_object(self):
        text = "## MCP Servers\n```json\n42\n```\n"
        result = parse_profile(text)
        assert not result.is_valid
        assert any("must be a list" in e for e in result.errors)

    def test_mcp_servers_list_with_non_string_entry(self):
        text = '## MCP Servers\n```json\n["good", 123]\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("must be a non-empty string" in e for e in result.errors)

    def test_unrecognized_sections_preserved(self):
        text = "## Custom Section\nCustom content.\n\n## Config\n```json\n{}\n```\n"
        result = parse_profile(text)
        assert result.is_valid
        assert "custom section" in result.sections
        assert result.sections["custom section"].text == "Custom content."

    def test_sections_case_insensitive(self):
        """Section headings should match case-insensitively."""
        text = '## config\n```json\n{"model": "opus"}\n```\n'
        result = parse_profile(text)
        assert result.is_valid
        assert result.config == {"model": "opus"}

    def test_all_sections_stored(self):
        result = parse_profile(SPEC_EXAMPLE)
        assert "config" in result.sections
        assert "tools" in result.sections
        assert "mcp servers" in result.sections
        assert "role" in result.sections
        assert "rules" in result.sections
        assert "reflection" in result.sections

    def test_section_raw_preserved(self):
        text = '## Config\nSome notes\n```json\n{"model": "opus"}\n```\nMore notes\n'
        result = parse_profile(text)
        section = result.sections["config"]
        assert "Some notes" in section.raw
        assert "```json" in section.raw
        assert "More notes" in section.raw

    def test_structured_section_text_without_json_block(self):
        """Text around JSON blocks in structured sections is preserved."""
        text = '## Config\nNotes about config:\n```json\n{"model": "opus"}\n```\nEnd notes.\n'
        result = parse_profile(text)
        section = result.sections["config"]
        assert "Notes about config:" in section.text
        assert "End notes." in section.text
        assert "```" not in section.text


# ---------------------------------------------------------------------------
# Conversion to AgentProfile dict
# ---------------------------------------------------------------------------


class TestParsedProfileToAgentProfile:
    """Test conversion of ParsedProfile to AgentProfile-compatible dict."""

    def test_full_conversion(self):
        result = parse_profile(SPEC_EXAMPLE)
        d = parsed_profile_to_agent_profile(result)
        assert d["id"] == "coding"
        assert d["name"] == "Coding Agent"
        assert d["model"] == "claude-sonnet-4-6"
        assert d["permission_mode"] == "auto"
        assert "shell" in d["allowed_tools"]
        assert "github" in d["mcp_servers"]
        assert "system_prompt_suffix" in d

    def test_system_prompt_includes_role(self):
        result = parse_profile(SPEC_EXAMPLE)
        d = parsed_profile_to_agent_profile(result)
        assert "software engineering agent" in d["system_prompt_suffix"]

    def test_system_prompt_includes_rules(self):
        result = parse_profile(SPEC_EXAMPLE)
        d = parsed_profile_to_agent_profile(result)
        assert "Always run existing tests" in d["system_prompt_suffix"]

    def test_system_prompt_includes_reflection(self):
        result = parse_profile(SPEC_EXAMPLE)
        d = parsed_profile_to_agent_profile(result)
        assert "surprising behavior" in d["system_prompt_suffix"]

    def test_minimal_profile(self):
        result = parse_profile("---\nid: minimal\nname: Minimal\n---\n")
        d = parsed_profile_to_agent_profile(result)
        assert d == {"id": "minimal", "name": "Minimal"}

    def test_empty_profile(self):
        result = parse_profile("")
        d = parsed_profile_to_agent_profile(result)
        assert d == {}

    def test_config_only_conversion(self):
        text = (
            "---\nid: test\nname: Test\n---\n"
            '## Config\n```json\n{"model": "opus", "permission_mode": "plan"}\n```\n'
        )
        result = parse_profile(text)
        d = parsed_profile_to_agent_profile(result)
        assert d["id"] == "test"
        assert d["model"] == "opus"
        assert d["permission_mode"] == "plan"
        assert "allowed_tools" not in d
        assert "mcp_servers" not in d

    def test_tools_only_conversion(self):
        text = '## Tools\n```json\n{"allowed": ["Read", "Grep"]}\n```\n'
        result = parse_profile(text)
        d = parsed_profile_to_agent_profile(result)
        assert d["allowed_tools"] == ["Read", "Grep"]

    def test_omits_empty_values(self):
        """Empty config fields should not appear in the output dict."""
        text = '## Config\n```json\n{"model": ""}\n```\n'
        result = parse_profile(text)
        d = parsed_profile_to_agent_profile(result)
        assert "model" not in d

    def test_individual_role_field(self):
        """Role text should be exposed as a separate 'role' field."""
        result = parse_profile(SPEC_EXAMPLE)
        d = parsed_profile_to_agent_profile(result)
        assert "role" in d
        assert "software engineering agent" in d["role"]

    def test_individual_rules_field(self):
        """Rules text should be exposed as a separate 'rules' field."""
        result = parse_profile(SPEC_EXAMPLE)
        d = parsed_profile_to_agent_profile(result)
        assert "rules" in d
        assert "Always run existing tests" in d["rules"]

    def test_individual_reflection_field(self):
        """Reflection text should be exposed as a separate 'reflection' field."""
        result = parse_profile(SPEC_EXAMPLE)
        d = parsed_profile_to_agent_profile(result)
        assert "reflection" in d
        assert "surprising behavior" in d["reflection"]

    def test_system_prompt_has_section_labels(self):
        """system_prompt_suffix should contain ## labels for each section."""
        result = parse_profile(SPEC_EXAMPLE)
        d = parsed_profile_to_agent_profile(result)
        suffix = d["system_prompt_suffix"]
        assert "## Role\n" in suffix
        assert "## Rules\n" in suffix
        assert "## Reflection\n" in suffix

    def test_section_labels_order(self):
        """Section labels should appear in Role → Rules → Reflection order."""
        result = parse_profile(SPEC_EXAMPLE)
        d = parsed_profile_to_agent_profile(result)
        suffix = d["system_prompt_suffix"]
        role_idx = suffix.index("## Role")
        rules_idx = suffix.index("## Rules")
        reflection_idx = suffix.index("## Reflection")
        assert role_idx < rules_idx < reflection_idx

    def test_single_prompt_section_no_extra_labels(self):
        """A profile with only Role should have just the Role label."""
        text = "## Role\nYou are a reviewer.\n"
        result = parse_profile(text)
        d = parsed_profile_to_agent_profile(result)
        assert d["system_prompt_suffix"] == "## Role\nYou are a reviewer."
        assert "## Rules" not in d["system_prompt_suffix"]
        assert "## Reflection" not in d["system_prompt_suffix"]

    def test_no_prompt_sections_no_suffix(self):
        """Profile without any prompt sections should not have system_prompt_suffix."""
        text = '## Config\n```json\n{"model": "opus"}\n```\n'
        result = parse_profile(text)
        d = parsed_profile_to_agent_profile(result)
        assert "system_prompt_suffix" not in d
        assert "role" not in d
        assert "rules" not in d
        assert "reflection" not in d


# ---------------------------------------------------------------------------
# English section extraction (_extract_prompt_text)
# ---------------------------------------------------------------------------


class TestExtractPromptText:
    """Test the _extract_prompt_text function for raw markdown preservation."""

    def test_plain_text(self):
        text = "You are a coding agent.\n"
        assert _extract_prompt_text(text) == "You are a coding agent."

    def test_strips_whitespace_boundaries(self):
        text = "\n\n  You are a reviewer.  \n\n"
        assert _extract_prompt_text(text) == "You are a reviewer."

    def test_empty_body(self):
        assert _extract_prompt_text("") == ""

    def test_whitespace_only(self):
        assert _extract_prompt_text("   \n\n   \n") == ""

    def test_preserves_markdown_lists(self):
        text = "- First rule\n- Second rule\n- Third rule\n"
        result = _extract_prompt_text(text)
        assert "- First rule" in result
        assert "- Second rule" in result
        assert "- Third rule" in result

    def test_preserves_ordered_lists(self):
        text = "1. Step one\n2. Step two\n3. Step three\n"
        result = _extract_prompt_text(text)
        assert "1. Step one" in result
        assert "2. Step two" in result
        assert "3. Step three" in result

    def test_preserves_sub_headings(self):
        text = "Main content.\n### Details\nDetailed instructions.\n"
        result = _extract_prompt_text(text)
        assert "Main content." in result
        assert "### Details" in result
        assert "Detailed instructions." in result

    def test_preserves_code_blocks(self):
        text = "Run this:\n```bash\npython -m pytest\n```\n"
        result = _extract_prompt_text(text)
        assert "```bash" in result
        assert "python -m pytest" in result
        assert result.count("```") == 2

    def test_preserves_emphasis(self):
        text = "This is **important** and *emphasised*.\n"
        result = _extract_prompt_text(text)
        assert "**important**" in result
        assert "*emphasised*" in result

    def test_preserves_links(self):
        text = "See [the docs](https://example.com) for details.\n"
        result = _extract_prompt_text(text)
        assert "[the docs](https://example.com)" in result

    def test_preserves_multiline_paragraphs(self):
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph.\n"
        result = _extract_prompt_text(text)
        assert "First paragraph.\n\nSecond paragraph.\n\nThird paragraph." == result

    def test_preserves_blockquotes(self):
        text = "> This is a quote.\n> It spans lines.\n"
        result = _extract_prompt_text(text)
        assert "> This is a quote." in result
        assert "> It spans lines." in result

    def test_preserves_inline_code(self):
        text = "Use `git commit` to save changes.\n"
        result = _extract_prompt_text(text)
        assert "`git commit`" in result

    def test_preserves_nested_lists(self):
        text = "- Top level\n  - Nested item\n  - Another nested\n- Back to top\n"
        result = _extract_prompt_text(text)
        assert "  - Nested item" in result
        assert "  - Another nested" in result

    def test_preserves_horizontal_rules(self):
        text = "Before rule.\n\n---\n\nAfter rule.\n"
        result = _extract_prompt_text(text)
        assert "---" in result

    def test_preserves_tables(self):
        text = "| Col A | Col B |\n|-------|-------|\n| val1  | val2  |\n"
        result = _extract_prompt_text(text)
        assert "| Col A | Col B |" in result
        assert "| val1  | val2  |" in result


# ---------------------------------------------------------------------------
# English section extraction (integration via parse_profile)
# ---------------------------------------------------------------------------


class TestEnglishSectionExtraction:
    """Integration tests for English section extraction through the full parser.

    These tests verify that Role, Rules, and Reflection sections are stored
    as raw markdown strings per the profiles spec §2.
    """

    def test_role_stored_as_raw_markdown(self):
        """Role section text is stored as a raw markdown string."""
        text = "## Role\nYou are a **software engineering** agent.\n"
        result = parse_profile(text)
        assert result.role == "You are a **software engineering** agent."
        assert result.sections["role"].text == result.role
        assert result.sections["role"].json_data is None

    def test_rules_stored_as_raw_markdown(self):
        """Rules section text is stored as a raw markdown string."""
        text = "## Rules\n- Always run tests\n- Never commit secrets\n"
        result = parse_profile(text)
        assert result.rules == "- Always run tests\n- Never commit secrets"
        assert result.sections["rules"].text == result.rules

    def test_reflection_stored_as_raw_markdown(self):
        """Reflection section text is stored as a raw markdown string."""
        text = "## Reflection\nAfter completing a task, consider:\n- What went well?\n"
        result = parse_profile(text)
        assert result.reflection == "After completing a task, consider:\n- What went well?"
        assert result.sections["reflection"].text == result.reflection

    def test_role_with_sub_headings(self):
        """Role section can contain ### sub-headings."""
        text = (
            "## Role\n"
            "You are a coding agent.\n\n"
            "### Primary Responsibilities\n"
            "- Write clean code\n"
            "- Review pull requests\n\n"
            "### Secondary\n"
            "- Documentation\n"
        )
        result = parse_profile(text)
        assert "### Primary Responsibilities" in result.role
        assert "### Secondary" in result.role
        assert "Write clean code" in result.role
        assert "Documentation" in result.role

    def test_rules_with_code_examples(self):
        """Rules section can contain fenced code blocks as examples."""
        text = (
            "## Rules\n"
            "Format imports like this:\n\n"
            "```python\n"
            "import os\n"
            "import sys\n"
            "```\n\n"
            "Always follow PEP 8.\n"
        )
        result = parse_profile(text)
        assert "```python" in result.rules
        assert "import os" in result.rules
        assert "Always follow PEP 8." in result.rules

    def test_reflection_with_rich_markdown(self):
        """Reflection section can contain full rich markdown."""
        text = (
            "## Reflection\n"
            "After completing a task:\n\n"
            "1. **Review** your changes\n"
            "2. Check for [common pitfalls](./pitfalls.md)\n"
            "3. Use `git diff` to verify\n\n"
            "> Remember: quality over speed\n"
        )
        result = parse_profile(text)
        assert "**Review**" in result.reflection
        assert "[common pitfalls](./pitfalls.md)" in result.reflection
        assert "`git diff`" in result.reflection
        assert "> Remember: quality over speed" in result.reflection

    def test_empty_role_section(self):
        """Empty Role section results in empty string."""
        text = "## Role\n\n## Config\n```json\n{}\n```\n"
        result = parse_profile(text)
        assert result.role == ""

    def test_whitespace_only_role_section(self):
        """Whitespace-only Role section results in empty string."""
        text = "## Role\n   \n\n## Rules\n- A rule\n"
        result = parse_profile(text)
        assert result.role == ""
        assert result.rules == "- A rule"

    def test_multiline_role_with_blank_lines(self):
        """Role with multiple paragraphs separated by blank lines."""
        text = (
            "## Role\n"
            "You are a reviewer.\n\n"
            "Your primary focus is code quality.\n\n"
            "You value correctness above all.\n"
        )
        result = parse_profile(text)
        assert "You are a reviewer." in result.role
        assert "Your primary focus is code quality." in result.role
        assert "You value correctness above all." in result.role
        # Blank lines between paragraphs should be preserved
        assert "\n\n" in result.role

    def test_rules_with_nested_lists(self):
        """Rules section with nested list items."""
        text = (
            "## Rules\n"
            "- Git conventions:\n"
            "  - Use conventional commits\n"
            "  - Keep commits small\n"
            "- Testing:\n"
            "  - Write unit tests\n"
            "  - Run tests before committing\n"
        )
        result = parse_profile(text)
        assert "  - Use conventional commits" in result.rules
        assert "  - Keep commits small" in result.rules
        assert "  - Write unit tests" in result.rules

    def test_section_raw_field_preserved(self):
        """The raw field contains the untouched section body."""
        text = "## Role\n  Some text with leading spaces.  \n\n"
        result = parse_profile(text)
        section = result.sections["role"]
        # raw is untouched
        assert "  Some text with leading spaces.  " in section.raw
        # text is stripped
        assert section.text == "Some text with leading spaces."

    def test_all_prompt_sections_in_spec_example(self):
        """Verify all three prompt sections from the spec example."""
        result = parse_profile(SPEC_EXAMPLE)

        # Role
        assert "software engineering agent" in result.role
        assert "project conventions" in result.role
        assert "commit clean, working code" in result.role

        # Rules — each rule is a markdown list item
        assert "- Always run existing tests before committing" in result.rules
        assert "- Never commit secrets, .env files, or credentials" in result.rules
        assert "- Prefer small, focused commits over large ones" in result.rules
        assert "- If tests fail after your changes" in result.rules
        assert "- Check for and respect any project-specific overrides" in result.rules

        # Reflection
        assert "After completing a task, consider:" in result.reflection
        assert "- Did I encounter any surprising behavior" in result.reflection
        assert "- Did I resolve an error that might recur" in result.reflection
        assert "- Is there a convention in this project" in result.reflection

    def test_prompt_sections_only_profile(self):
        """A profile with only prompt sections (no structured sections)."""
        text = (
            "---\nid: reviewer\nname: Code Reviewer\n---\n\n"
            "## Role\nYou review code for quality.\n\n"
            "## Rules\n- Be constructive\n- Focus on correctness\n\n"
            "## Reflection\n- Was my review helpful?\n"
        )
        result = parse_profile(text)
        assert result.is_valid
        assert result.frontmatter.id == "reviewer"
        assert "review code for quality" in result.role
        assert "- Be constructive" in result.rules
        assert "- Was my review helpful?" in result.reflection
        # Structured sections should be defaults
        assert result.config == {}
        assert result.tools == {}
        assert result.mcp_servers == []

    def test_unicode_in_prompt_sections(self):
        """Unicode content in prompt sections is preserved."""
        text = "## Role\nYou speak 日本語 and handle émojis: 🎉\n"
        result = parse_profile(text)
        assert "日本語" in result.role
        assert "🎉" in result.role

    def test_windows_line_endings_in_prompt_sections(self):
        """Windows \\r\\n line endings in prompt sections are handled."""
        text = "## Role\r\nA Windows role.\r\n\r\n## Rules\r\n- A rule\r\n"
        result = parse_profile(text)
        assert "A Windows role." in result.role
        assert "- A rule" in result.rules


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_windows_line_endings(self):
        text = "---\r\nid: win\r\nname: Windows\r\n---\r\n\r\n## Role\r\nA role.\r\n"
        result = parse_profile(text)
        assert result.frontmatter.id == "win"
        assert "A role." in result.role

    def test_no_trailing_newline(self):
        text = "## Role\nA role."
        result = parse_profile(text)
        assert result.role == "A role."

    def test_empty_json_object(self):
        text = "## Config\n```json\n{}\n```\n"
        result = parse_profile(text)
        assert result.is_valid
        # Empty dict means config stays empty (no keys to map)
        assert result.config == {}

    def test_json_with_comments_is_invalid(self):
        """JSON doesn't support comments — this should be an error."""
        text = '## Config\n```json\n{\n  // comment\n  "model": "opus"\n}\n```\n'
        result = parse_profile(text)
        assert not result.is_valid

    def test_multiple_json_blocks_takes_first(self):
        """If a section has multiple JSON blocks, only the first is parsed."""
        text = '## Config\n```json\n{"model": "first"}\n```\n```json\n{"model": "second"}\n```\n'
        result = parse_profile(text)
        assert result.is_valid
        assert result.config["model"] == "first"

    def test_deeply_nested_json(self):
        """Deeply nested env values are parsed but flagged as validation errors."""
        text = (
            "## MCP Servers\n```json\n"
            '{"server": {"command": "npx", "args": ["-y", "pkg"], '
            '"env": {"KEY": "val", "NESTED": {"a": 1}}}}\n```\n'
        )
        result = parse_profile(text)
        # JSON parses fine and is accessible
        assert result.mcp_servers_legacy["server"]["env"]["NESTED"]["a"] == 1
        # But non-string env values are now flagged as errors
        assert not result.is_valid
        assert any("env['NESTED']" in e and "string" in e for e in result.errors)

    def test_h1_heading_not_treated_as_section(self):
        text = "# Title\n\n## Config\n```json\n{}\n```\n"
        result = parse_profile(text)
        assert "config" in result.sections
        assert len([s for s in result.sections if s]) == 1

    def test_h3_subheading_within_section(self):
        text = "## Role\nMain role text.\n### Details\nDetailed instructions.\n"
        result = parse_profile(text)
        assert "Main role text." in result.role
        assert "### Details" in result.role
        assert "Detailed instructions." in result.role

    def test_env_variable_placeholder_preserved(self):
        """Environment variable placeholders like ${VAR} should be preserved as-is."""
        text = (
            "## MCP Servers\n```json\n"
            '{"gh": {"command": "npx", "env": {"TOKEN": "${GITHUB_TOKEN}"}}}\n```\n'
        )
        result = parse_profile(text)
        assert result.mcp_servers_legacy["gh"]["env"]["TOKEN"] == "${GITHUB_TOKEN}"

    def test_unicode_content(self):
        text = "---\nid: intl\nname: 国際化エージェント\n---\n\n## Role\nYou speak 日本語.\n"
        result = parse_profile(text)
        assert result.frontmatter.name == "国際化エージェント"
        assert "日本語" in result.role

    def test_section_with_only_whitespace_body(self):
        text = "## Config\n   \n\n## Role\nContent here.\n"
        result = parse_profile(text)
        assert result.is_valid
        assert result.sections["config"].json_data is None

    def test_tools_denied_list(self):
        text = '## Tools\n```json\n{"allowed": [], "denied": ["shell", "Write"]}\n```\n'
        result = parse_profile(text)
        assert result.is_valid
        assert result.tools["allowed"] == []
        assert result.tools["denied"] == ["shell", "Write"]


# ---------------------------------------------------------------------------
# MCP Servers validation (_validate_mcp_servers)
# ---------------------------------------------------------------------------


class TestValidateMcpServers:
    """Test structural validation of MCP server definitions.

    Per the spec (docs/specs/design/profiles.md §2): MCP server commands are
    validated for basic structure — command exists, args are strings.
    """

    # -- Valid configurations --

    def test_valid_full_server(self):
        """A complete server definition with command, args, and env passes."""
        servers = {
            "github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            }
        }
        errors = _validate_mcp_servers(servers)
        assert errors == []

    def test_valid_command_only(self):
        """A server with only command (no args or env) is valid."""
        servers = {"linter": {"command": "eslint"}}
        errors = _validate_mcp_servers(servers)
        assert errors == []

    def test_valid_command_with_args(self):
        """A server with command and args (no env) is valid."""
        servers = {"formatter": {"command": "prettier", "args": ["--write", "."]}}
        errors = _validate_mcp_servers(servers)
        assert errors == []

    def test_valid_command_with_env(self):
        """A server with command and env (no args) is valid."""
        servers = {"gh": {"command": "npx", "env": {"TOKEN": "abc123"}}}
        errors = _validate_mcp_servers(servers)
        assert errors == []

    def test_valid_multiple_servers(self):
        """Multiple valid server definitions all pass."""
        servers = {
            "github": {
                "command": "npx",
                "args": ["-y", "server-github"],
                "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            },
            "eslint": {"command": "eslint-server"},
            "prettier": {"command": "prettier", "args": ["--write"]},
        }
        errors = _validate_mcp_servers(servers)
        assert errors == []

    def test_valid_empty_args(self):
        """Empty args list is valid."""
        servers = {"s": {"command": "cmd", "args": []}}
        errors = _validate_mcp_servers(servers)
        assert errors == []

    def test_valid_empty_env(self):
        """Empty env dict is valid."""
        servers = {"s": {"command": "cmd", "env": {}}}
        errors = _validate_mcp_servers(servers)
        assert errors == []

    def test_valid_extra_keys_allowed(self):
        """Unknown keys are preserved for forward-compatibility."""
        servers = {"s": {"command": "cmd", "timeout": 30, "cwd": "/tmp"}}
        errors = _validate_mcp_servers(servers)
        assert errors == []

    def test_valid_empty_servers_dict(self):
        """An empty MCP servers dict is valid (no servers defined)."""
        errors = _validate_mcp_servers({})
        assert errors == []

    def test_valid_env_with_variable_placeholders(self):
        """Env values with ${VAR} placeholders are valid strings."""
        servers = {
            "gh": {
                "command": "npx",
                "env": {"TOKEN": "${GITHUB_TOKEN}", "PATH": "${HOME}/bin"},
            }
        }
        errors = _validate_mcp_servers(servers)
        assert errors == []

    # -- Invalid: server entry not a dict --

    def test_server_entry_is_string(self):
        """Server value must be a dict, not a string."""
        servers = {"bad": "not-a-dict"}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "bad" in errors[0]
        assert "expected an object" in errors[0]
        assert "str" in errors[0]

    def test_server_entry_is_list(self):
        """Server value must be a dict, not a list."""
        servers = {"bad": ["command", "npx"]}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "expected an object" in errors[0]
        assert "list" in errors[0]

    def test_server_entry_is_number(self):
        """Server value must be a dict, not a number."""
        servers = {"bad": 42}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "expected an object" in errors[0]

    def test_server_entry_is_null(self):
        """Server value must be a dict, not null."""
        servers = {"bad": None}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "expected an object" in errors[0]

    # -- Invalid: missing command --

    def test_missing_command(self):
        """Missing 'command' field is an error."""
        servers = {"gh": {"args": ["-y", "pkg"]}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "missing required field 'command'" in errors[0]
        assert "gh" in errors[0]

    def test_missing_command_with_env_only(self):
        """Server with only env but no command is an error."""
        servers = {"gh": {"env": {"TOKEN": "abc"}}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "missing required field 'command'" in errors[0]

    def test_empty_server_dict(self):
        """Server with empty dict (no fields at all) is an error."""
        servers = {"empty": {}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "missing required field 'command'" in errors[0]

    # -- Invalid: command wrong type --

    def test_command_is_number(self):
        """Command must be a string, not a number."""
        servers = {"s": {"command": 42}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "'command' must be a string" in errors[0]
        assert "int" in errors[0]

    def test_command_is_list(self):
        """Command must be a string, not a list."""
        servers = {"s": {"command": ["npx", "-y"]}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "'command' must be a string" in errors[0]
        assert "list" in errors[0]

    def test_command_is_bool(self):
        """Command must be a string, not a boolean."""
        servers = {"s": {"command": True}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "'command' must be a string" in errors[0]
        assert "bool" in errors[0]

    def test_command_is_null(self):
        """Command must be a string, not null."""
        servers = {"s": {"command": None}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "'command' must be a string" in errors[0]

    def test_command_empty_string(self):
        """Command must not be empty or whitespace-only."""
        servers = {"s": {"command": ""}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "'command' must not be empty" in errors[0]

    def test_command_whitespace_only(self):
        """Command must not be whitespace-only."""
        servers = {"s": {"command": "   "}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "'command' must not be empty" in errors[0]

    # -- Invalid: args wrong type --

    def test_args_is_string(self):
        """Args must be a list, not a string."""
        servers = {"s": {"command": "npx", "args": "-y pkg"}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "'args' must be an array" in errors[0]
        assert "str" in errors[0]

    def test_args_is_number(self):
        """Args must be a list, not a number."""
        servers = {"s": {"command": "npx", "args": 42}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "'args' must be an array" in errors[0]

    def test_args_is_dict(self):
        """Args must be a list, not a dict."""
        servers = {"s": {"command": "npx", "args": {"flag": "value"}}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "'args' must be an array" in errors[0]
        assert "dict" in errors[0]

    def test_args_contains_non_string(self):
        """Individual args must be strings."""
        servers = {"s": {"command": "npx", "args": ["-y", 42, True]}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 2  # 42 and True
        assert any("args[1]" in e and "int" in e for e in errors)
        assert any("args[2]" in e and "bool" in e for e in errors)

    def test_args_contains_null(self):
        """Null args entries are not allowed."""
        servers = {"s": {"command": "npx", "args": ["-y", None]}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "args[1]" in errors[0]

    def test_args_contains_nested_list(self):
        """Nested lists in args are not allowed."""
        servers = {"s": {"command": "npx", "args": ["-y", ["nested"]]}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "args[1]" in errors[0]
        assert "list" in errors[0]

    # -- Invalid: env wrong type --

    def test_env_is_string(self):
        """Env must be a dict, not a string."""
        servers = {"s": {"command": "npx", "env": "TOKEN=abc"}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "'env' must be an object" in errors[0]
        assert "str" in errors[0]

    def test_env_is_list(self):
        """Env must be a dict, not a list."""
        servers = {"s": {"command": "npx", "env": ["TOKEN=abc"]}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "'env' must be an object" in errors[0]
        assert "list" in errors[0]

    def test_env_value_is_number(self):
        """Env values must be strings."""
        servers = {"s": {"command": "npx", "env": {"PORT": 8080}}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "env['PORT']" in errors[0]
        assert "int" in errors[0]

    def test_env_value_is_bool(self):
        """Env values must be strings, not booleans."""
        servers = {"s": {"command": "npx", "env": {"DEBUG": True}}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "env['DEBUG']" in errors[0]
        assert "bool" in errors[0]

    def test_env_value_is_null(self):
        """Env values must be strings, not null."""
        servers = {"s": {"command": "npx", "env": {"TOKEN": None}}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "env['TOKEN']" in errors[0]

    def test_env_value_is_nested_dict(self):
        """Env values must be strings, not nested dicts."""
        servers = {"s": {"command": "npx", "env": {"NESTED": {"a": 1}}}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 1
        assert "env['NESTED']" in errors[0]
        assert "dict" in errors[0]

    def test_env_multiple_invalid_values(self):
        """Multiple invalid env values produce multiple errors."""
        servers = {"s": {"command": "npx", "env": {"A": 1, "B": True, "C": "ok"}}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 2  # A and B are invalid, C is fine

    # -- Multiple errors across servers --

    def test_multiple_servers_with_errors(self):
        """Errors from different servers are all reported."""
        servers = {
            "good": {"command": "npx", "args": ["-y"]},
            "no_cmd": {"args": ["-y"]},
            "bad_args": {"command": "npx", "args": "not-a-list"},
        }
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 2
        assert any("no_cmd" in e and "missing required field 'command'" in e for e in errors)
        assert any("bad_args" in e and "'args' must be an array" in e for e in errors)

    def test_same_server_multiple_errors(self):
        """A server with multiple issues reports all of them."""
        servers = {"s": {"command": 42, "args": "bad", "env": "bad"}}
        errors = _validate_mcp_servers(servers)
        assert len(errors) == 3
        assert any("'command' must be a string" in e for e in errors)
        assert any("'args' must be an array" in e for e in errors)
        assert any("'env' must be an object" in e for e in errors)

    # -- Integration via parse_profile --

    def test_integration_valid_mcp_servers(self):
        """Valid MCP servers pass parse_profile without errors."""
        text = (
            "## MCP Servers\n```json\n"
            '{"gh": {"command": "npx", "args": ["-y", "server-github"], '
            '"env": {"TOKEN": "${GITHUB_TOKEN}"}}}\n```\n'
        )
        result = parse_profile(text)
        assert result.is_valid, f"Unexpected errors: {result.errors}"
        assert result.mcp_servers_legacy["gh"]["command"] == "npx"

    def test_integration_missing_command(self):
        """parse_profile reports missing command in MCP server."""
        text = '## MCP Servers\n```json\n{"gh": {"args": ["-y"]}}\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("missing required field 'command'" in e for e in result.errors)
        # Data is still stored (parse, then validate)
        # Old shape preserved in mcp_servers_legacy
        assert result.mcp_servers_legacy == {"gh": {"args": ["-y"]}}
        # New flat shape: just the names
        assert result.mcp_servers == ["gh"]

    def test_integration_bad_args_type(self):
        """parse_profile reports non-array args in MCP server."""
        text = '## MCP Servers\n```json\n{"s": {"command": "x", "args": "bad"}}\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("'args' must be an array" in e for e in result.errors)

    def test_integration_bad_env_value(self):
        """parse_profile reports non-string env values in MCP server."""
        text = '## MCP Servers\n```json\n{"s": {"command": "x", "env": {"PORT": 8080}}}\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("env['PORT']" in e for e in result.errors)

    def test_integration_server_not_dict(self):
        """parse_profile reports server entry that isn't a dict."""
        text = '## MCP Servers\n```json\n{"bad": "not-a-dict"}\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("expected an object" in e for e in result.errors)

    def test_integration_command_only_valid(self):
        """A command-only MCP server passes validation in parse_profile."""
        text = '## MCP Servers\n```json\n{"linter": {"command": "eslint"}}\n```\n'
        result = parse_profile(text)
        assert result.is_valid
        assert result.mcp_servers_legacy["linter"]["command"] == "eslint"

    def test_integration_spec_example_valid(self):
        """The spec example MCP servers block passes validation."""
        result = parse_profile(SPEC_EXAMPLE)
        assert result.is_valid, f"Unexpected errors: {result.errors}"
        assert result.mcp_servers_legacy["github"]["command"] == "npx"


# ---------------------------------------------------------------------------
# Config validation (_validate_config)
# ---------------------------------------------------------------------------


class TestValidateConfig:
    """Test structural validation of the ## Config block fields.

    Per the spec (docs/specs/design/profiles.md §2): Config block contains
    model, permission_mode, and max_tokens_per_task. Each field has type
    constraints and (for permission_mode) value constraints.
    """

    # -- Constants --

    def test_config_known_keys(self):
        """CONFIG_KNOWN_KEYS contains the three spec-defined fields."""
        assert CONFIG_KNOWN_KEYS == {"model", "permission_mode", "max_tokens_per_task"}

    def test_valid_permission_modes_set(self):
        """VALID_PERMISSION_MODES contains all expected modes."""
        assert "default" in VALID_PERMISSION_MODES
        assert "plan" in VALID_PERMISSION_MODES
        assert "full" in VALID_PERMISSION_MODES
        assert "bypassPermissions" in VALID_PERMISSION_MODES
        assert "acceptEdits" in VALID_PERMISSION_MODES
        assert "auto" in VALID_PERMISSION_MODES
        # Empty string is NOT in the set (handled separately)
        assert "" not in VALID_PERMISSION_MODES

    # -- Valid configurations (no errors) --

    def test_valid_full_config(self):
        """A complete config with all three fields passes."""
        config = {
            "model": "claude-sonnet-4-6",
            "permission_mode": "auto",
            "max_tokens_per_task": 100000,
        }
        errors = _validate_config(config)
        assert errors == []

    def test_valid_model_only(self):
        """A config with only model passes."""
        config = {"model": "claude-sonnet-4-6"}
        errors = _validate_config(config)
        assert errors == []

    def test_valid_permission_mode_only(self):
        """A config with only permission_mode passes."""
        config = {"permission_mode": "plan"}
        errors = _validate_config(config)
        assert errors == []

    def test_valid_max_tokens_only(self):
        """A config with only max_tokens_per_task passes."""
        config = {"max_tokens_per_task": 50000}
        errors = _validate_config(config)
        assert errors == []

    def test_valid_empty_config(self):
        """An empty config dict is valid (all fields optional)."""
        errors = _validate_config({})
        assert errors == []

    def test_valid_all_permission_modes(self):
        """Every known permission mode value is accepted."""
        for mode in VALID_PERMISSION_MODES:
            errors = _validate_config({"permission_mode": mode})
            assert errors == [], f"Mode '{mode}' should be valid, got: {errors}"

    def test_valid_unknown_keys_allowed(self):
        """Unknown config keys are silently accepted for forward-compatibility."""
        config = {
            "model": "claude-sonnet-4-6",
            "custom_setting": "value",
            "future_field": 42,
        }
        errors = _validate_config(config)
        assert errors == []

    def test_valid_max_tokens_one(self):
        """max_tokens_per_task = 1 (smallest valid positive int) passes."""
        errors = _validate_config({"max_tokens_per_task": 1})
        assert errors == []

    def test_valid_max_tokens_large(self):
        """Very large max_tokens_per_task passes."""
        errors = _validate_config({"max_tokens_per_task": 10_000_000})
        assert errors == []

    def test_valid_model_various_formats(self):
        """Various model string formats pass."""
        for model in [
            "claude-sonnet-4-6",
            "claude-opus-4-20250514",
            "gpt-4o",
            "my-custom-model",
        ]:
            errors = _validate_config({"model": model})
            assert errors == [], f"Model '{model}' should be valid, got: {errors}"

    # -- Invalid: model --

    def test_model_is_number(self):
        """Model must be a string, not a number."""
        errors = _validate_config({"model": 42})
        assert len(errors) == 1
        assert "'model' must be a string" in errors[0]
        assert "int" in errors[0]

    def test_model_is_bool(self):
        """Model must be a string, not a boolean."""
        errors = _validate_config({"model": True})
        assert len(errors) == 1
        assert "'model' must be a string" in errors[0]
        assert "bool" in errors[0]

    def test_model_is_list(self):
        """Model must be a string, not a list."""
        errors = _validate_config({"model": ["claude-sonnet-4-6"]})
        assert len(errors) == 1
        assert "'model' must be a string" in errors[0]
        assert "list" in errors[0]

    def test_model_is_null(self):
        """Model must be a string, not null."""
        errors = _validate_config({"model": None})
        assert len(errors) == 1
        assert "'model' must be a string" in errors[0]

    def test_model_is_dict(self):
        """Model must be a string, not a dict."""
        errors = _validate_config({"model": {"name": "opus"}})
        assert len(errors) == 1
        assert "'model' must be a string" in errors[0]
        assert "dict" in errors[0]

    def test_model_empty_string(self):
        """Model must not be empty when explicitly provided."""
        errors = _validate_config({"model": ""})
        assert len(errors) == 1
        assert "'model' must not be empty" in errors[0]

    def test_model_whitespace_only(self):
        """Model must not be whitespace-only."""
        errors = _validate_config({"model": "   "})
        assert len(errors) == 1
        assert "'model' must not be empty" in errors[0]

    # -- Invalid: permission_mode --

    def test_permission_mode_is_number(self):
        """Permission mode must be a string, not a number."""
        errors = _validate_config({"permission_mode": 42})
        assert len(errors) == 1
        assert "'permission_mode' must be a string" in errors[0]
        assert "int" in errors[0]

    def test_permission_mode_is_bool(self):
        """Permission mode must be a string, not a boolean."""
        errors = _validate_config({"permission_mode": True})
        assert len(errors) == 1
        assert "'permission_mode' must be a string" in errors[0]
        assert "bool" in errors[0]

    def test_permission_mode_is_list(self):
        """Permission mode must be a string, not a list."""
        errors = _validate_config({"permission_mode": ["plan"]})
        assert len(errors) == 1
        assert "'permission_mode' must be a string" in errors[0]
        assert "list" in errors[0]

    def test_permission_mode_is_null(self):
        """Permission mode must be a string, not null."""
        errors = _validate_config({"permission_mode": None})
        assert len(errors) == 1
        assert "'permission_mode' must be a string" in errors[0]

    def test_permission_mode_unknown_value(self):
        """Unknown permission mode string is an error."""
        errors = _validate_config({"permission_mode": "turbo"})
        assert len(errors) == 1
        assert "must be one of" in errors[0]
        assert "'turbo'" in errors[0]

    def test_permission_mode_empty_string(self):
        """Empty string permission_mode is an error (not in VALID_PERMISSION_MODES)."""
        errors = _validate_config({"permission_mode": ""})
        assert len(errors) == 1
        assert "must be one of" in errors[0]

    def test_permission_mode_case_sensitive(self):
        """Permission mode matching is case-sensitive."""
        errors = _validate_config({"permission_mode": "Plan"})
        assert len(errors) == 1
        assert "must be one of" in errors[0]
        assert "'Plan'" in errors[0]

    def test_permission_mode_bypasspermissions_case(self):
        """bypassPermissions is camelCase — wrong case is an error."""
        errors = _validate_config({"permission_mode": "bypass_permissions"})
        assert len(errors) == 1
        assert "must be one of" in errors[0]

    # -- Invalid: max_tokens_per_task --

    def test_max_tokens_is_string(self):
        """max_tokens_per_task must be an integer, not a string."""
        errors = _validate_config({"max_tokens_per_task": "100000"})
        assert len(errors) == 1
        assert "'max_tokens_per_task' must be a positive integer" in errors[0]
        assert "str" in errors[0]

    def test_max_tokens_is_float(self):
        """max_tokens_per_task must be an integer, not a float."""
        errors = _validate_config({"max_tokens_per_task": 100.5})
        assert len(errors) == 1
        assert "'max_tokens_per_task' must be a positive integer" in errors[0]
        assert "float" in errors[0]

    def test_max_tokens_is_bool(self):
        """max_tokens_per_task must be an integer, not a boolean (bool subclasses int)."""
        errors = _validate_config({"max_tokens_per_task": True})
        assert len(errors) == 1
        assert "'max_tokens_per_task' must be a positive integer" in errors[0]
        assert "bool" in errors[0]

    def test_max_tokens_is_null(self):
        """max_tokens_per_task must be an integer, not null."""
        errors = _validate_config({"max_tokens_per_task": None})
        assert len(errors) == 1
        assert "'max_tokens_per_task' must be a positive integer" in errors[0]

    def test_max_tokens_is_list(self):
        """max_tokens_per_task must be an integer, not a list."""
        errors = _validate_config({"max_tokens_per_task": [100000]})
        assert len(errors) == 1
        assert "'max_tokens_per_task' must be a positive integer" in errors[0]
        assert "list" in errors[0]

    def test_max_tokens_zero(self):
        """max_tokens_per_task must be positive — zero is not allowed."""
        errors = _validate_config({"max_tokens_per_task": 0})
        assert len(errors) == 1
        assert "must be positive" in errors[0]
        assert "got 0" in errors[0]

    def test_max_tokens_negative(self):
        """max_tokens_per_task must be positive — negative is not allowed."""
        errors = _validate_config({"max_tokens_per_task": -1000})
        assert len(errors) == 1
        assert "must be positive" in errors[0]
        assert "got -1000" in errors[0]

    # -- Multiple errors --

    def test_all_fields_invalid(self):
        """Multiple invalid fields produce multiple errors."""
        config = {
            "model": 42,
            "permission_mode": 99,
            "max_tokens_per_task": "many",
        }
        errors = _validate_config(config)
        assert len(errors) == 3
        assert any("'model'" in e for e in errors)
        assert any("'permission_mode'" in e for e in errors)
        assert any("'max_tokens_per_task'" in e for e in errors)

    def test_two_fields_invalid(self):
        """Only invalid fields produce errors; valid fields pass."""
        config = {
            "model": "claude-sonnet-4-6",
            "permission_mode": "not-valid",
            "max_tokens_per_task": -1,
        }
        errors = _validate_config(config)
        assert len(errors) == 2
        assert any("'permission_mode'" in e for e in errors)
        assert any("'max_tokens_per_task'" in e for e in errors)

    # -- Integration via parse_profile --

    def test_integration_valid_config(self):
        """Valid config block passes parse_profile without errors."""
        text = (
            "## Config\n```json\n"
            '{"model": "claude-sonnet-4-6", "permission_mode": "auto", '
            '"max_tokens_per_task": 100000}\n```\n'
        )
        result = parse_profile(text)
        assert result.is_valid, f"Unexpected errors: {result.errors}"
        assert result.config["model"] == "claude-sonnet-4-6"
        assert result.config["permission_mode"] == "auto"
        assert result.config["max_tokens_per_task"] == 100000

    def test_integration_invalid_model_type(self):
        """parse_profile reports non-string model."""
        text = '## Config\n```json\n{"model": 42}\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("'model' must be a string" in e for e in result.errors)
        # Data is still stored (parse first, validate second)
        assert result.config == {"model": 42}

    def test_integration_invalid_permission_mode(self):
        """parse_profile reports unknown permission mode."""
        text = '## Config\n```json\n{"permission_mode": "turbo"}\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("must be one of" in e for e in result.errors)

    def test_integration_invalid_max_tokens(self):
        """parse_profile reports non-integer max_tokens_per_task."""
        text = '## Config\n```json\n{"max_tokens_per_task": "many"}\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("'max_tokens_per_task' must be a positive integer" in e for e in result.errors)

    def test_integration_negative_max_tokens(self):
        """parse_profile reports negative max_tokens_per_task."""
        text = '## Config\n```json\n{"max_tokens_per_task": -500}\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("must be positive" in e for e in result.errors)

    def test_integration_spec_example_valid(self):
        """The spec example config block passes validation."""
        result = parse_profile(SPEC_EXAMPLE)
        assert result.is_valid, f"Unexpected errors: {result.errors}"
        assert result.config["model"] == "claude-sonnet-4-6"
        assert result.config["permission_mode"] == "auto"
        assert result.config["max_tokens_per_task"] == 100000

    def test_integration_empty_config(self):
        """An empty config JSON object passes validation."""
        text = "## Config\n```json\n{}\n```\n"
        result = parse_profile(text)
        assert result.is_valid

    def test_integration_config_with_extras(self):
        """Unknown config keys don't trigger errors."""
        text = '## Config\n```json\n{"model": "opus", "custom_field": true}\n```\n'
        result = parse_profile(text)
        assert result.is_valid

    def test_integration_multiple_config_errors(self):
        """Multiple config validation errors are all reported."""
        text = (
            "## Config\n```json\n"
            '{"model": 123, "permission_mode": "turbo", "max_tokens_per_task": -1}\n```\n'
        )
        result = parse_profile(text)
        assert not result.is_valid
        assert len(result.errors) == 3
        assert any("'model'" in e for e in result.errors)
        assert any("'permission_mode'" in e for e in result.errors)
        assert any("'max_tokens_per_task'" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Tools validation (_validate_tools)
# ---------------------------------------------------------------------------


class TestValidateTools:
    """Test structural validation and tool name checking for ## Tools.

    Per the spec (docs/specs/design/profiles.md §2): Tool names in ## Tools
    are validated against the tool registry.  Unknown tools produce a
    **warning** (not a hard failure — the tool may not be loaded yet).
    """

    # -- Valid structures --

    def test_valid_allowed_and_denied(self):
        """Fully populated allowed and denied lists pass."""
        tools = {"allowed": ["create_task", "list_tasks"], "denied": ["stop_task"]}
        errors, warnings = _validate_tools(tools)
        assert errors == []
        assert warnings == []

    def test_valid_allowed_only(self):
        """Allowed list without denied is valid."""
        tools = {"allowed": ["create_task"]}
        errors, warnings = _validate_tools(tools)
        assert errors == []
        assert warnings == []

    def test_valid_denied_only(self):
        """Denied list without allowed is valid."""
        tools = {"denied": ["stop_task"]}
        errors, warnings = _validate_tools(tools)
        assert errors == []
        assert warnings == []

    def test_valid_empty_lists(self):
        """Empty allowed and denied lists are valid."""
        tools = {"allowed": [], "denied": []}
        errors, warnings = _validate_tools(tools)
        assert errors == []
        assert warnings == []

    def test_valid_empty_dict(self):
        """Empty tools dict is valid (no restrictions specified)."""
        errors, warnings = _validate_tools({})
        assert errors == []
        assert warnings == []

    # -- Invalid: allowed wrong type --

    def test_allowed_is_string(self):
        """'allowed' must be a list, not a string."""
        tools = {"allowed": "create_task"}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "'allowed' must be an array" in errors[0]
        assert "str" in errors[0]

    def test_allowed_is_dict(self):
        """'allowed' must be a list, not a dict."""
        tools = {"allowed": {"create_task": True}}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "'allowed' must be an array" in errors[0]
        assert "dict" in errors[0]

    def test_allowed_is_number(self):
        """'allowed' must be a list, not a number."""
        tools = {"allowed": 42}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "'allowed' must be an array" in errors[0]
        assert "int" in errors[0]

    def test_allowed_is_bool(self):
        """'allowed' must be a list, not a boolean."""
        tools = {"allowed": True}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "'allowed' must be an array" in errors[0]
        assert "bool" in errors[0]

    def test_allowed_is_null(self):
        """'allowed' must be a list, not null."""
        tools = {"allowed": None}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "'allowed' must be an array" in errors[0]

    # -- Invalid: denied wrong type --

    def test_denied_is_string(self):
        """'denied' must be a list, not a string."""
        tools = {"denied": "stop_task"}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "'denied' must be an array" in errors[0]
        assert "str" in errors[0]

    def test_denied_is_dict(self):
        """'denied' must be a list, not a dict."""
        tools = {"denied": {"stop_task": True}}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "'denied' must be an array" in errors[0]
        assert "dict" in errors[0]

    def test_denied_is_number(self):
        """'denied' must be a list, not a number."""
        tools = {"denied": 99}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "'denied' must be an array" in errors[0]
        assert "int" in errors[0]

    # -- Invalid: non-string items in lists --

    def test_allowed_item_is_number(self):
        """Items in allowed must be strings."""
        tools = {"allowed": ["create_task", 42, "list_tasks"]}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "allowed[1]" in errors[0]
        assert "must be a string" in errors[0]
        assert "int" in errors[0]

    def test_allowed_item_is_bool(self):
        """Boolean items in allowed produce an error."""
        tools = {"allowed": [True]}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "allowed[0]" in errors[0]
        assert "bool" in errors[0]

    def test_allowed_item_is_dict(self):
        """Dict items in allowed produce an error."""
        tools = {"allowed": [{"name": "create_task"}]}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "allowed[0]" in errors[0]
        assert "dict" in errors[0]

    def test_allowed_item_is_null(self):
        """Null items in allowed produce an error."""
        tools = {"allowed": [None]}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "allowed[0]" in errors[0]
        assert "NoneType" in errors[0]

    def test_allowed_item_is_list(self):
        """List items in allowed produce an error."""
        tools = {"allowed": [["nested"]]}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "allowed[0]" in errors[0]
        assert "list" in errors[0]

    def test_denied_item_is_number(self):
        """Items in denied must be strings."""
        tools = {"denied": [123]}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "denied[0]" in errors[0]
        assert "must be a string" in errors[0]

    def test_denied_item_is_bool(self):
        """Boolean items in denied produce an error."""
        tools = {"denied": [False]}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "denied[0]" in errors[0]
        assert "bool" in errors[0]

    def test_multiple_invalid_items(self):
        """Multiple invalid items produce multiple errors."""
        tools = {"allowed": [42, True], "denied": [None]}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 3

    # -- Invalid: empty string items --

    def test_allowed_empty_string(self):
        """Empty string in allowed produces an error."""
        tools = {"allowed": [""]}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "allowed[0]" in errors[0]
        assert "must not be empty" in errors[0]

    def test_allowed_whitespace_only(self):
        """Whitespace-only string in allowed produces an error."""
        tools = {"allowed": ["   "]}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "allowed[0]" in errors[0]
        assert "must not be empty" in errors[0]

    def test_denied_empty_string(self):
        """Empty string in denied produces an error."""
        tools = {"denied": [""]}
        errors, warnings = _validate_tools(tools)
        assert len(errors) == 1
        assert "denied[0]" in errors[0]
        assert "must not be empty" in errors[0]

    # -- Warnings: unknown keys --

    def test_unknown_key_warning(self):
        """Unknown top-level keys produce warnings, not errors."""
        tools = {"allowed": ["create_task"], "blocked": ["stop_task"]}
        errors, warnings = _validate_tools(tools)
        assert errors == []
        assert len(warnings) == 1
        assert "unknown key 'blocked'" in warnings[0]

    def test_multiple_unknown_keys(self):
        """Multiple unknown keys each produce a warning."""
        tools = {"allow_list": [], "block_list": [], "enabled": True}
        errors, warnings = _validate_tools(tools)
        # 'enabled' is not a list, but it's also an unknown key — the unknown-key
        # warning triggers before type checks on allowed/denied (which aren't present)
        assert errors == []
        assert len(warnings) == 3
        assert any("'allow_list'" in w for w in warnings)
        assert any("'block_list'" in w for w in warnings)
        assert any("'enabled'" in w for w in warnings)

    # -- Warnings: tool name validation --

    def test_unknown_allowed_tool(self):
        """Unknown tool name in allowed produces a warning."""
        known = {"create_task", "list_tasks", "stop_task"}
        tools = {"allowed": ["create_task", "nonexistent_tool"]}
        errors, warnings = _validate_tools(tools, known_tools=known)
        assert errors == []
        assert len(warnings) == 1
        assert "unknown tool 'nonexistent_tool'" in warnings[0]
        assert "'allowed'" in warnings[0]

    def test_unknown_denied_tool(self):
        """Unknown tool name in denied produces a warning."""
        known = {"create_task", "list_tasks", "stop_task"}
        tools = {"denied": ["fake_tool"]}
        errors, warnings = _validate_tools(tools, known_tools=known)
        assert errors == []
        assert len(warnings) == 1
        assert "unknown tool 'fake_tool'" in warnings[0]
        assert "'denied'" in warnings[0]

    def test_all_tools_known(self):
        """No warnings when all tool names are recognised."""
        known = {"create_task", "list_tasks", "stop_task"}
        tools = {"allowed": ["create_task", "list_tasks"], "denied": ["stop_task"]}
        errors, warnings = _validate_tools(tools, known_tools=known)
        assert errors == []
        assert warnings == []

    def test_multiple_unknown_tools(self):
        """Multiple unknown tools each produce a warning."""
        known = {"create_task"}
        tools = {"allowed": ["create_task", "foo_tool", "bar_tool"]}
        errors, warnings = _validate_tools(tools, known_tools=known)
        assert errors == []
        assert len(warnings) == 2
        assert any("'bar_tool'" in w for w in warnings)
        assert any("'foo_tool'" in w for w in warnings)

    def test_no_known_tools_skips_name_validation(self):
        """When known_tools is None, tool name validation is skipped."""
        tools = {"allowed": ["totally_fake_tool"]}
        errors, warnings = _validate_tools(tools, known_tools=None)
        assert errors == []
        assert warnings == []

    def test_empty_known_tools_set(self):
        """Empty known_tools set means all tool names are unknown."""
        tools = {"allowed": ["create_task"]}
        errors, warnings = _validate_tools(tools, known_tools=set())
        assert errors == []
        assert len(warnings) == 1
        assert "unknown tool 'create_task'" in warnings[0]

    # -- Warnings: duplicates between allowed and denied --

    def test_duplicate_in_allowed_and_denied(self):
        """Tool in both allowed and denied produces a warning."""
        tools = {"allowed": ["create_task", "list_tasks"], "denied": ["create_task"]}
        errors, warnings = _validate_tools(tools)
        assert errors == []
        assert len(warnings) == 1
        assert "'create_task'" in warnings[0]
        assert "both 'allowed' and 'denied'" in warnings[0]

    def test_multiple_duplicates(self):
        """Multiple tools in both lists each produce a warning."""
        tools = {"allowed": ["a", "b", "c"], "denied": ["b", "c", "d"]}
        errors, warnings = _validate_tools(tools)
        assert errors == []
        assert len(warnings) == 2
        assert any("'b'" in w for w in warnings)
        assert any("'c'" in w for w in warnings)

    # -- Mixed errors and warnings --

    def test_errors_and_warnings_together(self):
        """Structural errors and name warnings are reported independently."""
        known = {"create_task"}
        tools = {
            "allowed": [42, "unknown_tool"],
            "denied": ["create_task"],
            "extra_key": True,
        }
        errors, warnings = _validate_tools(tools, known_tools=known)
        assert len(errors) == 1  # allowed[0] is not a string
        assert "allowed[0]" in errors[0]
        # Warnings: unknown key, unknown tool name
        assert any("'extra_key'" in w for w in warnings)
        assert any("'unknown_tool'" in w for w in warnings)

    # -- TOOLS_KNOWN_KEYS constant --

    def test_tools_known_keys_constant(self):
        """TOOLS_KNOWN_KEYS contains exactly 'allowed' and 'denied'."""
        assert TOOLS_KNOWN_KEYS == {"allowed", "denied"}


# ---------------------------------------------------------------------------
# Tools validation integration with parse_profile
# ---------------------------------------------------------------------------


class TestToolsValidationIntegration:
    """Test that _validate_tools is correctly wired into parse_profile.

    The spec says unknown tools produce warnings, not errors, so the
    profile should remain valid (is_valid=True) even with unknown tools.
    """

    def test_valid_tools_section(self):
        """A well-formed tools section with known tools passes."""
        known = {"shell", "git", "file_read"}
        text = '## Tools\n```json\n{"allowed": ["shell", "git"], "denied": []}\n```\n'
        result = parse_profile(text, known_tools=known)
        assert result.is_valid
        assert result.warnings == []
        assert result.tools["allowed"] == ["shell", "git"]

    def test_unknown_tool_produces_warning_not_error(self):
        """Unknown tool names produce warnings; profile remains valid."""
        known = {"shell", "git"}
        text = '## Tools\n```json\n{"allowed": ["shell", "vibecop_scan"], "denied": []}\n```\n'
        result = parse_profile(text, known_tools=known)
        assert result.is_valid, f"Unexpected errors: {result.errors}"
        assert len(result.warnings) == 1
        assert "unknown tool 'vibecop_scan'" in result.warnings[0]

    def test_structural_error_makes_profile_invalid(self):
        """Non-string items in allowed make the profile invalid."""
        text = '## Tools\n```json\n{"allowed": [42]}\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("allowed[0]" in e for e in result.errors)

    def test_no_known_tools_no_warnings(self):
        """Without known_tools, no name validation occurs."""
        text = '## Tools\n```json\n{"allowed": ["completely_fake_tool"]}\n```\n'
        result = parse_profile(text)
        assert result.is_valid
        assert result.warnings == []

    def test_tools_section_preserves_data_despite_warnings(self):
        """Data is stored even when warnings are generated."""
        known = {"create_task"}
        text = (
            "## Tools\n```json\n"
            '{"allowed": ["create_task", "unknown_1"], '
            '"denied": ["unknown_2"]}\n```\n'
        )
        result = parse_profile(text, known_tools=known)
        assert result.is_valid
        assert result.tools["allowed"] == ["create_task", "unknown_1"]
        assert result.tools["denied"] == ["unknown_2"]
        assert len(result.warnings) == 2

    def test_spec_example_with_known_tools(self):
        """The spec example tools section validates against a tools set."""
        known = {"shell", "file_read", "file_write", "git", "vibecop_scan", "vibecop_check"}
        text = (
            "## Tools\n```json\n"
            '{"allowed": ["shell", "file_read", "file_write", "git", '
            '"vibecop_scan", "vibecop_check"], "denied": []}\n```\n'
        )
        result = parse_profile(text, known_tools=known)
        assert result.is_valid
        assert result.warnings == []

    def test_spec_example_partially_known(self):
        """Spec example with some tools not yet loaded produces warnings."""
        known = {"shell", "file_read", "file_write", "git"}
        text = (
            "## Tools\n```json\n"
            '{"allowed": ["shell", "file_read", "file_write", "git", '
            '"vibecop_scan", "vibecop_check"], "denied": []}\n```\n'
        )
        result = parse_profile(text, known_tools=known)
        assert result.is_valid
        assert len(result.warnings) == 2
        assert any("vibecop_scan" in w for w in result.warnings)
        assert any("vibecop_check" in w for w in result.warnings)

    def test_duplicate_tools_warning_in_integration(self):
        """Duplicates between allowed and denied produce warnings via parse_profile."""
        text = '## Tools\n```json\n{"allowed": ["create_task"], "denied": ["create_task"]}\n```\n'
        result = parse_profile(text)
        assert result.is_valid
        assert len(result.warnings) == 1
        assert "both 'allowed' and 'denied'" in result.warnings[0]


# ---------------------------------------------------------------------------
# get_registry_tool_names
# ---------------------------------------------------------------------------


class TestGetRegistryToolNames:
    """Test the convenience function for getting known tool names."""

    def test_returns_set_of_strings(self):
        """get_registry_tool_names returns a set of string tool names."""
        names = get_registry_tool_names()
        assert isinstance(names, set)
        assert len(names) > 0
        assert all(isinstance(n, str) for n in names)

    def test_contains_known_tools(self):
        """Result includes well-known built-in tool names."""
        names = get_registry_tool_names()
        # These are core tools that should always exist
        assert "create_task" in names
        assert "list_tasks" in names
        assert "list_projects" in names

    def test_with_explicit_registry(self):
        """Passing an explicit ToolRegistry works."""
        from src.tools import ToolRegistry

        registry = ToolRegistry()
        names = get_registry_tool_names(registry)
        assert isinstance(names, set)
        assert "create_task" in names

    def test_custom_registry_with_limited_tools(self):
        """A custom registry with limited tools returns only those names."""
        from src.tools import ToolRegistry

        registry = ToolRegistry(
            tools=[
                {"name": "alpha", "description": "a", "input_schema": {}},
                {"name": "beta", "description": "b", "input_schema": {}},
            ]
        )
        names = get_registry_tool_names(registry)
        # Custom registry creates navigation tools automatically,
        # but our custom tools should be there
        assert "alpha" in names
        assert "beta" in names
