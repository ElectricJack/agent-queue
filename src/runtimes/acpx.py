"""ACPX runtime — fans out to any ACP-compatible coding agent via openclaw/acpx.

The ACPXRuntime spawns ``acpx exec`` to run a single task against an ACP
agent (Claude / Codex / Gemini / OpenCode / Cursor / GitHub Copilot /
Factory Droid / iFlow / Kilocode / Kimi / Kiro / Qoder / Qwen / Trae —
the agent is selected per-profile via ``AgentProfile.agent_name``).

Why one runtime for many agents:
- ACP normalises the wire shape across agents (JSON-RPC 2.0 over
  NDJSON), so the runtime side stays one event-dispatch loop instead of
  one-per-agent.  Adding a new ACP agent later = registry config, not
  new code.

Subprocess invocation:
- ``acpx --format json --approve-all <agent_name> exec <prompt>``
- ``--cd <workspace>`` set when present so the agent operates in the
  task's checkout.

Cancellation:
- Cooperative ``session/cancel`` via SIGTERM through the existing
  ``_subprocess.py`` plumbing; SIGKILL after ``sigterm_grace_seconds``.

Capabilities:
- ACPX exposes the underlying agent's surface (Read / Write / Bash /
  MCP, etc.) — capability filtering happens via the agent's own flags
  or ACP capability ``set_config``.  In v1 we declare the full set;
  per-agent tightening is a follow-up.

Verified against ACPX schema documented at
https://github.com/openclaw/acpx and the corresponding ACP spec at
https://agentclientprotocol.com/protocol/overview.
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


def _classify_acp_error(error_msg: str, stop_reason: str | None = None) -> AgentResult:
    """Classify an ACP error into the orchestrator's :class:`AgentResult`.

    ACP normalises stop reasons across agents (``completed`` /
    ``cancelled`` / ``failed``) but the actual error text comes from the
    underlying agent and varies — we re-use the same heuristics the
    Claude runtimes apply since the substrings are mostly model-agnostic
    (rate-limit, quota, overloaded keywords).
    """
    if stop_reason == "cancelled":
        return AgentResult.FAILED
    lower = (error_msg or "").lower()
    if "hit your limit" in lower or "resets " in lower:
        return AgentResult.PAUSED_RATE_LIMIT
    if "rate_limit" in lower or "rate limit" in lower or "429" in lower:
        return AgentResult.PAUSED_RATE_LIMIT
    if "overloaded" in lower or "503" in lower or "capacity" in lower:
        return AgentResult.PAUSED_RATE_LIMIT
    if "token" in lower or "quota" in lower:
        return AgentResult.PAUSED_TOKENS
    return AgentResult.FAILED


class ACPXRuntime(Runtime):
    """Runtime that wraps ``acpx exec`` for any ACP-compatible agent.

    ``profile.agent_name`` selects the underlying agent (``"claude"`` /
    ``"codex"`` / ``"gemini"`` / ...).  Required when constructing this
    runtime: an empty ``agent_name`` is rejected at sync-time by
    :func:`src.profiles.parser._validate_config` (Phase 1.6 spec) so we
    don't have to fail at runtime.
    """

    name: ClassVar[str] = "acpx"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(Capability)
    requires_workspace: ClassVar[bool] = True

    def __init__(self, profile=None, llm_logger=None):
        self._profile = profile
        self._llm_logger = llm_logger
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._session_id: str | None = None
        # Accumulated NDJSON events — used to build summary + token count
        # at the end and to surface the final stopReason / error.
        self._events: list[dict] = []
        self._on_message: MessageCallback | None = None

    async def start(self, task: TaskContext) -> None:
        self._task = task
        self._cancel_event.clear()
        self._events = []
        self._session_id = None
        ctx = get_correlation_context()
        # Read agent name softly — missing value is a configuration bug
        # caught later in ``_build_command()``; don't crash the start
        # logging path over it.
        agent_name_for_log = (
            getattr(self._profile, "agent_name", "") if self._profile else ""
        ) or "(none)"
        logger.info(
            "ACPX runtime starting for task %s (agent=%s)",
            ctx.get("task_id", task.task_id if hasattr(task, "task_id") else "unknown"),
            agent_name_for_log,
        )

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        assert self._task is not None, "wait() called before start()"
        self._on_message = on_message

        prompt = self._build_prompt()
        cmd = self._build_command()
        env = isolated_env()
        cwd = self._task.checkout_path or "."

        # Dispatch tasks created by _on_line so we can await them after
        # the subprocess exits — guarantees on_message callbacks complete
        # before we return AgentOutput.
        _dispatch_tasks: list[asyncio.Task] = []

        def _on_line(line: bytes) -> None:
            event = parse_ndjson_line(line)
            if event is None:
                return
            self._events.append(event)
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
                logger.exception("ACPX subprocess failed")
                return self._build_failure_output(str(e))
        finally:
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
                error_message=(
                    f"ACPX exited with code {exit_code} before emitting a "
                    "stopReason event"
                ),
            )

        stop_reason = result_event.get("stopReason") or result_event.get("stop_reason")
        result_text = (
            result_event.get("result")
            or result_event.get("text")
            or ""
        )
        usage = result_event.get("usage") or {}
        # ACP usage shape varies slightly per agent; sum the common keys.
        tokens = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("output_tokens", 0) or 0)
            + int(usage.get("prompt_tokens", 0) or 0)
            + int(usage.get("completion_tokens", 0) or 0)
        )

        if stop_reason == "completed":
            output = AgentOutput(
                result=AgentResult.COMPLETED,
                summary=result_text,
                tokens_used=tokens,
            )
        else:
            output = AgentOutput(
                result=_classify_acp_error(result_text, stop_reason),
                summary=result_text,
                tokens_used=tokens,
                error_message=result_text or f"stopReason={stop_reason}",
            )

        logger.info(
            "ACPX finished task %s in %.1fs (exit=%s, stop=%s, result=%s)",
            self._task.task_id,
            time.monotonic() - start_time,
            exit_code,
            stop_reason,
            output.result,
        )
        return output

    async def stop(self) -> None:
        self._cancel_event.set()

    async def is_alive(self) -> bool:
        return self._task is not None and not self._cancel_event.is_set()

    # ---------------- helpers ----------------

    def _agent_name(self) -> str:
        """Return the ACP agent identifier from the profile.

        The profile is required: ACPXRuntime can only run when an agent
        is selected.  ``parser._validate_config`` rejects ``runtime: acpx``
        with empty ``agent_name`` at sync-time so we should always have
        a value here in production; we still defend at runtime.
        """
        agent = ""
        if self._profile is not None:
            agent = getattr(self._profile, "agent_name", "") or ""
        if not agent:
            raise RuntimeError(
                "ACPXRuntime requires profile.agent_name (e.g. 'claude', "
                "'codex', 'gemini'); got empty value. Check the profile's "
                "## Config block."
            )
        return agent

    def _build_command(self) -> list[str]:
        """Assemble the ``acpx`` invocation (without the trailing prompt).

        Validates the profile (``agent_name``) before checking the
        environment (PATH) so that a missing ``agent_name`` raises a
        config-pointing error even on machines that happen not to have
        ``acpx`` installed yet.
        """
        agent = self._agent_name()
        cli = shutil.which("acpx")
        if cli is None:
            raise RuntimeError("`acpx` CLI not found in PATH")
        cmd = [cli, "--format", "json", "--approve-all", agent, "exec"]
        # NOTE: acpx 0.1.x's `<agent> exec` subcommand does not accept
        # `--model` — passing it makes acpx print help and exit 0,
        # producing zero JSON-RPC output and the dispatcher fails with
        # "ACPX exited before emitting a stopReason event".
        # Model selection happens via the underlying agent's own config
        # (e.g. Claude's settings.json `model` key, or `acpx <agent> set
        # model <id>` against a *named* session — incompatible with the
        # one-shot `exec` we use here).  Profiles that need a specific
        # model should use `runtime: claude_sdk` for now.
        return cmd

    def _build_prompt(self) -> str:
        """Assemble the agent prompt from TaskContext.

        Mirrors the structure used by :class:`ClaudeSDKRuntime` so
        behaviour is consistent across runtimes — same L0/L1 tiers,
        description, acceptance criteria, test commands, attached
        context.
        """
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
        """Forward an ACP event to the ``on_message`` callback as readable text.

        ACP event types (from the JSON-RPC envelope's ``method`` /
        ``params`` shape):

        * ``session/update`` — incremental output (agent text, thinking,
          plan updates).  Emit text chunks; suppress thinking unless
          large.
        * ``tool_call`` — agent invoked a tool.  Emit ``-# {tool_name}``
          to mirror the existing live-stream UX.
        * ``tool_result`` — tool finished.  Logged only.
        * Final response / ``stopReason`` event — Discord stream stays
          quiet here; the orchestrator posts its own completion summary
          after the task finishes.
        """
        method = event.get("method") or event.get("type")
        params = event.get("params") or {}

        # Capture session ID from initialise / session/new responses.
        if method in ("initialize", "session/new"):
            sid = params.get("sessionId") or event.get("sessionId")
            if sid:
                self._session_id = sid
            return

        if self._on_message is None:
            return

        if method == "session/update":
            update = params.get("update") or {}
            kind = update.get("sessionUpdate") or update.get("kind")
            if kind == "agent_message_chunk":
                content = update.get("content") or {}
                text = content.get("text") if isinstance(content, dict) else ""
                if text:
                    await self._on_message(text)
            elif kind == "agent_thought_chunk":
                # Suppress thinking from the live stream; persisted via
                # event log only.
                return
            elif kind == "tool_call":
                tool = update.get("toolCall") or {}
                tool_name = tool.get("title") or tool.get("name") or "?"
                await self._on_message(f"-# {tool_name}")
            return

        if method == "tool_call":
            # Some ACP implementations emit tool_call as a top-level event.
            tool_name = params.get("name") or params.get("title") or "?"
            await self._on_message(f"-# {tool_name}")
            return

        # tool_result, plan_update, etc. — log only.

    def _final_result_event(self) -> dict | None:
        """Return the last ACP event carrying a ``stopReason`` (final response).

        Different ACP servers place ``stopReason`` either at the top
        level or inside ``params.result`` — we accept both.
        """
        for event in reversed(self._events):
            if event.get("stopReason") or event.get("stop_reason"):
                return event
            params = event.get("params") or {}
            result = params.get("result") if isinstance(params, dict) else None
            if isinstance(result, dict) and (
                result.get("stopReason") or result.get("stop_reason")
            ):
                return result
        return None

    def _build_failure_output(self, error: str) -> AgentOutput:
        return AgentOutput(
            result=_classify_acp_error(error),
            summary=error,
            error_message=error,
        )
