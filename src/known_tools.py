"""Registry of known Claude Code tools and well-known MCP servers.

Provides validation helpers for agent profile configuration. Tool names
are soft-validated — unrecognized names produce warnings, not errors,
so custom MCP-provided tools still work.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Claude Code built-in tools
# ---------------------------------------------------------------------------

CLAUDE_CODE_TOOLS: dict[str, str] = {
    "Read": "Read file contents",
    "Write": "Write/create files",
    "Edit": "Edit existing files",
    "Bash": "Run shell commands",
    "Glob": "Find files by pattern",
    "Grep": "Search file contents",
    "WebSearch": "Search the web",
    "WebFetch": "Fetch and process URL content",
    "NotebookEdit": "Edit Jupyter notebooks",
    "Agent": "Launch sub-agents",
    "TodoRead": "Read task list",
    "TodoWrite": "Write to task list",
    "Skill": "Execute a skill/slash command",
    "TaskCreate": "Create tracked tasks",
    "TaskUpdate": "Update tracked tasks",
    "TaskList": "List tracked tasks",
    "TaskGet": "Get task details",
    "EnterWorktree": "Create isolated git worktree",
}

KNOWN_TOOL_NAMES: frozenset[str] = frozenset(CLAUDE_CODE_TOOLS)


# ---------------------------------------------------------------------------
# Well-known MCP servers
# ---------------------------------------------------------------------------

KNOWN_MCP_SERVERS: dict[str, dict] = {
    "playwright": {
        "description": "Browser automation for web testing",
        "npm_package": "@anthropic/mcp-playwright",
        "command": "npx",
        "args_template": ["@anthropic/mcp-playwright"],
    },
    "filesystem": {
        "description": "Extended filesystem operations",
        "npm_package": "@anthropic/mcp-filesystem",
        "command": "npx",
        "args_template": ["@anthropic/mcp-filesystem"],
    },
}

KNOWN_MCP_NAMES: frozenset[str] = frozenset(KNOWN_MCP_SERVERS)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_tool_names(tools: list[str]) -> list[str]:
    """Return unrecognized tool names (warnings, not errors)."""
    return [t for t in tools if t not in KNOWN_TOOL_NAMES]


# ---------------------------------------------------------------------------
# Install manifest
# ---------------------------------------------------------------------------


@dataclass
class InstallManifest:
    """Parsed install manifest from an AgentProfile.install dict.

    Schema::

        install:
          npm: ["@anthropic/mcp-playwright"]
          pip: ["black"]
          commands: ["docker", "node"]
    """

    npm: list[str] = field(default_factory=list)
    pip: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> InstallManifest:
        if not d:
            return cls()
        return cls(
            npm=list(d.get("npm", [])),
            pip=list(d.get("pip", [])),
            commands=list(d.get("commands", [])),
        )

    def to_dict(self) -> dict:
        result: dict = {}
        if self.npm:
            result["npm"] = list(self.npm)
        if self.pip:
            result["pip"] = list(self.pip)
        if self.commands:
            result["commands"] = list(self.commands)
        return result

    @property
    def is_empty(self) -> bool:
        return not self.npm and not self.pip and not self.commands
