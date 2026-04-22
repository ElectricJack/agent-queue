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


def _classify_error_result(error_msg: str) -> AgentResult:
    """Classify a CLI error message into the appropriate AgentResult.

    Detects rate limits, session limits, and token quota errors and routes
    them to PAUSED results so the orchestrator backs off gracefully instead
    of burning retries.
    """
    lower = error_msg.lower()

    # Session / usage cap: "You've hit your limit · resets 2pm ..."
    if "hit your limit" in lower or "resets " in lower:
        return AgentResult.PAUSED_RATE_LIMIT

    # Rate limit errors (HTTP 429, SDK exceptions)
    if "rate_limit" in lower or "rate limit" in lower or "429" in lower:
        return AgentResult.PAUSED_RATE_LIMIT

    # Overloaded / capacity errors
    if "overloaded" in lower or "503" in lower or "capacity" in lower:
        return AgentResult.PAUSED_RATE_LIMIT

    # Token / quota exhaustion
    if "token" in lower or "quota" in lower:
        return AgentResult.PAUSED_TOKENS

    return AgentResult.FAILED


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

    # Track our own transport/query so cleanup only clears references we own
    _our_transport = None
    _our_query = None

    try:
        await transport.connect()
        _our_transport = transport

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
        _our_query = query_obj

        # Store references on the adapter so stop() can force-close the subprocess.
        if adapter is not None:
            adapter._active_transport = transport
            adapter._active_query = query_obj

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
        # Only clear adapter references if they still point to OUR objects.
        # A retry may have already created a new generator that set its own
        # references — we must not clobber them.
        if adapter is not None:
            if adapter._active_transport is _our_transport:
                adapter._active_transport = None
            if adapter._active_query is _our_query:
                adapter._active_query = None
        # Wrap close() in try/except so cleanup errors never mask the
        # original exception (e.g. ProcessError from a failed resume).
        if _our_query is not None:
            try:
                await _our_query.close()
            except Exception as close_err:
                print(f"Claude adapter: cleanup error (suppressed): {close_err}")
        elif _our_transport is not None:
            # connect() succeeded but Query wasn't created — close transport directly
            try:
                await _our_transport.close()
            except Exception as close_err:
                print(f"Claude adapter: transport cleanup error (suppressed): {close_err}")


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
        self._inject_queue: asyncio.Queue[str] = asyncio.Queue()
        self._inject_event = asyncio.Event()
        self._session_id: str | None = None
        self._llm_logger = llm_logger
        # Per-turn transcript: accumulated structured records of every
        # AssistantMessage / UserMessage / ResultMessage seen during the SDK
        # stream. The adapter otherwise only persists prompt + final summary,
        # so without this the intermediate turns (tool uses, tool results,
        # thinking, per-turn text) are lost after the session ends.
        self._turns: list[dict] = []
        # References to the active SDK transport/query so stop() can force-kill
        # the subprocess instead of just setting a flag.
        self._active_transport = None
        self._active_query = None

    async def start(self, task: TaskContext) -> None:
        self._task = task
        self._cancel_event.clear()
        self._turns = []
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

            # Capture stderr from the CLI subprocess for diagnostics.
            # Without this, the SDK's ProcessError only says "Check stderr
            # output for details" which is useless for debugging.
            _stderr_lines: list[str] = []
            _STDERR_ALERT_PATTERNS = (
                "error",
                "enoent",
                "failed",
                "cannot find",
                "no such",
                "permission denied",
                "refused",
                "timed out",
                "timeout",
                "traceback",
            )

            def _capture_stderr(line: str) -> None:
                _stderr_lines.append(line)
                low = line.lower()
                # Promote likely-important lines to INFO so they land in
                # daemon.log without requiring DEBUG. Keeps routine stderr
                # (progress bars, etc.) quiet at DEBUG.
                if any(p in low for p in _STDERR_ALERT_PATTERNS):
                    logger.info("Claude CLI stderr (alert): %s", line.rstrip())
                else:
                    logger.debug("Claude CLI stderr: %s", line)

            options = ClaudeAgentOptions(
                allowed_tools=allowed,
                permission_mode=self._config.permission_mode,
                max_turns=self._config.max_turns,
                cwd=self._task.checkout_path or None,
                cli_path=system_claude,  # None → falls back to bundled binary
                stderr=_capture_stderr,
                # Isolate the agent from the host's Claude CLI config. Without this,
                # user-level claude.ai connectors (Gmail/Drive/Calendar) bleed into
                # every agent — including ones in a broken "needs-auth" state, which
                # then show up as tools the agent can see but never call successfully.
                # Agent-queue owns the tool surface via --mcp-config; nothing else
                # should leak in.
                extra_args={"strict-mcp-config": None},
            )
            if self._config.model:
                options.model = self._config.model
            if self._task.mcp_servers:
                options.mcp_servers = self._task.mcp_servers
            if self._task.add_dirs:
                # Translates to --add-dir <path> flags on the Claude CLI, so
                # the agent can Read/Edit/Write files outside cwd. Used by
                # the orchestrator to expose the project's vault memory
                # directory (insights, knowledge, facts) to every task.
                options.add_dirs = list(self._task.add_dirs)

            # Log the full launch surface: exactly which MCP servers the
            # Claude subprocess will try to connect to, and which tool names
            # are allowed. Makes "agent says it can't find tool X" debuggable
            # from daemon.log alone.
            def _summarize_mcp(mcp_servers: dict | None) -> list[str]:
                if not mcp_servers:
                    return []
                out: list[str] = []
                for sname, sconf in mcp_servers.items():
                    if isinstance(sconf, dict):
                        t = sconf.get("type")
                        if t == "http":
                            out.append(f"{sname}=http({sconf.get('url', '?')})")
                        elif t == "sdk":
                            out.append(f"{sname}=sdk-instance")
                        elif "command" in sconf:
                            out.append(f"{sname}=subprocess[{sconf['command']}]")
                        else:
                            out.append(f"{sname}=?")
                    else:
                        out.append(f"{sname}=<non-dict>")
                return out

            logger.info(
                "Claude adapter launch surface: task=%s cwd=%s model=%s "
                "mcp_servers=[%s] allowed_tools=%s add_dirs=%s",
                getattr(self._task, "task_id", "?"),
                options.cwd,
                options.model,
                ", ".join(_summarize_mcp(options.mcp_servers)) or "(none)",
                allowed,
                options.add_dirs or "[]",
            )

            # Track whether the original options included a resume request.
            # This flag survives even if `options` is replaced during retry,
            # so the error handler always knows a resume was attempted.
            _is_resume_attempt = False
            if self._task.resume_session_id:
                try:
                    from claude_agent_sdk import get_session_messages

                    msgs = get_session_messages(self._task.resume_session_id, limit=1)
                    if msgs:
                        options.resume = self._task.resume_session_id
                        options.fork_session = True
                        _is_resume_attempt = True
                        logger.info(
                            "Claude adapter: forking session %s",
                            self._task.resume_session_id,
                        )
                    else:
                        logger.info(
                            "Claude adapter: session %s not found, starting fresh",
                            self._task.resume_session_id,
                        )
                except Exception as e:
                    logger.warning(
                        "Claude adapter: session fork check failed (%s), starting fresh",
                        e,
                    )

            summary_parts = []
            tokens_used = 0
            current_prompt = self._build_prompt()

            mcp_names = list(self._task.mcp_servers.keys()) if self._task.mcp_servers else []
            logger.info(
                "Claude adapter: starting query (session=%s, model=%s, prompt=%d chars, "
                "allowed_tools=%s, mcp_servers=%s)",
                self._session_id or "new",
                self._config.model or "(default)",
                len(current_prompt),
                allowed,
                mcp_names,
            )

            # Import SDK error types for specific error classification
            try:
                from claude_agent_sdk._errors import ProcessError as _ProcessError
            except ImportError:
                _ProcessError = None
            try:
                from claude_agent_sdk._errors import CLIConnectionError as _CLIConnError
            except ImportError:
                _CLIConnError = None

            # Main query loop — runs once normally, but repeats when a
            # message is injected mid-execution (cancel → resume cycle).
            _resume_retry_attempted = False
            _resume_original_error: str | None = None
            while True:
                cli_error: str | None = None
                _interrupted_by_inject = False
                try:
                    async for message in _resilient_query(
                        prompt=current_prompt, options=options, adapter=self
                    ):
                        if self._cancel_event.is_set():
                            output = AgentOutput(
                                result=AgentResult.FAILED,
                                summary="Cancelled",
                                error_message="Agent was stopped",
                            )
                            self._log_session(current_prompt, output, _wait_start, _time)
                            return output

                        # Capture session ID from init message.
                        if hasattr(message, "subtype") and message.subtype == "init":
                            data = getattr(message, "data", {})
                            self._session_id = (
                                data.get("session_id")
                                if isinstance(data, dict)
                                else getattr(message, "session_id", None)
                            )
                            logger.info(
                                "Claude adapter: session started (%s)",
                                self._session_id,
                            )

                        # Forward interesting messages to the callback
                        if on_message:
                            text = self._extract_message_text(message)
                            if text:
                                await on_message(text)

                        # Record a structured turn for later analysis. Unlike
                        # the callback (lossy, display-oriented), this keeps
                        # full tool inputs/results.
                        turn = self._extract_structured_turn(message)
                        if turn is not None:
                            self._turns.append(turn)

                        # Capture result and token usage from ResultMessage
                        if isinstance(message, ResultMessage):
                            if getattr(message, "is_error", False):
                                err_subtype = getattr(message, "subtype", "") or "unknown"
                                err_result = str(getattr(message, "result", "") or "")
                                cli_error = (
                                    f"{err_subtype}: {err_result}".strip(": ") or err_subtype
                                )
                                logger.warning(
                                    "Claude adapter: CLI returned error result: %s",
                                    cli_error,
                                )
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
                    # If inject closed the transport, the stream may raise.
                    # Check inject_event before treating as a real error.
                    if self._inject_event.is_set() and not self._cancel_event.is_set():
                        _interrupted_by_inject = True
                    elif (
                        # Use _is_resume_attempt (set before query starts) in
                        # addition to checking options.resume.  This handles
                        # the race where a CLIConnectionError is raised instead
                        # of ProcessError (process exits before write completes).
                        (_is_resume_attempt or getattr(options, "resume", None))
                        and not _resume_retry_attempted
                        and not self._cancel_event.is_set()
                    ):
                        # Session resume/fork failed (e.g. session no longer
                        # exists on disk, corrupt, or CLI version mismatch).
                        # Fall back to a fresh session instead of failing the
                        # entire task.
                        from dataclasses import replace as _replace

                        error_msg = str(e)
                        # Append captured stderr for diagnostics
                        stderr_tail = "\n".join(_stderr_lines[-20:])
                        if stderr_tail:
                            error_msg += f"\nCLI stderr:\n{stderr_tail}"
                        _resume_original_error = error_msg
                        # Classify the error for better diagnostics
                        is_process_error = _ProcessError is not None and isinstance(
                            e, _ProcessError
                        )
                        is_conn_error = _CLIConnError is not None and isinstance(e, _CLIConnError)
                        exit_code = getattr(e, "exit_code", None) if is_process_error else None
                        logger.warning(
                            "Claude adapter: session resume failed "
                            "(error=%s, process_error=%s, conn_error=%s, "
                            "exit_code=%s), retrying as fresh session",
                            error_msg,
                            is_process_error,
                            is_conn_error,
                            exit_code,
                        )
                        if on_message:
                            await on_message("⚠️ Session resume failed — starting fresh session")
                        options = _replace(options, resume=None, fork_session=False)
                        _is_resume_attempt = False  # Fresh session, no longer a resume
                        _resume_retry_attempted = True
                        _stderr_lines.clear()  # Reset stderr for the fresh attempt
                        continue
                    else:
                        import traceback

                        error_msg = str(e)
                        # Append captured stderr for diagnostics
                        stderr_tail = "\n".join(_stderr_lines[-20:])
                        if stderr_tail:
                            error_msg += f"\nCLI stderr:\n{stderr_tail}"
                        full_traceback = traceback.format_exc()
                        # If this is a retry failure, include context about the
                        # original resume error so the user understands the full
                        # sequence of events.
                        if _resume_retry_attempted and _resume_original_error:
                            logger.error(
                                "Claude adapter: fresh session also failed "
                                "(original resume error: %s)",
                                _resume_original_error,
                            )
                            error_msg = (
                                f"Session resume failed ({_resume_original_error}), "
                                f"and fresh session also failed: {error_msg}"
                            )
                        else:
                            logger.error("Claude adapter error: %s", error_msg)
                        logger.debug("Full traceback:\n%s", full_traceback)
                        # The SDK often throws a generic ProcessError ("exit code 1")
                        # AFTER the CLI already sent a specific error via ResultMessage
                        # (stored in cli_error).  Check cli_error first since it has
                        # the real reason (e.g. "You've hit your limit").
                        classify_input = cli_error or error_msg
                        full_error = f"{error_msg}\n{full_traceback}"
                        result = _classify_error_result(classify_input)
                        output = AgentOutput(
                            result=result,
                            error_message=full_error
                            if result == AgentResult.FAILED
                            else (cli_error or error_msg),
                        )
                        self._log_session(current_prompt, output, _wait_start, _time)
                        return output

                # Check if an injected message interrupted the query.
                # Also handle the case where the stream ended cleanly but
                # an inject arrived between messages.
                if not _interrupted_by_inject and self._inject_event.is_set():
                    _interrupted_by_inject = True

                if _interrupted_by_inject and not self._inject_queue.empty():
                    injected = self._inject_queue.get_nowait()
                    self._inject_event.clear()
                    logger.info(
                        "Claude adapter: injecting message into session %s (%d chars)",
                        self._session_id,
                        len(injected),
                    )
                    if on_message:
                        await on_message("💬 **User message received** — resuming session")
                    # Resume same session with the injected message as prompt
                    from dataclasses import replace as _replace

                    options = _replace(
                        options,
                        resume=self._session_id,
                        fork_session=False,
                    )
                    current_prompt = injected
                    continue  # loop back to run a new query

                # Normal exit — no injection pending
                if cli_error:
                    # If this was a resume attempt and the CLI reported an
                    # error via ResultMessage (rather than an exception),
                    # retry as a fresh session before giving up.
                    if (
                        (_is_resume_attempt or getattr(options, "resume", None))
                        and not _resume_retry_attempted
                        and not self._cancel_event.is_set()
                    ):
                        from dataclasses import replace as _replace

                        _resume_original_error = cli_error
                        logger.warning(
                            "Claude adapter: CLI error during session resume "
                            "(%s), retrying as fresh session",
                            cli_error,
                        )
                        if on_message:
                            await on_message("⚠️ Session resume failed — starting fresh session")
                        options = _replace(options, resume=None, fork_session=False)
                        _is_resume_attempt = False
                        _resume_retry_attempted = True
                        cli_error = None
                        _stderr_lines.clear()
                        continue

                    logger.error("Claude adapter: query failed with CLI error: %s", cli_error)
                    if _resume_retry_attempted and _resume_original_error:
                        cli_error = (
                            f"Session resume failed ({_resume_original_error}), "
                            f"and fresh session also failed: {cli_error}"
                        )
                    result = _classify_error_result(cli_error)
                    output = AgentOutput(
                        result=result,
                        error_message=cli_error,
                        tokens_used=tokens_used,
                    )
                    self._log_session(current_prompt, output, _wait_start, _time)
                    return output

                logger.info(
                    "Claude adapter: query completed, %d result parts, %d tokens",
                    len(summary_parts),
                    tokens_used,
                )

                if tokens_used == 0 and not summary_parts:
                    # If this was a resume attempt that silently failed (process
                    # exited without error but produced no output), retry fresh.
                    if (
                        (_is_resume_attempt or getattr(options, "resume", None))
                        and not _resume_retry_attempted
                        and not self._cancel_event.is_set()
                    ):
                        from dataclasses import replace as _replace

                        _resume_original_error = "0 tokens and no output during resume"
                        logger.warning(
                            "Claude adapter: resume produced no output, retrying as fresh session"
                        )
                        if on_message:
                            await on_message(
                                "⚠️ Session resume produced no output — starting fresh session"
                            )
                        options = _replace(options, resume=None, fork_session=False)
                        _is_resume_attempt = False
                        _resume_retry_attempted = True
                        _stderr_lines.clear()
                        continue

                    logger.error("Claude adapter: 0 tokens and no output — treating as failure")
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
            transcript=list(self._turns),
        )

    _TOOL_RESULT_MAX_CHARS = 20_000

    def _extract_structured_turn(self, message) -> dict | None:
        """Return a structured record for a single SDK message, or ``None``.

        Companion to :meth:`_extract_message_text`: that one produces lossy,
        Discord-friendly text for live streaming; this one produces a
        machine-readable record for persistence and later analysis.

        Tool result payloads are truncated to ``_TOOL_RESULT_MAX_CHARS`` to
        keep per-session log size bounded; the original length is recorded
        so analysis code can tell when truncation happened.
        """
        if not _sdk_types_loaded:
            return None

        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).isoformat()

        if isinstance(message, _AssistantMessage):
            blocks: list[dict] = []
            for block in getattr(message, "content", None) or []:
                if isinstance(block, _ThinkingBlock):
                    blocks.append(
                        {"type": "thinking", "text": getattr(block, "thinking", "")}
                    )
                elif isinstance(block, _TextBlock):
                    blocks.append({"type": "text", "text": getattr(block, "text", "")})
                elif isinstance(block, _ToolUseBlock):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "input": getattr(block, "input", {}),
                        }
                    )
                elif isinstance(block, _ToolResultBlock):
                    # Rare on assistant messages but handle defensively.
                    content_val = getattr(block, "content", "")
                    blocks.append(self._tool_result_block(block, content_val))
            if not blocks:
                return None
            return {"ts": ts, "type": "assistant", "content": blocks}

        if isinstance(message, _UserMessage):
            blocks = []
            content = getattr(message, "content", None)
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, _ToolResultBlock):
                        blocks.append(
                            self._tool_result_block(block, getattr(block, "content", ""))
                        )
                    elif isinstance(block, _TextBlock):
                        blocks.append({"type": "text", "text": getattr(block, "text", "")})
            # Some SDK versions surface tool results via tool_use_result dict.
            tu_result = getattr(message, "tool_use_result", None)
            if isinstance(tu_result, dict) and tu_result.get("content"):
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu_result.get("tool_use_id", ""),
                        "is_error": bool(tu_result.get("is_error")),
                        "content": self._truncate(tu_result.get("content", "")),
                        "content_length": len(str(tu_result.get("content", ""))),
                    }
                )
            if not blocks:
                return None
            return {"ts": ts, "type": "user", "content": blocks}

        if isinstance(message, _ResultMessage):
            return {
                "ts": ts,
                "type": "result",
                "subtype": getattr(message, "subtype", ""),
                "is_error": bool(getattr(message, "is_error", False)),
                "result": str(getattr(message, "result", "") or ""),
                "usage": getattr(message, "usage", None) or {},
                "total_cost_usd": getattr(message, "total_cost_usd", None),
                "duration_ms": getattr(message, "duration_ms", None),
                "num_turns": getattr(message, "num_turns", None),
            }

        return None

    def _tool_result_block(self, block, content_val) -> dict:
        """Shape a ToolResultBlock for the transcript, truncating if needed."""
        if isinstance(content_val, list):
            # SDK sometimes returns a list of content parts (text + image).
            text_parts = [
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content_val
            ]
            content_str = "\n".join(text_parts)
        else:
            content_str = str(content_val or "")
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "tool_use_id", ""),
            "is_error": bool(getattr(block, "is_error", False)),
            "content": self._truncate(content_str),
            "content_length": len(content_str),
        }

    def _truncate(self, s: str) -> str:
        s = str(s or "")
        if len(s) <= self._TOOL_RESULT_MAX_CHARS:
            return s
        return s[: self._TOOL_RESULT_MAX_CHARS] + f"\n…[truncated {len(s) - self._TOOL_RESULT_MAX_CHARS} chars]"

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

    async def inject_message(self, msg: str) -> None:
        """Inject a message into the running session.

        Queues the message and interrupts the current SDK query so that
        ``_wait()`` can resume the session with the new prompt.
        """
        self._inject_queue.put_nowait(msg)
        self._inject_event.set()
        # Gracefully close the active query so the session is saved to disk
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
        """Build the final prompt from TaskContext using PromptBuilder.

        L0 (role) and L1 (facts) are injected as first-class PromptBuilder
        layers from TaskContext, ensuring they are always present at the top
        of the prompt in the correct tier order (L0 → L1 → description).
        See docs/specs/design/memory-scoping.md §2 and Roadmap 3.3.5.
        """
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()

        # L0 Identity tier — agent role (~50 tokens, always present at task start)
        if self._task.l0_role:
            builder.set_l0_role(self._task.l0_role)

        # Project-scoped profile's role — appended after L0 so the agent sees
        # base agent-type identity followed by project specialisation.
        if self._task.project_override_role:
            builder.set_override_content(self._task.project_override_role)

        # L1 Critical Facts tier — project/agent-type KV entries (~200 tokens)
        if self._task.l1_facts:
            builder.set_l1_facts(self._task.l1_facts)

        # L1 Guidance tier — deterministic behavioral rules (~300 tokens)
        if self._task.l1_guidance:
            builder.set_l1_guidance(self._task.l1_guidance)

        # L2 Topic Context tier — semantic search results (~500 tokens)
        if self._task.l2_context:
            builder.set_l2_context(self._task.l2_context)

        # Main description (assembled by orchestrator: system context,
        # execution rules, override, upstream work, task description)
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
