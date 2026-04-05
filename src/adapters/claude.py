"""Claude Code adapter -- runs AI agent tasks via the Claude Agent SDK.

This implements AgentAdapter by wrapping the Claude Code CLI as a subprocess,
communicating through the SDK's streaming protocol.  The orchestrator sees
only the AgentAdapter interface; all Claude-specific concerns live here.

Key design decisions:

- **System CLI over bundled binary:** We use the user's installed ``claude``
  CLI (found via ``shutil.which``) rather than the SDK's bundled binary.
  The system CLI carries the user's login credentials (from ``claude login``
  or ANTHROPIC_API_KEY); the bundled one does not.

- **Environment scrubbing:** CLAUDECODE env vars are stripped before launching
  to prevent the SDK from detecting it's inside an existing Claude session,
  which would block nested agent invocations.

- **Resilient query (_resilient_query):** The SDK's message parser crashes on
  unknown message types (e.g. ``rate_limit_event``) because its async
  generator dies on the first MessageParseError.  Our wrapper accesses SDK
  internals to iterate raw JSON messages and parse them ourselves, silently
  skipping unrecognised types instead of aborting the entire session.

- **Message extraction (_extract_message_text):** Translates the SDK's typed
  message objects (AssistantMessage, ResultMessage, etc.) into human-readable
  Discord-friendly text with markdown formatting.

See specs/adapters/claude.md for the full behavioral specification.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from src.adapters.base import AgentAdapter, MessageCallback
from src.logging_config import get_correlation_context
from src.models import AgentOutput, AgentResult, TaskContext

logger = logging.getLogger(__name__)

# Import SDK types for isinstance checks (lazy, set in wait())
_sdk_types_loaded = False
_AssistantMessage = None
_ResultMessage = None
_UserMessage = None
_TextBlock = None
_ThinkingBlock = None
_ToolUseBlock = None
_ToolResultBlock = None


async def _resilient_query(prompt, options, adapter=None):
    """Wrap claude_agent_sdk.query() to survive MessageParseError.

    Why this exists: The SDK's ``query()`` function yields parsed messages from
    an async generator.  Internally it calls ``parse_message()`` on each raw
    JSON dict from the CLI subprocess.  When the CLI emits an unknown message
    type (like ``rate_limit_event``), ``parse_message`` raises
    MessageParseError -- and because Python async generators can't recover
    from mid-iteration exceptions, the entire stream dies.

    This wrapper reproduces the SDK's query setup but iterates the raw JSON
    dicts ourselves, calling ``parse_message`` in a try/except so we can skip
    unrecognised types and keep the session alive.  It's fragile (depends on
    SDK internals) but necessary until the SDK handles unknown types gracefully.

    If *adapter* is provided, the transport and query objects are stored on it
    so that ``adapter.stop()`` can force-close the subprocess.
    """
    from claude_agent_sdk._internal.client import InternalClient
    from claude_agent_sdk._internal.message_parser import parse_message
    from claude_agent_sdk._errors import MessageParseError as _MPE
    import os
    import json
    from collections.abc import AsyncIterable

    os.environ["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"

    client = InternalClient()

    configured_options = options
    if options.can_use_tool:
        if isinstance(prompt, str):
            raise ValueError("can_use_tool requires streaming mode")
        if options.permission_prompt_tool_name:
            raise ValueError("can_use_tool and permission_prompt_tool_name are mutually exclusive")
        from dataclasses import replace

        configured_options = replace(options, permission_prompt_tool_name="stdio")

    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    transport = SubprocessCLITransport(prompt=prompt, options=configured_options)
    await transport.connect()

    sdk_mcp_servers = {}
    if configured_options.mcp_servers and isinstance(configured_options.mcp_servers, dict):
        for name, config in configured_options.mcp_servers.items():
            if isinstance(config, dict) and config.get("type") == "sdk":
                sdk_mcp_servers[name] = config["instance"]

    from dataclasses import asdict

    agents_dict = None
    if configured_options.agents:
        agents_dict = {
            name: {k: v for k, v in asdict(agent_def).items() if v is not None}
            for name, agent_def in configured_options.agents.items()
        }

    hooks = (
        client._convert_hooks_to_internal_format(configured_options.hooks)
        if configured_options.hooks
        else None
    )

    from claude_agent_sdk._internal.query import Query

    query_obj = Query(
        transport=transport,
        is_streaming_mode=True,
        can_use_tool=configured_options.can_use_tool,
        hooks=hooks,
        sdk_mcp_servers=sdk_mcp_servers,
        agents=agents_dict,
    )

    # Store references on the adapter so stop() can force-close the subprocess.
    if adapter is not None:
        adapter._active_transport = transport
        adapter._active_query = query_obj

    try:
        await query_obj.start()
        await query_obj.initialize()

        if isinstance(prompt, str):
            user_message = {
                "type": "user",
                "session_id": "",
                "message": {"role": "user", "content": prompt},
                "parent_tool_use_id": None,
            }
            await transport.write(json.dumps(user_message) + "\n")
            await transport.end_input()
        elif isinstance(prompt, AsyncIterable) and query_obj._tg:
            query_obj._tg.start_soon(query_obj.stream_input, prompt)

        # Iterate raw dicts, parse ourselves, skip unknown message types
        async for data in query_obj.receive_messages():
            try:
                yield parse_message(data)
            except _MPE as e:
                msg_type = data.get("type", "unknown") if isinstance(data, dict) else "unknown"
                print(f"Claude adapter: skipping unrecognised message type '{msg_type}': {e}")
                continue
    finally:
        if adapter is not None:
            adapter._active_transport = None
            adapter._active_query = None
        await query_obj.close()


@dataclass
class ClaudeAdapterConfig:
    """Configuration for the Claude Code agent adapter.

    Attributes:
        model: Model ID to pass to Claude Code.  Empty string means let the
            CLI pick its default (usually the latest Sonnet).
        permission_mode: Controls which tool calls require human approval.
            "acceptEdits" auto-approves file edits but prompts for shell
            commands; other modes are more or less permissive.
        allowed_tools: Whitelist of tool names the agent may use.  Defaults
            to the safe set of file and search tools.  The orchestrator may
            extend this per-task (e.g. adding "WebSearch").
    """

    model: str = ""  # Empty = let Claude Code pick the default model
    permission_mode: str = "acceptEdits"
    allowed_tools: list[str] = field(
        default_factory=lambda: [
            "Read",
            "Write",
            "Edit",
            "Bash",
            "Glob",
            "Grep",
        ]
    )
    max_turns: int = 20000  # Allow long-running multi-step tasks (100x default)


class ClaudeAdapter(AgentAdapter):
    """AgentAdapter implementation that runs tasks via the Claude Code CLI.

    Each task gets a fresh SDK query (subprocess).  The adapter streams
    messages back through the on_message callback and collects the final
    result/token counts from the ResultMessage.
    """

    def __init__(self, config: ClaudeAdapterConfig | None = None, llm_logger=None):
        self._config = config or ClaudeAdapterConfig()
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._session_id: str | None = None
        self._llm_logger = llm_logger
        # References to the active SDK transport/query so stop() can force-kill
        # the subprocess instead of just setting a flag.
        self._active_transport = None
        self._active_query = None

    async def start(self, task: TaskContext) -> None:
        self._task = task
        self._cancel_event.clear()
        ctx = get_correlation_context()
        logger.info(
            "Claude adapter starting for task %s",
            ctx.get("task_id", task.task_id if hasattr(task, "task_id") else "unknown"),
        )

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        import time as _time

        _wait_start = _time.monotonic()
        try:
            # Strip Claude Code session markers to allow launching agent sessions
            import os

            for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
                os.environ.pop(var, None)

            import shutil
            from claude_agent_sdk import ClaudeAgentOptions
            from claude_agent_sdk.types import (
                AssistantMessage,
                ResultMessage,
                UserMessage,
                TextBlock,
                ThinkingBlock,
                ToolUseBlock,
                ToolResultBlock,
            )

            global _sdk_types_loaded, _AssistantMessage, _ResultMessage, _UserMessage
            global _TextBlock, _ThinkingBlock, _ToolUseBlock, _ToolResultBlock
            _AssistantMessage = AssistantMessage
            _ResultMessage = ResultMessage
            _UserMessage = UserMessage
            _TextBlock = TextBlock
            _ThinkingBlock = ThinkingBlock
            _ToolUseBlock = ToolUseBlock
            _ToolResultBlock = ToolResultBlock
            _sdk_types_loaded = True

            # Prefer the system claude CLI over the SDK's bundled binary.
            # The bundled binary has no user credentials; the system one does
            # (either via `claude login` or ANTHROPIC_API_KEY).
            system_claude = shutil.which("claude")

            # Build allowed_tools: start with the configured set, then
            # auto-approve MCP tools from any configured MCP servers.
            # Without this, MCP tools would need interactive permission
            # approval — impossible in headless SDK mode — so the agent
            # can't discover or use them even though the server is connected.
            allowed = list(self._config.allowed_tools)
            if self._task.mcp_servers:
                for server_name in self._task.mcp_servers:
                    pattern = f"mcp__{server_name}__*"
                    if pattern not in allowed:
                        allowed.append(pattern)

            options = ClaudeAgentOptions(
                allowed_tools=allowed,
                permission_mode=self._config.permission_mode,
                max_turns=self._config.max_turns,
                cwd=self._task.checkout_path or None,
                cli_path=system_claude,  # None → falls back to bundled binary
            )
            if self._config.model:
                options.model = self._config.model
            if self._task.mcp_servers:
                options.mcp_servers = self._task.mcp_servers
            if self._task.resume_session_id:
                try:
                    from claude_agent_sdk import get_session_messages

                    msgs = get_session_messages(self._task.resume_session_id, limit=1)
                    if msgs:
                        options.resume = self._task.resume_session_id
                        options.fork_session = True
                        print(f"Claude adapter: forking session {self._task.resume_session_id}")
                    else:
                        print(
                            f"Claude adapter: session "
                            f"{self._task.resume_session_id} not found, "
                            f"starting fresh"
                        )
                except Exception as e:
                    print(f"Claude adapter: session fork check failed ({e}), starting fresh")

            summary_parts = []
            tokens_used = 0
            current_prompt = self._build_prompt()

            mcp_names = list(self._task.mcp_servers.keys()) if self._task.mcp_servers else []
            print(
                f"Claude adapter: starting query (session={self._session_id or 'new'}, "
                f"prompt={len(current_prompt)} chars, "
                f"allowed_tools={allowed}, mcp_servers={mcp_names})"
            )
            cli_error: str | None = None
            try:
                async for message in _resilient_query(
                    prompt=current_prompt, options=options, adapter=self
                ):
                    # Log only messages with meaningful subtypes to reduce noise
                    msg_subtype = getattr(message, "subtype", "")
                    if msg_subtype and msg_subtype not in ("", None):
                        msg_type = getattr(message, "type", "unknown")
                        print(f"Claude adapter message: type={msg_type} subtype={msg_subtype}")

                    if self._cancel_event.is_set():
                        output = AgentOutput(
                            result=AgentResult.FAILED,
                            summary="Cancelled",
                            error_message="Agent was stopped",
                        )
                        self._log_session(current_prompt, output, _wait_start, _time)
                        return output

                    # Capture session ID from init message.
                    # SystemMessage only has .subtype and .data (a raw dict);
                    # the session_id lives in .data, not as a top-level attribute.
                    if hasattr(message, "subtype") and message.subtype == "init":
                        data = getattr(message, "data", {})
                        self._session_id = (
                            data.get("session_id")
                            if isinstance(data, dict)
                            else getattr(message, "session_id", None)
                        )
                        print(f"Claude adapter: session started ({self._session_id})")

                    # Forward interesting messages to the callback
                    if on_message:
                        text = self._extract_message_text(message)
                        if text:
                            await on_message(text)

                    # Capture result and token usage from ResultMessage
                    if isinstance(message, ResultMessage):
                        # Check for error result BEFORE treating as success
                        if getattr(message, "is_error", False):
                            err_subtype = getattr(message, "subtype", "") or "unknown"
                            err_result = str(getattr(message, "result", "") or "")
                            cli_error = f"{err_subtype}: {err_result}".strip(": ") or err_subtype
                            print(f"Claude adapter: CLI returned error result: {cli_error}")
                        else:
                            if message.result:
                                summary_parts.append(str(message.result))
                        usage = getattr(message, "usage", None)
                        if usage and isinstance(usage, dict):
                            tokens_used += usage.get("input_tokens", 0) + usage.get(
                                "output_tokens", 0
                            )
                    elif hasattr(message, "result") and message.result:
                        summary_parts.append(str(message.result))

            except Exception as e:
                import traceback

                error_msg = str(e)
                full_traceback = traceback.format_exc()
                print(f"Claude adapter error: {error_msg}")
                print(full_traceback)
                if "token" in error_msg.lower() or "quota" in error_msg.lower():
                    output = AgentOutput(
                        result=AgentResult.PAUSED_TOKENS,
                        error_message=error_msg,
                    )
                    self._log_session(current_prompt, output, _wait_start, _time)
                    return output
                output = AgentOutput(
                    result=AgentResult.FAILED,
                    error_message=f"{error_msg}\n{full_traceback}",
                )
                self._log_session(current_prompt, output, _wait_start, _time)
                return output

            # If the CLI reported an error result, propagate it as FAILED
            if cli_error:
                print(f"Claude adapter: query failed with CLI error: {cli_error}")
                output = AgentOutput(
                    result=AgentResult.FAILED,
                    error_message=cli_error,
                    tokens_used=tokens_used,
                )
                self._log_session(current_prompt, output, _wait_start, _time)
                return output

            print(
                f"Claude adapter: query completed, {len(summary_parts)} result parts, "
                f"{tokens_used} tokens"
            )

            # If the agent used 0 tokens and produced no meaningful output,
            # something went wrong (e.g. auth failure, rate limit, CLI crash).
            if tokens_used == 0 and not summary_parts:
                print("Claude adapter: 0 tokens and no output — treating as failure")
                output = AgentOutput(
                    result=AgentResult.FAILED,
                    error_message=(
                        "Agent session ended with 0 tokens and no output. "
                        "Possible causes: rate limit on subscription, "
                        "authentication failure, or Claude CLI crash. "
                        "Check `claude login` status and subscription limits."
                    ),
                    tokens_used=0,
                )
                self._log_session(current_prompt, output, _wait_start, _time)
                return output

            output = AgentOutput(
                result=AgentResult.COMPLETED,
                summary="\n".join(summary_parts) or "Completed",
                tokens_used=tokens_used,
            )
            self._log_session(current_prompt, output, _wait_start, _time)
            return output
        except ImportError as e:
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=f"Claude Agent SDK not available: {e}",
            )

    def _log_session(self, prompt: str, output: AgentOutput, start: float, time_mod) -> None:
        """Log agent session to LLMLogger if available."""
        output.session_id = self._session_id
        duration_ms = int((time_mod.monotonic() - start) * 1000)
        task_id = self._task.task_id if self._task else ""
        logger.info(
            "Claude session completed",
            extra={
                "result": output.result.value,
                "tokens": output.tokens_used,
                "duration_ms": duration_ms,
            },
        )
        if not self._llm_logger:
            return
        self._llm_logger.log_agent_session(
            task_id=task_id,
            session_id=self._session_id,
            model=self._config.model or "(default)",
            prompt=prompt,
            config_summary={
                "allowed_tools": self._config.allowed_tools,
                "permission_mode": self._config.permission_mode,
                "cwd": self._task.checkout_path if self._task else "",
            },
            output=output,
            duration_ms=duration_ms,
        )

    async def stop(self) -> None:
        self._cancel_event.set()
        # Force-close the subprocess transport so the CLI actually terminates,
        # rather than just hoping the cancel flag is checked between messages.
        if self._active_query:
            try:
                await self._active_query.close()
            except Exception:
                pass
        if self._active_transport:
            try:
                await self._active_transport.close()
            except Exception:
                pass

    async def is_alive(self) -> bool:
        return self._task is not None and not self._cancel_event.is_set()

    def _shorten_path(self, path: str) -> str:
        """Strip the task's checkout_path prefix to produce a relative path."""
        root = getattr(self._task, "checkout_path", "") if self._task else ""
        if root and path.startswith(root):
            return path[len(root) :].lstrip("/")
        return path

    def _extract_message_text(self, message) -> str | None:
        """Translate SDK message objects into Discord-friendly markdown text.

        The SDK emits typed messages (AssistantMessage with content blocks,
        UserMessage with tool results, ResultMessage with final output).
        This method formats each into concise, readable text suitable for
        streaming into a Discord thread -- truncating long content, rendering
        tool use as bold labels, and summarising costs/tokens.
        """
        if not _sdk_types_loaded:
            return None

        # AssistantMessage — Claude's response with content blocks
        if isinstance(message, _AssistantMessage):
            content = getattr(message, "content", None)
            if not content:
                return None
            parts = []
            for block in content:
                if isinstance(block, _ThinkingBlock):
                    thinking = getattr(block, "thinking", "")
                    if thinking:
                        # Truncate long thinking blocks
                        preview = thinking[:500]
                        if len(thinking) > 500:
                            preview += "..."
                        parts.append(f"-# *thinking: {preview}*")
                elif isinstance(block, _TextBlock):
                    text = getattr(block, "text", "")
                    if text:
                        parts.append(text)
                elif isinstance(block, _ToolUseBlock):
                    name = getattr(block, "name", "unknown")
                    inp = getattr(block, "input", {})
                    detail = ""
                    if name == "Bash" and isinstance(inp, dict):
                        cmd = inp.get("command", "")[:100]
                        cmd = self._shorten_path(cmd)
                        detail = f" · {cmd}" if cmd else ""
                    elif name in ("Read", "Write", "Edit", "Glob", "Grep") and isinstance(
                        inp, dict
                    ):
                        path = inp.get("file_path", inp.get("path", inp.get("pattern", "")))
                        if path:
                            path = self._shorten_path(path)
                        detail = f" · {path}" if path else ""
                    parts.append(f"-# {name}{detail}")
                elif isinstance(block, _ToolResultBlock):
                    content_val = getattr(block, "content", None)
                    is_error = getattr(block, "is_error", False)
                    if content_val and isinstance(content_val, str):
                        prefix = "**Error:**" if is_error else ""
                        preview = content_val[:300]
                        if len(content_val) > 300:
                            preview += "..."
                        parts.append(f"{prefix}```\n{preview}\n```")
            return "\n".join(parts) if parts else None

        # UserMessage — tool results flowing back
        if isinstance(message, _UserMessage):
            result = getattr(message, "tool_use_result", None)
            if result and isinstance(result, dict):
                content_val = result.get("content", "")
                if content_val and isinstance(content_val, str) and len(content_val) < 300:
                    return f"```\n{content_val}\n```"
            return None

        # ResultMessage — final completion.
        # Do NOT stream this to the thread: the orchestrator posts its own
        # completion/failure summary (embed or detailed failure lines) after
        # the task finishes, so streaming the ResultMessage would duplicate
        # the result information in the thread.
        if isinstance(message, _ResultMessage):
            return None

        return None

    def _build_prompt(self) -> str:
        """Build the final prompt from TaskContext using PromptBuilder."""
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()

        # Main description (already assembled by orchestrator)
        if self._task.description:
            builder.add_context("description", self._task.description)

        # Optional sections
        if self._task.acceptance_criteria:
            criteria = "\n".join(f"- {c}" for c in self._task.acceptance_criteria)
            builder.add_context("acceptance_criteria", f"## Acceptance Criteria\n{criteria}")

        if self._task.test_commands:
            cmds = "\n".join(f"- `{c}`" for c in self._task.test_commands)
            builder.add_context("test_commands", f"## Test Commands\n{cmds}")

        if self._task.image_paths:
            paths = "\n".join(f"- `{p}`" for p in self._task.image_paths)
            builder.add_context(
                "attached_images",
                f"## Attached Images\n"
                f"The user attached the following image files to this task. "
                f"Use the Read tool to view each image — Claude Code can read "
                f"image files natively.\n{paths}",
            )

        if self._task.attached_context:
            combined = "\n".join(f"- {ctx}" for ctx in self._task.attached_context)
            builder.add_context("additional_context", f"## Additional Context\n{combined}")

        return builder.build_task_prompt()
