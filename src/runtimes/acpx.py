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


def _meta_tool_name(d: dict) -> str | None:
    """Extract the underlying tool name from an ACP event's `_meta` block.

    The Zed Industries claude-agent-acp ACP server stores the real tool
    name under ``_meta.claudeCode.toolName`` (e.g. ``"Bash"``, ``"Glob"``)
    while the top-level ``title``/``name`` fields can be the agent's
    user-facing label (e.g. ``"Find"`` for a Glob call).  We prefer the
    top-level fields when present and fall back here.
    """
    meta = d.get("_meta") if isinstance(d, dict) else None
    if not isinstance(meta, dict):
        return None
    cc = meta.get("claudeCode")
    if not isinstance(cc, dict):
        return None
    name = cc.get("toolName")
    return name if isinstance(name, str) and name else None


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
        # ACP streams agent_message_chunk events as 1-3 token fragments.
        # We accumulate them as cumulative turn text and emit edits to a
        # single tracked Discord message via the stream_id protocol —
        # see TaskMessageEvent.stream_id and the notification handler's
        # _handle_streamed_task_message.  Time-debounced so we don't
        # spam ~30 edit/sec; flushed immediately on tool_call (paragraph
        # boundary) and on subprocess exit (with stream_done=True).
        self._stream_text: list[str] = []  # cumulative chunks within the current turn
        self._stream_id: str | None = None  # uuid4 hex per turn; set lazily
        self._last_emit: float = 0.0  # monotonic of last on_message call
        self._stream_min_interval: float = 0.5  # seconds between edits

    async def start(self, task: TaskContext) -> None:
        self._task = task
        self._cancel_event.clear()
        self._events = []
        self._session_id = None
        self._stream_text = []
        self._stream_id = None
        self._last_emit = 0.0
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
            # Final flush of any in-flight stream — closes the tracked
            # Discord message so the next task (or the orchestrator's
            # completion summary) starts fresh.  Done after dispatch
            # tasks so the cumulative buffer is fully populated.
            await self._close_stream()

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
        # If `result` was nested (a dict), don't use the dict itself as
        # text — the conversational text already streamed via on_message.
        if not isinstance(result_text, str):
            result_text = ""
        usage = result_event.get("usage") or {}
        # ACP usage shape varies slightly per agent.  claude-agent-acp uses
        # camelCase (inputTokens, outputTokens, totalTokens); other agents
        # may use snake_case.  Prefer totalTokens when present (it includes
        # cache reads/writes); otherwise sum the input+output buckets.
        def _u(k: str) -> int:
            v = usage.get(k)
            try:
                return int(v) if v is not None else 0
            except (TypeError, ValueError):
                return 0
        tokens = _u("totalTokens") or _u("total_tokens") or (
            _u("inputTokens") + _u("input_tokens")
            + _u("outputTokens") + _u("output_tokens")
            + _u("promptTokens") + _u("prompt_tokens")
            + _u("completionTokens") + _u("completion_tokens")
        )

        # Map stop reasons to AgentResult.  ACP uses "end_turn" (agent
        # spoke and yielded), "completed" (some servers' alias), "tool_use"
        # (transient mid-tool-call, shouldn't appear here), "max_tokens",
        # "cancelled", "refusal".  Anything we don't recognise is treated
        # as failure so blocked-state monitoring catches it.
        SUCCESS_REASONS = {"end_turn", "completed", "stop_sequence"}
        if stop_reason in SUCCESS_REASONS:
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

        Streaming model: agent text chunks accumulate into ``_stream_text``
        (cumulative for the current turn) and we emit ``on_message`` calls
        carrying ``stream_id`` so the Discord receiver edits a single
        message in place.  Emits are time-debounced (``_stream_min_interval``)
        to avoid Discord rate limits, and flushed immediately at boundaries:

        * ``tool_call`` — closes the current stream (``stream_done=True``),
          then posts the tool banner as a separate (non-streamed) message,
          then a fresh stream starts when the next chunk arrives.
        * Subprocess exit — final flush in :meth:`wait`.

        ACP event types:

        * ``session/update.agent_message_chunk`` — token fragment;
          appended + debounced emit.
        * ``session/update.agent_thought_chunk`` — extended thinking;
          suppressed from the live stream (event log only).
        * ``session/update.tool_call`` — agent invoked a tool.
        * ``session/update.tool_call_update``, ``plan``, ``available_commands_update``
          — silent (status / metadata only, would spam the stream).
        * Top-level result with ``stopReason`` — Discord stream stays
          quiet; the orchestrator posts its own completion summary.
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
                    self._stream_text.append(text)
                    await self._maybe_emit_stream(force=False)
            elif kind == "agent_thought_chunk":
                return  # suppress thinking from live stream
            elif kind == "tool_call":
                # Close the current stream message before announcing the
                # tool — so the user sees the agent's paragraph as a
                # finalised edit, then the tool banner, then (when the
                # next chunk arrives) a NEW stream message for the next
                # paragraph.  Tool fields are on the update dict directly,
                # not nested under "toolCall" — that was the source of
                # the "?" markers in v1.
                await self._close_stream()
                tool_title = (
                    update.get("title")
                    or update.get("name")
                    or _meta_tool_name(update)
                    or ""
                )
                if tool_title:
                    # Tool banner is a one-shot message, not part of the
                    # stream — no stream_id, posts as a fresh message.
                    await self._on_message(f"-# {tool_title}")
            # tool_call_update, plan, available_commands_update — silent.
            return

        if method == "tool_call":
            # Some ACP implementations emit tool_call as a top-level event.
            await self._close_stream()
            tool_title = (
                params.get("name")
                or params.get("title")
                or _meta_tool_name(params)
                or ""
            )
            if tool_title:
                await self._on_message(f"-# {tool_title}")
            return

        # tool_result, plan_update, etc. — log only.

    async def _maybe_emit_stream(self, *, force: bool) -> None:
        """Emit the cumulative stream text via the on_message callback.

        Debounced: skips when fewer than ``_stream_min_interval`` seconds
        have passed since the last emit, unless ``force=True`` (used by
        :meth:`_close_stream` and the final flush in :meth:`wait`).
        Lazily allocates a ``stream_id`` on the first emit of a turn so
        the Discord receiver knows to start tracking a stream message.
        """
        if not self._stream_text or self._on_message is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_emit) < self._stream_min_interval:
            return
        if self._stream_id is None:
            import uuid
            self._stream_id = uuid.uuid4().hex
        text = "".join(self._stream_text)
        await self._on_message(text, stream_id=self._stream_id, stream_done=False)
        self._last_emit = now

    async def _close_stream(self) -> None:
        """Finalise the current turn's stream message, if any.

        Forces a final emit with ``stream_done=True`` so the receiver
        releases its tracked Discord message and a subsequent emit (e.g.
        the next paragraph after a tool call) starts a fresh stream.
        """
        if not self._stream_text or self._on_message is None:
            self._stream_text = []
            self._stream_id = None
            return
        if self._stream_id is None:
            import uuid
            self._stream_id = uuid.uuid4().hex
        text = "".join(self._stream_text)
        await self._on_message(text, stream_id=self._stream_id, stream_done=True)
        self._stream_text = []
        self._stream_id = None
        self._last_emit = 0.0

    def _final_result_event(self) -> dict | None:
        """Return the last ACP event carrying a ``stopReason`` (final response).

        Three shapes seen in the wild:

        1. JSON-RPC response (the canonical claude-agent-acp shape): the
           ``result`` field is at the *top level* of the envelope, e.g.
           ``{"jsonrpc": "2.0", "id": 2, "result": {"stopReason": ...}}``.
        2. Notification with nested result: ``{"method": "...",
           "params": {"result": {"stopReason": ...}}}``.
        3. Top-level ``stopReason`` directly on the envelope.

        Earlier this method only handled (2) and (3) — (1) is what
        claude-agent-acp actually emits, so the dispatcher saw "exit 0
        before stopReason" and marked the task BLOCKED even though the
        agent finished successfully.
        """
        for event in reversed(self._events):
            # (3) Top-level stopReason.
            if event.get("stopReason") or event.get("stop_reason"):
                return event

            # (1) JSON-RPC response: result at top level.
            top_result = event.get("result")
            if isinstance(top_result, dict) and (
                top_result.get("stopReason") or top_result.get("stop_reason")
            ):
                return top_result

            # (2) Notification with nested params.result.
            params = event.get("params") or {}
            nested_result = params.get("result") if isinstance(params, dict) else None
            if isinstance(nested_result, dict) and (
                nested_result.get("stopReason") or nested_result.get("stop_reason")
            ):
                return nested_result
        return None

    def _build_failure_output(self, error: str) -> AgentOutput:
        return AgentOutput(
            result=_classify_acp_error(error),
            summary=error,
            error_message=error,
        )
