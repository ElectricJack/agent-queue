"""Tests for ACPXRuntime.

Subprocess invocation is mocked via ``_subprocess.run_streaming_subprocess``
so tests don't depend on the local ``acpx`` CLI being installed.

These tests verify:
- Runtime contract: name, capabilities, requires_workspace, lifecycle
- ``profile.agent_name`` flows through to the subprocess argv
- ACP NDJSON event dispatch (session/update + tool_call) reaches
  the on_message callback
- ``stopReason`` parsing for completed / failed / cancelled
- Token-usage extraction from the final ACP response
- ``agent_name`` requirement: missing value raises a clear error
- Cancellation interrupts the subprocess and surfaces FAILED
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from src.models import AgentProfile, AgentResult, TaskContext
from src.runtimes.acpx import ACPXRuntime
from src.runtimes.base import Capability


def _make_task(**overrides) -> TaskContext:
    defaults = {
        "description": "Implement the foo feature",
        "task_id": "t-1",
        "checkout_path": "/tmp/test-workspace",
    }
    defaults.update(overrides)
    return TaskContext(**defaults)


def _ndjson_lines(*objs) -> list[bytes]:
    return [(json.dumps(o) + "\n").encode() for o in objs]


def _claude_profile(model: str = "") -> AgentProfile:
    """Profile pointing ACPX at the Claude agent — the most common case."""
    return AgentProfile(
        id="acpx-claude",
        name="ACPX Claude",
        runtime="acpx",
        agent_name="claude",
        model=model,
    )


class TestACPXRuntimeContract:
    def test_name_and_capabilities(self):
        assert ACPXRuntime.name == "acpx"
        # ACPX surfaces the underlying agent's full capability set in v1;
        # per-agent tightening is a follow-up.
        assert ACPXRuntime.capabilities == frozenset(Capability)

    def test_requires_workspace_true(self):
        # ACPX runs the underlying agent against a checkout, so the
        # orchestrator must provision a workspace.
        assert ACPXRuntime.requires_workspace is True

    @pytest.mark.asyncio
    async def test_lifecycle_basic(self):
        runtime = ACPXRuntime(profile=_claude_profile())
        await runtime.start(_make_task())
        assert await runtime.is_alive()
        await runtime.stop()
        assert not await runtime.is_alive()


class TestACPXRuntimeBuildCommand:
    def test_command_includes_agent_name(self):
        runtime = ACPXRuntime(profile=_claude_profile())
        # _build_command needs the task set since it reads no task fields,
        # but to be safe we go through start() first.
        captured: dict = {}

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            captured["cmd"] = cmd
            return 1  # No NDJSON => failure path is fine

        async def _go():
            await runtime.start(_make_task())
            with patch(
                "src.runtimes.acpx.shutil.which",
                return_value="/usr/bin/acpx",
            ), patch(
                "src.runtimes.acpx.run_streaming_subprocess",
                side_effect=fake_run,
            ):
                await runtime.wait()

        asyncio.run(_go())

        cmd = captured["cmd"]
        # Verify the command shape: acpx --format json --approve-all <agent> exec <prompt>
        assert "--format" in cmd and "json" in cmd
        assert "--approve-all" in cmd
        assert "claude" in cmd  # the agent_name
        assert "exec" in cmd
        # The prompt is the last arg.
        assert cmd[-1] == "Implement the foo feature"

    def test_command_includes_model_when_profile_sets_it(self):
        runtime = ACPXRuntime(profile=_claude_profile(model="claude-sonnet-4-6"))
        captured: dict = {}

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            captured["cmd"] = cmd
            return 1

        async def _go():
            await runtime.start(_make_task())
            with patch(
                "src.runtimes.acpx.shutil.which",
                return_value="/usr/bin/acpx",
            ), patch(
                "src.runtimes.acpx.run_streaming_subprocess",
                side_effect=fake_run,
            ):
                await runtime.wait()

        asyncio.run(_go())
        assert "--model" in captured["cmd"]
        assert "claude-sonnet-4-6" in captured["cmd"]

    @pytest.mark.asyncio
    async def test_missing_agent_name_raises(self):
        # An ACPX runtime with no agent_name on the profile is a parser
        # bug (validation should catch it at sync-time), but we defend at
        # runtime too.  _build_command via wait() raises RuntimeError.
        bad_profile = AgentProfile(id="bad", name="Bad", runtime="acpx", agent_name="")
        runtime = ACPXRuntime(profile=bad_profile)
        await runtime.start(_make_task())
        with pytest.raises(RuntimeError, match="agent_name"):
            await runtime.wait()


class TestACPXRuntimeWait:
    @pytest.mark.asyncio
    async def test_happy_path_completed(self):
        # ACP final response carries stopReason + result text + usage.
        emitted_lines = _ndjson_lines(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"text": "Hello"},
                    }
                },
            },
            {
                "stopReason": "completed",
                "result": "Done.",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 0

        runtime = ACPXRuntime(profile=_claude_profile())
        await runtime.start(_make_task())
        with patch("src.runtimes.acpx.shutil.which", return_value="/usr/bin/acpx"), \
                patch("src.runtimes.acpx.run_streaming_subprocess", side_effect=fake_run):
            output = await runtime.wait()

        assert output.result == AgentResult.COMPLETED
        assert "Done." in output.summary
        assert output.tokens_used == 150

    @pytest.mark.asyncio
    async def test_stop_reason_in_nested_params_result(self):
        """Some ACP servers nest stopReason under params.result instead of top-level."""
        emitted_lines = _ndjson_lines(
            {
                "method": "result",
                "params": {
                    "result": {
                        "stopReason": "completed",
                        "result": "ok",
                        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                    }
                },
            },
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 0

        runtime = ACPXRuntime(profile=_claude_profile())
        await runtime.start(_make_task())
        with patch("src.runtimes.acpx.shutil.which", return_value="/usr/bin/acpx"), \
                patch("src.runtimes.acpx.run_streaming_subprocess", side_effect=fake_run):
            output = await runtime.wait()
        assert output.result == AgentResult.COMPLETED
        assert output.tokens_used == 30

    @pytest.mark.asyncio
    async def test_failed_stop_reason(self):
        emitted_lines = _ndjson_lines(
            {
                "stopReason": "failed",
                "result": "subprocess error: model unavailable",
            },
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 1

        runtime = ACPXRuntime(profile=_claude_profile())
        await runtime.start(_make_task())
        with patch("src.runtimes.acpx.shutil.which", return_value="/usr/bin/acpx"), \
                patch("src.runtimes.acpx.run_streaming_subprocess", side_effect=fake_run):
            output = await runtime.wait()
        assert output.result == AgentResult.FAILED
        assert "model unavailable" in (output.error_message or "")

    @pytest.mark.asyncio
    async def test_rate_limit_classified_paused(self):
        emitted_lines = _ndjson_lines(
            {
                "stopReason": "failed",
                "result": "HTTP 429: rate limit exceeded",
            },
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 1

        runtime = ACPXRuntime(profile=_claude_profile())
        await runtime.start(_make_task())
        with patch("src.runtimes.acpx.shutil.which", return_value="/usr/bin/acpx"), \
                patch("src.runtimes.acpx.run_streaming_subprocess", side_effect=fake_run):
            output = await runtime.wait()
        assert output.result == AgentResult.PAUSED_RATE_LIMIT

    @pytest.mark.asyncio
    async def test_token_quota_classified_paused(self):
        emitted_lines = _ndjson_lines(
            {
                "stopReason": "failed",
                "result": "token quota exceeded",
            },
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 1

        runtime = ACPXRuntime(profile=_claude_profile())
        await runtime.start(_make_task())
        with patch("src.runtimes.acpx.shutil.which", return_value="/usr/bin/acpx"), \
                patch("src.runtimes.acpx.run_streaming_subprocess", side_effect=fake_run):
            output = await runtime.wait()
        assert output.result == AgentResult.PAUSED_TOKENS

    @pytest.mark.asyncio
    async def test_subprocess_crashes_without_stop_reason(self):
        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            return 137  # killed

        runtime = ACPXRuntime(profile=_claude_profile())
        await runtime.start(_make_task())
        with patch("src.runtimes.acpx.shutil.which", return_value="/usr/bin/acpx"), \
                patch("src.runtimes.acpx.run_streaming_subprocess", side_effect=fake_run):
            output = await runtime.wait()
        assert output.result == AgentResult.FAILED
        assert "stopReason" in (output.error_message or "")

    @pytest.mark.asyncio
    async def test_cancellation_returns_failed(self):
        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            await cancel_event.wait()
            return -15  # SIGTERM

        runtime = ACPXRuntime(profile=_claude_profile())
        await runtime.start(_make_task())

        async def cancel_soon():
            await asyncio.sleep(0.05)
            await runtime.stop()

        asyncio.create_task(cancel_soon())
        with patch("src.runtimes.acpx.shutil.which", return_value="/usr/bin/acpx"), \
                patch("src.runtimes.acpx.run_streaming_subprocess", side_effect=fake_run):
            output = await runtime.wait()
        assert output.result == AgentResult.FAILED


class TestACPXRuntimeStreamsMessages:
    @pytest.mark.asyncio
    async def test_agent_message_chunks_reach_callback(self):
        emitted_lines = _ndjson_lines(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"text": "Step 1"},
                    }
                },
            },
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"text": "Step 2"},
                    }
                },
            },
            {"stopReason": "completed", "result": "Done.", "usage": {}},
        )
        received: list[str] = []

        async def on_message(text: str) -> None:
            received.append(text)

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 0

        runtime = ACPXRuntime(profile=_claude_profile())
        await runtime.start(_make_task())
        with patch("src.runtimes.acpx.shutil.which", return_value="/usr/bin/acpx"), \
                patch("src.runtimes.acpx.run_streaming_subprocess", side_effect=fake_run):
            await runtime.wait(on_message=on_message)

        joined = "\n".join(received)
        assert "Step 1" in joined
        assert "Step 2" in joined

    @pytest.mark.asyncio
    async def test_tool_call_emits_tool_name(self):
        emitted_lines = _ndjson_lines(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCall": {"title": "Read", "name": "Read"},
                    }
                },
            },
            {"stopReason": "completed", "result": "ok", "usage": {}},
        )
        received: list[str] = []

        async def on_message(text: str) -> None:
            received.append(text)

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 0

        runtime = ACPXRuntime(profile=_claude_profile())
        await runtime.start(_make_task())
        with patch("src.runtimes.acpx.shutil.which", return_value="/usr/bin/acpx"), \
                patch("src.runtimes.acpx.run_streaming_subprocess", side_effect=fake_run):
            await runtime.wait(on_message=on_message)

        # Tool name surfaced in the Discord-friendly "-# {name}" format.
        assert any("Read" in m for m in received)

    @pytest.mark.asyncio
    async def test_thinking_chunks_suppressed_from_live_stream(self):
        emitted_lines = _ndjson_lines(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"text": "Pondering..."},
                    }
                },
            },
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"text": "Answer"},
                    }
                },
            },
            {"stopReason": "completed", "result": "Answer", "usage": {}},
        )
        received: list[str] = []

        async def on_message(text: str) -> None:
            received.append(text)

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kw):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 0

        runtime = ACPXRuntime(profile=_claude_profile())
        await runtime.start(_make_task())
        with patch("src.runtimes.acpx.shutil.which", return_value="/usr/bin/acpx"), \
                patch("src.runtimes.acpx.run_streaming_subprocess", side_effect=fake_run):
            await runtime.wait(on_message=on_message)

        joined = "\n".join(received)
        assert "Answer" in joined
        # Thinking content should NOT have been streamed to the callback.
        assert "Pondering" not in joined


class TestACPXRuntimeRegistryIntegration:
    """ACPXRuntime is wired into the default registry."""

    def test_default_registry_includes_acpx(self):
        from src.runtimes import default_registry

        registry = default_registry()
        assert "acpx" in registry.names()
        cls = registry.get("acpx")
        assert cls is ACPXRuntime

    def test_registry_create_returns_acpx_instance(self):
        from src.runtimes import default_registry

        registry = default_registry()
        runtime = registry.create("acpx", profile=_claude_profile())
        assert isinstance(runtime, ACPXRuntime)
