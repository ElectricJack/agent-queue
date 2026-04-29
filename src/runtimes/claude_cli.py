"""Claude CLI platform — runs tasks via the `claude` CLI in -p stream-json mode.

Functionally equivalent to :class:`ClaudeSDKRuntime` (the SDK is a wrapper
around this CLI), but reaches the agent without the SDK as an intermediary.
This is the path of least resistance when the SDK lacks a feature the CLI
exposes.

Capabilities are identical to the SDK's full set in v1; the distinction
exists so future divergence (e.g., a tmux-backed LIVE_TAKEOVER cap) can
be added to one platform without affecting the other.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import ClassVar

from src.logging_config import get_correlation_context
from src.models import AgentOutput, AgentResult, TaskContext
from src.runtimes._subprocess import (
    isolated_env,
    parse_ndjson_line,
    run_streaming_subprocess,
)
from src.runtimes.base import Capability, MessageCallback, Runtime

logger = logging.getLogger(__name__)


def _classify_error_result(error_msg: str) -> AgentResult:
    """Classify a CLI error message into the appropriate AgentResult.

    Mirrors the ClaudeSDKRuntime classifier so behavior is consistent
    across the two Claude platforms.
    """
    lower = error_msg.lower()
    if "hit your limit" in lower or "resets " in lower:
        return AgentResult.PAUSED_RATE_LIMIT
    if "rate_limit" in lower or "rate limit" in lower or "429" in lower:
        return AgentResult.PAUSED_RATE_LIMIT
    if "overloaded" in lower or "503" in lower or "capacity" in lower:
        return AgentResult.PAUSED_RATE_LIMIT
    if "token" in lower or "quota" in lower:
        return AgentResult.PAUSED_TOKENS
    return AgentResult.FAILED


class ClaudeCLIRuntime(Runtime):
    """Runtime that wraps the `claude -p --output-format stream-json` CLI."""

    name: ClassVar[str] = "claude_cli"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(Capability)

    def __init__(self, profile=None, llm_logger=None):
        self._profile = profile
        self._llm_logger = llm_logger
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._session_id: str | None = None
        # Accumulated NDJSON events; used to build summary + token count.
        self._events: list[dict] = []
        self._on_message: MessageCallback | None = None

    async def start(self, task: TaskContext) -> None:
        self._task = task
        self._cancel_event.clear()
        self._events = []
        self._session_id = None
        ctx = get_correlation_context()
        logger.info(
            "ClaudeCLI platform starting for task %s",
            ctx.get("task_id", task.task_id if hasattr(task, "task_id") else "unknown"),
        )

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        assert self._task is not None, "wait() called before start()"
        self._on_message = on_message

        prompt = self._build_prompt()
        cmd = self._build_command()
        env = isolated_env()
        cwd = self._task.checkout_path or "."

        # Dispatch tasks spawned by _on_line so we can await them after the subprocess exits.
        _dispatch_tasks: list[asyncio.Task] = []

        def _on_line(line: bytes) -> None:
            event = parse_ndjson_line(line)
            if event is None:
                return
            self._events.append(event)
            # Use create_task rather than run_coroutine_threadsafe: _on_line is called
            # from within the async event loop (by run_streaming_subprocess's stdout
            # reader), so create_task schedules the coroutine on the same loop
            # without the thread-safety overhead.
            task = asyncio.create_task(self._dispatch(event))
            _dispatch_tasks.append(task)

        cmd_with_prompt = [*cmd, prompt]

        start_time = time.monotonic()
        exit_code: int | None = None
        try:
            try:
                exit_code = await run_streaming_subprocess(
                    cmd=cmd_with_prompt,
                    env=env,
                    cwd=cwd,
                    on_line=_on_line,
                    cancel_event=self._cancel_event,
                )
            except Exception as e:
                logger.exception("ClaudeCLI subprocess failed")
                return self._build_failure_output(str(e))
        finally:
            # Drain any pending dispatch tasks so callbacks finish before we return,
            # even when run_streaming_subprocess raised.
            if _dispatch_tasks:
                await asyncio.gather(*_dispatch_tasks, return_exceptions=True)

        if self._cancel_event.is_set():
            return AgentOutput(
                result=AgentResult.FAILED,
                summary="Cancelled",
                error_message="Agent was stopped",
            )

        result_event = self._final_result_event()
        if result_event is None:
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=f"ClaudeCLI exited with code {exit_code} before emitting a result event",
            )

        subtype = result_event.get("subtype")
        if subtype == "success":
            text = result_event.get("result", "")
            usage = result_event.get("usage") or {}
            tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
            output = AgentOutput(
                result=AgentResult.COMPLETED,
                summary=text,
                tokens_used=tokens,
            )
        else:
            text = result_event.get("result", "")
            output = AgentOutput(
                result=_classify_error_result(text),
                summary=text,
                error_message=text,
            )

        logger.info(
            "ClaudeCLI finished task %s in %.1fs (exit=%s, result=%s)",
            self._task.task_id,
            time.monotonic() - start_time,
            exit_code,
            output.result,
        )
        return output

    async def stop(self) -> None:
        self._cancel_event.set()

    async def is_alive(self) -> bool:
        return self._task is not None and not self._cancel_event.is_set()

    # ---------------- helpers ----------------

    def _build_command(self) -> list[str]:
        """Assemble the `claude` invocation (without the trailing prompt)."""
        cli = shutil.which("claude")
        if cli is None:
            raise RuntimeError("`claude` CLI not found in PATH")
        cmd = [cli, "-p", "--output-format", "stream-json", "--verbose"]
        if self._profile is not None:
            mode = getattr(self._profile, "permission_mode", "") or ""
            if mode:
                cmd += ["--permission-mode", mode]
            model = getattr(self._profile, "model", "") or ""
            if model:
                cmd += ["--model", model]
        return cmd

    def _build_prompt(self) -> str:
        """Assemble the agent prompt from TaskContext (mirrors ClaudeSDKRuntime)."""
        assert self._task is not None
        parts: list[str] = []
        if self._task.l0_role:
            parts.append(self._task.l0_role)
        if self._task.l1_facts:
            parts.append(self._task.l1_facts)
        parts.append(self._task.description)
        if self._task.acceptance_criteria:
            parts.append("## Acceptance Criteria")
            for c in self._task.acceptance_criteria:
                parts.append(f"- {c}")
        if self._task.test_commands:
            parts.append("## Test Commands")
            for cmd in self._task.test_commands:
                parts.append(f"- `{cmd}`")
        if self._task.attached_context:
            parts.append("## Additional Context")
            for ctx in self._task.attached_context:
                parts.append(f"- {ctx}")
        return "\n\n".join(p for p in parts if p)

    async def _dispatch(self, event: dict) -> None:
        """Dispatch an NDJSON event to the on_message callback as readable text.

        Verified against claude v2.1.116 live output:
        - system/hook_started, system/hook_response → ignored.
        - system/init → captures session_id, no callback.
        - rate_limit_event → ignored (streams inline as a non-error signal).
        - assistant.message.content blocks: text → emit; tool_use → emit name.
        - result.subtype=success → emit final result text.
        """
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            self._session_id = event.get("session_id")
            return
        if etype == "rate_limit_event":
            status = (event.get("rate_limit_info") or {}).get("status")
            if status and status != "allowed":
                logger.warning("ClaudeCLI rate-limit signal: %s", event.get("rate_limit_info"))
            return
        if self._on_message is None:
            return
        if etype == "assistant":
            for block in event.get("message", {}).get("content", []) or []:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        await self._on_message(text)
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    await self._on_message(f"-# {name}")
        elif etype == "result":
            text = event.get("result", "")
            if text:
                await self._on_message(text)

    def _final_result_event(self) -> dict | None:
        for event in reversed(self._events):
            if event.get("type") == "result":
                return event
        return None

    def _build_failure_output(self, error: str) -> AgentOutput:
        return AgentOutput(
            result=_classify_error_result(error),
            summary=error,
            error_message=error,
        )
