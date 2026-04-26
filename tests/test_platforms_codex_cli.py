"""Tests for CodexCLIPlatform.

Same mocking approach as ClaudeCLIPlatform — patch run_streaming_subprocess
so tests are independent of Codex authentication and CLI surface drift.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from src.platforms.base import Capability
from src.platforms.codex_cli import CodexCLIPlatform
from src.models import AgentResult, TaskContext


def _make_task(**overrides) -> TaskContext:
    defaults = {"description": "Implement foo", "task_id": "t-1", "checkout_path": "/tmp/x"}
    defaults.update(overrides)
    return TaskContext(**defaults)


def _ndjson_lines(*objs) -> list[bytes]:
    return [(json.dumps(o) + "\n").encode() for o in objs]


class TestCodexCLIPlatformContract:
    def test_name(self):
        assert CodexCLIPlatform.name == "codex_cli"

    def test_capabilities_includes_streaming_json(self):
        assert Capability.STREAMING_JSON in CodexCLIPlatform.capabilities

    def test_capabilities_excludes_skills_and_memory_md(self):
        # Codex doesn't share Claude's skills / MEMORY.md infrastructure.
        assert Capability.SKILLS not in CodexCLIPlatform.capabilities
        assert Capability.MEMORY_MD not in CodexCLIPlatform.capabilities

    @pytest.mark.asyncio
    async def test_lifecycle_basic(self):
        platform = CodexCLIPlatform(profile=None)
        await platform.start(_make_task())
        assert await platform.is_alive()
        await platform.stop()
        assert not await platform.is_alive()


class TestCodexCLIPlatformWait:
    @pytest.mark.asyncio
    async def test_happy_path_simple_message(self):
        """Verified shape from `codex exec --json` v0.125.0 — single agent_message turn."""
        emitted = _ndjson_lines(
            {"type": "thread.started", "thread_id": "019dc856-0873-7a82-9ea1-c8456914f8d1"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "2 + 2 equals 4."},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 29325,
                    "cached_input_tokens": 7552,
                    "output_tokens": 12,
                    "reasoning_output_tokens": 0,
                },
            },
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted:
                on_line(line)
            return 0

        platform = CodexCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.codex_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.COMPLETED
        assert "2 + 2 equals 4." in output.summary
        # input + output + reasoning_output (cached_input is a subset, not additive)
        assert output.tokens_used == 29325 + 12 + 0

    @pytest.mark.asyncio
    async def test_happy_path_with_tool_items(self):
        """Tool-using turn: agent_message + file_change + command_execution items."""
        emitted = _ndjson_lines(
            {"type": "thread.started", "thread_id": "thr-2"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "I'll create the file."},
            },
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "file_change",
                    "changes": [{"path": "/tmp/x/hello.txt", "kind": "add"}],
                    "status": "in_progress",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "file_change",
                    "changes": [{"path": "/tmp/x/hello.txt", "kind": "add"}],
                    "status": "completed",
                },
            },
            {
                "type": "item.started",
                "item": {
                    "id": "item_2",
                    "type": "command_execution",
                    "command": "/bin/bash -lc 'cat hello.txt'",
                    "status": "in_progress",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "item_2",
                    "type": "command_execution",
                    "command": "/bin/bash -lc 'cat hello.txt'",
                    "aggregated_output": "hi\n",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {"id": "item_3", "type": "agent_message", "text": "Created hello.txt with hi."},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 90884,
                    "cached_input_tokens": 88192,
                    "output_tokens": 138,
                    "reasoning_output_tokens": 0,
                },
            },
        )

        received: list[str] = []

        async def on_message(text: str) -> None:
            received.append(text)

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted:
                on_line(line)
            return 0

        platform = CodexCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.codex_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait(on_message=on_message)

        assert output.result == AgentResult.COMPLETED
        assert "I'll create the file." in output.summary
        assert "Created hello.txt with hi." in output.summary
        assert output.tokens_used == 90884 + 138 + 0
        joined = "\n".join(received)
        assert "I'll create the file." in joined
        assert "Created hello.txt" in joined
        assert "file_change" in joined or "/tmp/x/hello.txt" in joined
        assert "command_execution" in joined or "cat hello.txt" in joined

    @pytest.mark.asyncio
    async def test_turn_failed_event(self):
        emitted = _ndjson_lines(
            {"type": "thread.started", "thread_id": "thr-3"},
            {"type": "turn.started"},
            {"type": "turn.failed", "error": {"message": "Unauthorized"}},
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted:
                on_line(line)
            return 1

        platform = CodexCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.codex_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()
        assert output.result == AgentResult.FAILED
        assert "Unauthorized" in (output.error_message or "")

    @pytest.mark.asyncio
    async def test_inline_error_events_are_ignored(self):
        emitted = _ndjson_lines(
            {"type": "thread.started", "thread_id": "thr-4"},
            {"type": "error", "message": "Reconnecting... 1/5"},
            {"type": "error", "message": "Reconnecting... 2/5"},
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "OK"},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0},
            },
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted:
                on_line(line)
            return 0

        platform = CodexCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.codex_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()
        assert output.result == AgentResult.COMPLETED

    @pytest.mark.asyncio
    async def test_no_terminal_event_is_failed(self):
        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            return 1

        platform = CodexCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.codex_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()
        assert output.result == AgentResult.FAILED

    @pytest.mark.asyncio
    async def test_cancellation(self):
        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            await cancel_event.wait()
            return -15

        platform = CodexCLIPlatform(profile=None)
        await platform.start(_make_task())

        async def cancel_soon():
            await asyncio.sleep(0.05)
            await platform.stop()

        asyncio.create_task(cancel_soon())
        with patch("src.platforms.codex_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.FAILED
