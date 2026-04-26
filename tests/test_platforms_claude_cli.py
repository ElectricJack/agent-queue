"""Tests for ClaudeCLIPlatform.

Subprocess invocation is mocked via `_subprocess.run_streaming_subprocess`
so tests don't depend on the local `claude` CLI being authenticated.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from src.platforms.base import Capability
from src.platforms.claude_cli import ClaudeCLIPlatform
from src.models import AgentResult, TaskContext


def _make_task(**overrides) -> TaskContext:
    defaults = {
        "description": "Implement the foo feature",
        "task_id": "t-1",
        "checkout_path": "/tmp/test-workspace",
    }
    defaults.update(overrides)
    return TaskContext(**defaults)


class TestClaudeCLIPlatformContract:
    def test_name_and_capabilities(self):
        assert ClaudeCLIPlatform.name == "claude_cli"
        # Same surface as the SDK in v1.
        assert ClaudeCLIPlatform.capabilities == frozenset(Capability)

    @pytest.mark.asyncio
    async def test_lifecycle_basic(self):
        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        assert await platform.is_alive()
        await platform.stop()
        assert not await platform.is_alive()


def _ndjson_lines(*objs) -> list[bytes]:
    return [(json.dumps(o) + "\n").encode() for o in objs]


class TestClaudeCLIPlatformWait:
    @pytest.mark.asyncio
    async def test_happy_path_completed(self):
        emitted_lines = _ndjson_lines(
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}},
            {
                "type": "result",
                "subtype": "success",
                "result": "Done.",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 0

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.COMPLETED
        assert "Done." in output.summary
        assert output.tokens_used == 150

    @pytest.mark.asyncio
    async def test_cancellation_returns_failed(self):
        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            await cancel_event.wait()
            return -15

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())

        async def cancel_soon():
            await asyncio.sleep(0.05)
            await platform.stop()

        asyncio.create_task(cancel_soon())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.FAILED
        assert "cancelled" in output.summary.lower() or "stopped" in (output.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_rate_limit_error_on_subprocess_stderr(self):
        emitted_lines = _ndjson_lines(
            {"type": "result", "subtype": "error", "result": "HTTP 429: rate limit exceeded"},
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 1

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.PAUSED_RATE_LIMIT

    @pytest.mark.asyncio
    async def test_token_quota_exhaustion(self):
        emitted_lines = _ndjson_lines(
            {"type": "result", "subtype": "error", "result": "token quota exceeded"},
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 1

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.PAUSED_TOKENS

    @pytest.mark.asyncio
    async def test_subprocess_crash_no_ndjson(self):
        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            return 137  # killed

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.FAILED

    @pytest.mark.asyncio
    async def test_rate_limit_event_streamed_inline_is_ignored(self):
        """rate_limit_event types stream as informational signals, not errors."""
        emitted_lines = _ndjson_lines(
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}]}},
            {
                "type": "result",
                "subtype": "success",
                "result": "Hi",
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 0

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.COMPLETED
        assert output.tokens_used == 10

    @pytest.mark.asyncio
    async def test_streams_messages_via_callback(self):
        emitted_lines = _ndjson_lines(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Step 1"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Step 2"}]}},
            {"type": "result", "subtype": "success", "result": "Done.", "usage": {"input_tokens": 10, "output_tokens": 10}},
        )
        received: list[str] = []

        async def on_message(text: str) -> None:
            received.append(text)

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 0

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            await platform.wait(on_message=on_message)

        # At least the two assistant messages reached the callback.
        joined = "\n".join(received)
        assert "Step 1" in joined
        assert "Step 2" in joined
