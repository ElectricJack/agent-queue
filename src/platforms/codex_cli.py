"""Codex CLI platform — runs tasks via the `codex exec --json` CLI.

Capability set: STREAMING_JSON, RESUME, THINKING, MCP, PLAN_MODE.

Verified event schema (codex v0.125.0, ChatGPT auth, both message-only and
tool-using runs):

- ``thread.started``  — once at start, has ``thread_id``.
- ``turn.started``    — turn boundary, no payload of interest.
- ``item.started``    — tool-like items (file_change, command_execution)
                         emit a started event before completion. Pure
                         agent_message items skip this and only emit
                         item.completed.
- ``item.completed``  — wraps an ``item`` with ``type`` switching the shape:
                         * ``agent_message`` — ``{id, type, text}``
                           assistant's user-facing text.
                         * ``file_change`` — ``{id, type, changes, status}``
                           (changes = list of ``{path, kind}``).
                         * ``command_execution`` — ``{id, type, command,
                           aggregated_output, exit_code, status}``.
                         Other item types may exist; dispatcher adds
                         branches as encountered.
- ``error``           — non-fatal, streams inline (transient connectivity).
                         Don't classify as failure on its own.
- ``turn.failed``     — terminal failure with ``error.message``.
- ``turn.completed``  — terminal success. ``usage`` has ``input_tokens``,
                         ``cached_input_tokens`` (informational subset of
                         input_tokens, NOT additive), ``output_tokens``,
                         ``reasoning_output_tokens``.  Token accounting:
                         ``input_tokens + output_tokens +
                         reasoning_output_tokens``.

Codex requires being inside a trusted directory or ``--skip-git-repo-check``;
agent-queue task workspaces aren't always git repos, so we always pass the flag.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import ClassVar

from src.logging_config import get_correlation_context
from src.models import AgentOutput, AgentResult, TaskContext
from src.platforms._subprocess import (
    isolated_env,
    parse_ndjson_line,
    run_streaming_subprocess,
)
from src.platforms.base import Capability, MessageCallback, Platform

logger = logging.getLogger(__name__)

# Terminal events that end the run.
_TERMINAL_EVENT_TYPES = frozenset({"turn.completed", "turn.failed"})


def _classify_error_result(error_msg: str) -> AgentResult:
    lower = error_msg.lower()
    if "rate limit" in lower or "429" in lower or "rate_limit" in lower:
        return AgentResult.PAUSED_RATE_LIMIT
    if "quota" in lower or "token limit" in lower:
        return AgentResult.PAUSED_TOKENS
    if "401" in lower or "unauthorized" in lower or "missing bearer" in lower:
        return AgentResult.FAILED
    return AgentResult.FAILED


class CodexCLIPlatform(Platform):
    """Platform that wraps the `codex exec --json` CLI."""

    name: ClassVar[str] = "codex_cli"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({
        Capability.STREAMING_JSON,
        Capability.RESUME,
        Capability.THINKING,
        Capability.MCP,
        Capability.PLAN_MODE,
    })

    def __init__(self, profile=None, llm_logger=None):
        self._profile = profile
        self._llm_logger = llm_logger
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._events: list[dict] = []
        self._on_message: MessageCallback | None = None
        self._thread_id: str | None = None

    async def start(self, task: TaskContext) -> None:
        self._task = task
        self._cancel_event.clear()
        self._events = []
        self._thread_id = None
        ctx = get_correlation_context()
        logger.info(
            "CodexCLI platform starting for task %s",
            ctx.get("task_id", task.task_id if hasattr(task, "task_id") else "unknown"),
        )

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        assert self._task is not None
        self._on_message = on_message

        prompt = self._build_prompt()
        cmd = self._build_command(prompt)
        env = isolated_env()
        cwd = self._task.checkout_path or "."

        # Match the ClaudeCLIPlatform pattern: collect dispatched coroutines so
        # we can drain them after the subprocess returns.  asyncio.create_task
        # is correct here — _on_line is invoked from within the event loop by
        # _read_stdout in run_streaming_subprocess.
        dispatch_tasks: list[asyncio.Task] = []

        def _on_line(line: bytes) -> None:
            event = parse_ndjson_line(line)
            if event is None:
                return
            self._events.append(event)
            dispatch_tasks.append(asyncio.create_task(self._dispatch(event)))

        start_time = time.monotonic()
        exit_code: int | None = None
        try:
            try:
                exit_code = await run_streaming_subprocess(
                    cmd=cmd,
                    env=env,
                    cwd=cwd,
                    on_line=_on_line,
                    cancel_event=self._cancel_event,
                )
            except Exception as e:
                logger.exception("CodexCLI subprocess failed")
                return AgentOutput(
                    result=_classify_error_result(str(e)),
                    summary=str(e),
                    error_message=str(e),
                )
        finally:
            # Drain in-flight dispatch coroutines so any pending on_message
            # callbacks land before we return, even when run_streaming_subprocess raised.
            if dispatch_tasks:
                await asyncio.gather(*dispatch_tasks, return_exceptions=True)

        if self._cancel_event.is_set():
            return AgentOutput(
                result=AgentResult.FAILED,
                summary="Cancelled",
                error_message="Agent was stopped",
            )

        terminal = self._final_terminal_event()
        if terminal is None:
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=f"CodexCLI exited with code {exit_code} before emitting a terminal event",
            )

        if terminal.get("type") == "turn.completed":
            summary = self._compose_summary_from_events()
            usage = terminal.get("usage") or {}
            tokens = (
                int(usage.get("input_tokens", 0))
                + int(usage.get("output_tokens", 0))
                + int(usage.get("reasoning_output_tokens", 0))
            )
            output = AgentOutput(
                result=AgentResult.COMPLETED,
                summary=summary,
                tokens_used=tokens,
            )
        else:  # turn.failed
            err = terminal.get("error") or {}
            text = err.get("message") or terminal.get("message") or ""
            output = AgentOutput(
                result=_classify_error_result(text),
                summary=text,
                error_message=text,
            )

        logger.info(
            "CodexCLI finished task %s in %.1fs (exit=%s, result=%s)",
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

    def _build_command(self, prompt: str) -> list[str]:
        cli = shutil.which("codex")
        if cli is None:
            raise RuntimeError("`codex` CLI not found in PATH")
        # --skip-git-repo-check: codex refuses to run outside a trusted git repo
        # by default; agent-queue worktrees aren't always one.
        # --sandbox workspace-write: sane default for autonomous agents on a
        # checked-out workspace (operators override via profile in phase 2).
        cmd = [
            cli,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
        ]
        if self._profile is not None:
            model = getattr(self._profile, "model", "") or ""
            if model:
                cmd += ["--model", model]
        cmd += [prompt]
        return cmd

    def _build_prompt(self) -> str:
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
        return "\n\n".join(p for p in parts if p)

    async def _dispatch(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "thread.started":
            self._thread_id = event.get("thread_id")
            return
        if etype == "error":
            logger.warning("CodexCLI inline error: %s", event.get("message", ""))
            return
        if self._on_message is None:
            return
        if etype in ("item.started", "item.completed"):
            await self._dispatch_item(event)

    async def _dispatch_item(self, event: dict) -> None:
        """Dispatch an item.started / item.completed event to the callback.

        Item types:
            agent_message       — text response (only on item.completed)
            file_change         — file edits (started + completed both relevant)
            command_execution   — shell commands (started + completed both relevant)

        Add branches as new item types are observed.
        """
        assert self._on_message is not None
        item = event.get("item") or {}
        item_type = item.get("type")
        is_started = event.get("type") == "item.started"

        if item_type == "agent_message":
            if not is_started:
                text = item.get("text", "")
                if text:
                    await self._on_message(text)
        elif item_type == "file_change":
            verb = "starting" if is_started else "completed"
            paths = ", ".join(c.get("path", "?") for c in item.get("changes", []))
            await self._on_message(f"-# file_change {verb}: {paths}")
        elif item_type == "command_execution":
            cmd = item.get("command", "?")
            verb = "starting" if is_started else "completed"
            await self._on_message(f"-# command_execution {verb}: {cmd}")
        else:
            logger.info(
                "CodexCLI unknown item type=%s (capture for schema): %s", item_type, item
            )

    def _final_terminal_event(self) -> dict | None:
        for event in reversed(self._events):
            if event.get("type") in _TERMINAL_EVENT_TYPES:
                return event
        return None

    def _compose_summary_from_events(self) -> str:
        """Build the AgentOutput summary by concatenating agent_message item texts.

        Codex's turn.completed event carries no summary field — the assistant's
        user-facing response lives in item.completed events with item.type=
        "agent_message". This helper joins them in arrival order.
        """
        parts: list[str] = []
        for event in self._events:
            if event.get("type") != "item.completed":
                continue
            item = event.get("item") or {}
            if item.get("type") == "agent_message":
                text = item.get("text", "")
                if text:
                    parts.append(text)
        return "\n\n".join(parts) or "Completed"
