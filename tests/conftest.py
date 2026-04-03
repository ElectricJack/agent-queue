"""Root-level test fixtures shared across all test modules."""

from __future__ import annotations

import asyncio
import shutil

import pytest

from src.adapters.claude import ClaudeAdapter, ClaudeAdapterConfig
from src.models import TaskContext


@pytest.fixture(scope="session")
def claude_cli_path() -> str:
    """Resolve the ``claude`` CLI binary. Skip if missing."""
    path = shutil.which("claude")
    if path is None:
        pytest.skip("claude CLI not found on PATH — skipping functional tests")
    return path


@pytest.fixture(scope="session")
def claude_cli_authenticated(claude_cli_path: str, tmp_path_factory) -> str:
    """Verify the Claude Agent SDK can authenticate and complete a trivial prompt.

    Uses the same code path as the real application (ClaudeAdapter + SDK),
    not subprocess. Runs once per session; skips all functional tests if
    authentication fails.
    """
    workspace = str(tmp_path_factory.mktemp("auth_check"))
    adapter = ClaudeAdapter(
        ClaudeAdapterConfig(
            model="claude-haiku-4-5-20251001",
            permission_mode="bypassPermissions",
            allowed_tools=[],
        )
    )
    ctx = TaskContext(
        description="respond with only: ok",
        task_id="auth-check",
        checkout_path=workspace,
    )

    async def _check():
        await adapter.start(ctx)
        return await adapter.wait()

    try:
        result = asyncio.get_event_loop().run_until_complete(_check())
    except RuntimeError:
        # No running loop — create one
        result = asyncio.run(_check())
    except Exception as exc:
        pytest.skip(f"Claude SDK auth check failed: {exc}")
        return claude_cli_path  # unreachable, keeps type checker happy

    from src.models import AgentResult

    if result.result == AgentResult.FAILED:
        pytest.skip(f"Claude SDK auth check failed: {result.error_message or result.summary}")

    return claude_cli_path


@pytest.fixture(scope="session")
def npm_available() -> str:
    """Resolve ``npx`` binary. Skip if missing."""
    path = shutil.which("npx")
    if path is None:
        pytest.skip("npx not found on PATH — skipping MCP functional tests")
    return path
