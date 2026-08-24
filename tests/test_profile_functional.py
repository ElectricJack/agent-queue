"""Profile-system tests: data-layer correctness and install-manifest checks.

The tool-restriction / skill / MCP *functional* suites that used to live here
drove ``ClaudeSDKRuntime`` — they launched the real Claude CLI through the
Agent SDK and asserted on what the model did with a restricted tool set. That
runtime was deleted in the tmux-harness migration, so those tests were removed
rather than ported: agents now run as tmux sessions, and asserting the same
behaviour there needs a live session provider and tmux, which is a new suite
rather than a port. Tracked as a coverage gap in the migration commit.

What remains needs no agent process at all.
"""

from __future__ import annotations

import pytest

from src.models import TaskContext


class TestMCPTypeFix:
    """Verify TaskContext.mcp_servers is dict[str, dict], not list[dict]."""

    def test_default_task_context_has_empty_dict(self):
        ctx = TaskContext(description="test")
        assert ctx.mcp_servers == {}
        assert isinstance(ctx.mcp_servers, dict)

    def test_accepts_named_server_dict(self):
        servers = {
            "playwright": {"command": "npx", "args": ["@anthropic/mcp-playwright"]},
            "filesystem": {"command": "npx", "args": ["@anthropic/mcp-filesystem", "/tmp"]},
        }
        ctx = TaskContext(description="test", mcp_servers=servers)
        assert len(ctx.mcp_servers) == 2
        assert "playwright" in ctx.mcp_servers
        assert "filesystem" in ctx.mcp_servers

    def test_preserves_server_names(self):
        servers = {
            "my-server": {"command": "node", "args": ["server.js"]},
        }
        ctx = TaskContext(description="test", mcp_servers=servers)
        assert "my-server" in ctx.mcp_servers
        assert ctx.mcp_servers["my-server"]["command"] == "node"


# ===========================================================================
# Part 2: Tool Restriction — Positive (agent CAN use allowed tools)
# ===========================================================================


# ===========================================================================


class TestCheckProfileFunctional:
    """Test install manifest validation against real system state."""

    @pytest.fixture
    async def handler(self, tmp_path):
        from src.commands.handler import CommandHandler
        from src.config import AppConfig
        from src.orchestrator import Orchestrator

        config = AppConfig(
            database_path=str(tmp_path / "test.db"),
            workspace_dir=str(tmp_path / "workspaces"),
            data_dir=str(tmp_path / "data"),
        )
        orch = Orchestrator(config)
        await orch.initialize()
        handler = CommandHandler(orch, config)
        yield handler
        await orch.wait_for_running_tasks(timeout=5)
        await orch.shutdown()

    async def test_valid_commands(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "cmd-valid",
                "name": "Cmd Valid",
                "install": {"commands": ["python3", "git"]},
            },
        )
        result = await handler.execute("check_profile", {"profile_id": "cmd-valid"})
        assert result["valid"] is True
        assert result["issues"] == []

    async def test_invalid_command(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "cmd-invalid",
                "name": "Cmd Invalid",
                "install": {"commands": ["xyzzy-no-such-command-99"]},
            },
        )
        result = await handler.execute("check_profile", {"profile_id": "cmd-invalid"})
        assert result["valid"] is False
        assert any("xyzzy-no-such-command-99" in i for i in result["issues"])

    async def test_valid_pip_package(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "pip-valid",
                "name": "Pip Valid",
                "install": {"pip": ["pytest"]},
            },
        )
        result = await handler.execute("check_profile", {"profile_id": "pip-valid"})
        assert result["valid"] is True
        assert result["issues"] == []

    async def test_invalid_pip_package(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "pip-invalid",
                "name": "Pip Invalid",
                "install": {"pip": ["xyzzy-no-such-package-99"]},
            },
        )
        result = await handler.execute("check_profile", {"profile_id": "pip-invalid"})
        assert result["valid"] is False
        assert any("xyzzy-no-such-package-99" in i for i in result["issues"])

    async def test_invalid_npm_package(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "npm-invalid",
                "name": "NPM Invalid",
                "install": {"npm": ["@xyzzy/no-such-pkg-99"]},
            },
        )
        result = await handler.execute("check_profile", {"profile_id": "npm-invalid"})
        assert result["valid"] is False
        # Should fail whether npm is installed (package not found) or not (npm not available)
        assert len(result["issues"]) >= 1

    async def test_mixed_valid_and_invalid(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "mixed",
                "name": "Mixed",
                "install": {
                    "commands": ["python3", "xyzzy-no-such-cmd"],
                    "pip": ["pytest", "xyzzy-no-such-pkg"],
                },
            },
        )
        result = await handler.execute("check_profile", {"profile_id": "mixed"})
        assert result["valid"] is False
        # At least 2 issues: one bad command + one bad pip package
        assert len(result["issues"]) >= 2

    async def test_empty_manifest_always_valid(self, handler):
        await handler.execute(
            "create_profile",
            {
                "id": "empty-install",
                "name": "Empty Install",
                "install": {},
            },
        )
        result = await handler.execute("check_profile", {"profile_id": "empty-install"})
        assert result["valid"] is True
        assert result["issues"] == []
