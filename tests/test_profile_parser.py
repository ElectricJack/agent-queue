"""Tests for profile_parser — markdown profile parsing.

Covers:
- YAML frontmatter extraction (id, name, tags, extras)
- Section splitting at ## boundaries
- JSON extraction from Config, Tools, MCP Servers sections
- Text extraction from Role, Rules, Reflection sections
- Error handling for invalid JSON
- Type validation (JSON must be objects, not arrays)
- Edge cases: empty files, missing sections, multiple JSON blocks
- Full round-trip with the spec example from docs/specs/design/profiles.md §2
- Conversion to AgentProfile-compatible dict
"""

from __future__ import annotations

from src.profile_parser import (
    KNOWN_SECTIONS,
    PROMPT_SECTIONS,
    STRUCTURED_SECTIONS,
    _extract_json_block,
    _parse_section,
    _split_sections,
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
        assert len(STRUCTURED_SECTIONS) == 3

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
        assert result.mcp_servers["github"]["command"] == "npx"
        assert "-y" in result.mcp_servers["github"]["args"]
        assert result.mcp_servers["github"]["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"

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
        assert result.mcp_servers == {}
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
        assert result.mcp_servers["linter"]["command"] == "eslint"

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
        text = (
            '## Config\n```json\n{bad}\n```\n\n'
            '## Tools\n```json\n{also bad}\n```\n'
        )
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

    def test_mcp_servers_json_not_object(self):
        text = '## MCP Servers\n```json\n["not", "an", "object"]\n```\n'
        result = parse_profile(text)
        assert not result.is_valid
        assert any("must be an object" in e for e in result.errors)

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
        assert '```json' in section.raw
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
        text = '## Config\n```json\n{}\n```\n'
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
        text = (
            "## Config\n"
            '```json\n{"model": "first"}\n```\n'
            '```json\n{"model": "second"}\n```\n'
        )
        result = parse_profile(text)
        assert result.is_valid
        assert result.config["model"] == "first"

    def test_deeply_nested_json(self):
        text = (
            '## MCP Servers\n```json\n'
            '{"server": {"command": "npx", "args": ["-y", "pkg"], '
            '"env": {"KEY": "val", "NESTED": {"a": 1}}}}\n```\n'
        )
        result = parse_profile(text)
        assert result.is_valid
        assert result.mcp_servers["server"]["env"]["NESTED"]["a"] == 1

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
            '## MCP Servers\n```json\n'
            '{"gh": {"command": "npx", "env": {"TOKEN": "${GITHUB_TOKEN}"}}}\n```\n'
        )
        result = parse_profile(text)
        assert result.mcp_servers["gh"]["env"]["TOKEN"] == "${GITHUB_TOKEN}"

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
