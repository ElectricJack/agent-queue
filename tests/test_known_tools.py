"""Tests for the known tools registry and InstallManifest."""

from src.known_tools import (
    CLAUDE_CODE_TOOLS,
    KNOWN_MCP_NAMES,
    KNOWN_MCP_SERVERS,
    KNOWN_TOOL_NAMES,
    InstallManifest,
    validate_tool_names,
)


class TestKnownTools:
    def test_core_tools_present(self):
        for tool in ("Read", "Write", "Edit", "Bash", "Glob", "Grep"):
            assert tool in KNOWN_TOOL_NAMES

    def test_known_tool_names_matches_dict(self):
        assert KNOWN_TOOL_NAMES == frozenset(CLAUDE_CODE_TOOLS)

    def test_validate_tool_names_all_valid(self):
        assert validate_tool_names(["Read", "Write", "Edit"]) == []

    def test_validate_tool_names_returns_unknowns(self):
        result = validate_tool_names(["Read", "Typo", "FakeGlob"])
        assert result == ["Typo", "FakeGlob"]

    def test_validate_tool_names_empty(self):
        assert validate_tool_names([]) == []


class TestKnownMCPServers:
    def test_known_servers_present(self):
        assert "playwright" in KNOWN_MCP_NAMES
        assert "filesystem" in KNOWN_MCP_NAMES

    def test_server_has_required_fields(self):
        for name, server in KNOWN_MCP_SERVERS.items():
            assert "description" in server
            assert "npm_package" in server
            assert "command" in server


class TestInstallManifest:
    def test_from_dict_empty(self):
        m = InstallManifest.from_dict({})
        assert m.npm == []
        assert m.pip == []
        assert m.commands == []
        assert m.is_empty

    def test_from_dict_none(self):
        m = InstallManifest.from_dict(None)
        assert m.is_empty

    def test_from_dict_full(self):
        m = InstallManifest.from_dict(
            {
                "npm": ["@anthropic/mcp-playwright"],
                "pip": ["black"],
                "commands": ["docker", "node"],
            }
        )
        assert m.npm == ["@anthropic/mcp-playwright"]
        assert m.pip == ["black"]
        assert m.commands == ["docker", "node"]
        assert not m.is_empty

    def test_roundtrip(self):
        original = {"npm": ["pkg-a"], "pip": ["pkg-b"], "commands": ["cmd-c"]}
        m = InstallManifest.from_dict(original)
        d = m.to_dict()
        assert d == original

    def test_to_dict_omits_empty(self):
        m = InstallManifest(npm=["pkg-a"])
        d = m.to_dict()
        assert d == {"npm": ["pkg-a"]}
        assert "pip" not in d
        assert "commands" not in d
