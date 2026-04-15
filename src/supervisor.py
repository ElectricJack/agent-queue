"""Supervisor — the single intelligent entity coordinating AgentQueue.

**Supervisor** -- the multi-turn conversation loop that manages the system.
The ``chat()`` method sends the user message (plus history) to the LLM,
checks if the response contains tool-use blocks, executes those tools via
``CommandHandler``, feeds the results back, and repeats until the LLM
produces a final text response.

Tool definitions live in ``tool_registry.py``.  ``TOOLS`` is kept here
as a backward-compatible alias that returns all tools from the registry.

Design boundaries:
    - History management (compaction, summarization, per-channel storage)
      lives in the Discord bot layer, not here.  Supervisor is stateless
      between calls -- the caller passes history in and gets text out.
    - The system prompt shapes the LLM's persona and operating rules.
      It is NOT a code-worker prompt; it instructs the LLM to act as a
      dispatcher that plans and delegates to agents via the tool interface.

See ``specs/supervisor.md`` for the full behavioral specification.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

from src.chat_providers import ChatProvider, LoggedChatProvider, create_chat_provider
from src.commands.handler import CommandHandler
from src.config import AppConfig, ChatProviderConfig
from src.llm_logger import LLMLogger
from src.orchestrator import Orchestrator
from src.reflection import ReflectionEngine, ReflectionVerdict
from src.tools.registry import ToolRegistry as _ToolRegistry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Context variable for per-hook provider overrides.  Each asyncio task gets
# its own copy, so concurrent hooks don't race on a shared attribute.
_hook_provider_override: contextvars.ContextVar[ChatProvider | None] = contextvars.ContextVar(
    "_hook_provider_override", default=None
)


# ---------------------------------------------------------------------------
# Tool definitions -- the LLM's interface to the system.
#
# Each entry describes one operation the LLM can invoke during a conversation.
# The names match CommandHandler._cmd_* methods (e.g. "create_task" calls
# _cmd_create_task).  The input_schema tells the LLM what arguments are
# available; the description tells it *when* to use the tool.
# ---------------------------------------------------------------------------
# Tool definitions have moved to tool_registry.py.
# TOOLS is kept as a backward-compatible alias.
TOOLS = _ToolRegistry().get_all_tools()

# ---------------------------------------------------------------------------
# System prompt -- now lives in src/prompts/chat_agent_system.md.
# SYSTEM_PROMPT_TEMPLATE below is a deprecated backward-compat stub.
# The actual prompt is loaded via PromptBuilder in _build_system_prompt().
# ---------------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = """You are AgentQueue, a Discord bot that manages an AI agent task queue.
Workspaces root: {workspace_dir}
Use browse_tools/load_tools to discover and load tool categories on demand."""


# Maps tool names to the input_data key (str) or extractor (callable) used
# to build a short label for observer output.  Keeps _tool_label compact and
# easy to extend when new tools are added.
_TOOL_DETAIL_KEYS: dict[str, str | Callable[[dict], str | None]] = {
    "run_command": "command",
    "search_files": lambda d: (
        f"{d.get('mode', 'grep')}: {d['pattern']}" if d.get("pattern") else d.get("mode", "grep")
    ),
    "create_task": "title",
    "update_task": "task_id",
    "git_log": "project_id",
    "git_diff": "project_id",
    "git_status": "project_id",
    "git_commit": "message",
    "git_push": "branch",
    "git_pull": "branch",
    "git_checkout": "branch",
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "glob_files": "pattern",
    "grep": "pattern",
    "list_directory": lambda d: d.get("path") or d.get("project_id"),
    "list_tasks": "status",
    "assign_task": "task_id",
}


def _tool_label(name: str, input_data: dict) -> str:
    """Return a short descriptive label for a tool call.

    Instead of just ``run_command`` this produces something like
    ``run_command(pytest tests/)``, giving observers a quick sense of
    what the agent is actually doing at each step.
    """
    extractor = _TOOL_DETAIL_KEYS.get(name)
    if extractor is None:
        return name

    detail = extractor(input_data) if callable(extractor) else input_data.get(extractor)
    if detail:
        # Truncate long details (e.g. long shell commands)
        if len(detail) > 60:
            detail = detail[:57] + "..."
        return f"{name}({detail})"
    return name


def _infer_provider_from_model(model: str) -> str | None:
    """Infer the chat-provider type from a model name string.

    Returns ``"anthropic"``, ``"gemini"``, or *None* when the provider
    cannot be reliably determined (e.g. an Ollama model name).
    """
    m = model.lower()
    # Anthropic models: "claude-*" or Vertex-style "claude-*@date"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gemini"):
        return "gemini"
    return None


class Supervisor:
    """Platform-agnostic LLM supervisor for managing the AgentQueue system.

    Owns the tool definitions, system prompt, LLM client, and multi-turn
    tool-use loop.  Callers (Discord bot, CLI, web API) are responsible for
    building message history and routing responses.

    Business logic is delegated to the shared CommandHandler so that Discord
    slash commands and the supervisor use the same code path.
    """

    def __init__(
        self, orchestrator: Orchestrator, config: AppConfig, llm_logger: LLMLogger | None = None
    ):
        """Initialise the supervisor.

        Args:
            orchestrator: The running orchestrator instance — used for
                accessing the database and creating the ``CommandHandler``.
            config: Application configuration (chat provider, reflection, etc.).
            llm_logger: Optional logger for capturing all LLM interactions.
        """
        self.orchestrator = orchestrator
        self.config = config
        self._provider: ChatProvider | None = None
        self._llm_logger = llm_logger
        self.handler = CommandHandler(orchestrator, config)
        self.reflection = ReflectionEngine(config.supervisor.reflection)
        self._registry = _ToolRegistry()
        # Full message history from the last chat() call, including tool
        # calls and results.  Used by PlaybookRunner to preserve inter-node
        # context.  Reset at the start of each _chat_inner() call.
        self._last_messages: list[dict] = []
        # Tool actions from the last chat() call (for memory extraction).
        self._last_tool_actions: list[str] = []
        # Stack of cancel events — one per concurrent chat() call.
        # Using a stack instead of a single event prevents concurrent/recursive
        # chat() calls (e.g. hook LLM + user chat, or reflection retry) from
        # clobbering each other's cancel state.
        self._cancel_events: list[asyncio.Event] = []
        # Serialises all LLM-using entry points so that only one request
        # is processed at a time.  Concurrent callers (Discord messages,
        # hooks, task-completion pipeline) queue on this lock.
        self._llm_lock = asyncio.Lock()

    def initialize(self) -> bool:
        """Create LLM provider. Returns True if provider is ready."""
        provider = create_chat_provider(self.config.chat_provider)
        if provider and self._llm_logger and self._llm_logger._enabled:
            provider = LoggedChatProvider(provider, self._llm_logger, caller="supervisor.chat")
        self._provider = provider
        return self._provider is not None

    @property
    def is_ready(self) -> bool:
        return self._provider is not None

    async def is_model_loaded(self) -> bool:
        """Check if the LLM model is loaded and ready (delegates to provider)."""
        if not self._provider:
            return True
        return await self._provider.is_model_loaded()

    @property
    def model(self) -> str | None:
        return self._provider.model_name if self._provider else None

    def _resolve_call_provider(self, llm_config: dict | None) -> ChatProvider | None:
        """Create a one-shot provider when *llm_config* requests a different model.

        Returns a ready-to-use :class:`ChatProvider` (wrapped with
        :class:`LoggedChatProvider` when logging is enabled) or *None* when no
        swap is needed — i.e. the caller should fall back to the default
        provider.

        The returned provider is **not** stored on ``self`` — it lives only
        for the duration of the ``_chat_inner`` call that requested it.

        Supported ``llm_config`` keys:

        * ``model`` — model name to use (e.g. ``"gemini-2.5-flash"``).
        * ``provider`` — explicit provider type (``"anthropic"``,
          ``"gemini"``, ``"ollama"``).  If omitted, inferred from
          ``model`` via :func:`_infer_provider_from_model`, falling back
          to the current default provider type.
        * ``base_url`` — Ollama base URL override.
        * ``api_key`` — Gemini API key override.

        ``max_tokens`` and ``temperature`` are *not* handled here; they
        are applied directly at the ``create_message()`` call site.
        """
        if not llm_config:
            return None

        requested_model = llm_config.get("model")
        requested_provider = llm_config.get("provider")

        # Nothing to swap if no model/provider override was specified.
        if not requested_model and not requested_provider:
            return None

        # Determine the effective provider type.
        default_cfg = self.config.chat_provider
        if requested_provider:
            eff_provider = requested_provider
        elif requested_model:
            eff_provider = _infer_provider_from_model(requested_model) or default_cfg.provider
        else:
            eff_provider = default_cfg.provider

        eff_model = requested_model or default_cfg.model

        # Short-circuit: if effective provider+model match the current
        # default, no swap is needed.
        if eff_provider == default_cfg.provider and eff_model == (
            self._provider.model_name if self._provider else default_cfg.model
        ):
            return None

        cfg = ChatProviderConfig(
            provider=eff_provider,
            model=str(eff_model) if eff_model else "",
            base_url=llm_config.get("base_url", default_cfg.base_url),
            api_key=llm_config.get("api_key", default_cfg.api_key),
            keep_alive=default_cfg.keep_alive,
            num_ctx=default_cfg.num_ctx,
        )

        provider = create_chat_provider(cfg)
        if provider is None:
            logger.warning(
                "llm_config requested provider %s / model %s but create_chat_provider "
                "returned None — falling back to default provider",
                eff_provider,
                eff_model,
            )
            return None

        # Wrap with logging if enabled (mirrors initialize()).
        if self._llm_logger and self._llm_logger._enabled:
            provider = LoggedChatProvider(
                provider,
                self._llm_logger,
                caller="supervisor.chat:llm_config_override",
            )

        logger.info(
            "llm_config override: using provider=%s model=%s for this call",
            eff_provider,
            provider.model_name,
        )
        return provider

    def set_active_project(self, project_id: str | None) -> None:
        self.handler.set_active_project(project_id)

    @property
    def _active_project_id(self) -> str | None:
        return self.handler._active_project_id

    def reload_credentials(self) -> bool:
        """Re-create the LLM provider (e.g. after token refresh). Returns True on success."""
        return self.initialize()

    def cancel(self) -> None:
        """Cancel all active chat() calls.

        Sets all internal cancel events so every in-flight response loop
        exits immediately at the next checkpoint.  Safe to call from any
        coroutine — events are checked between LLM calls and tool
        executions.
        """
        for ev in self._cancel_events:
            ev.set()

    @property
    def is_chatting(self) -> bool:
        """True while at least one ``chat()`` call is in progress."""
        return any(not ev.is_set() for ev in self._cancel_events)

    async def _build_system_prompt(
        self,
        *,
        l2_query: str = "",
        preloaded_categories: list[str] | None = None,
        extra_context: dict[str, str] | None = None,
    ) -> str:
        """Build the system prompt for the current conversation.

        Uses ``PromptBuilder`` to assemble identity + active project context.
        Called before every LLM call so the prompt always reflects the
        current project scope.

        Args:
            l2_query: Optional user text for L2 semantic memory search.
                      Pass on the first round only to avoid redundant
                      embedding calls during the tool-loop.
            extra_context: Optional dict of named context blocks to inject
                (e.g. channel context, thread context from Discord).

        Returns:
            Assembled system prompt string.
        """
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        builder.set_identity(
            "supervisor-system",
            {"workspace_dir": self.config.workspace_dir},
        )

        # Load supervisor profile for L0 role context
        profile_text = await self._load_supervisor_profile()
        if profile_text:
            builder.set_l0_role_from_markdown(profile_text)

        if self._active_project_id:
            # Fetch project metadata to include in context
            project_context = await self._build_active_project_context(self._active_project_id)
            builder.add_context("active_project", project_context)

            # L1 Critical Facts — inject project facts so the supervisor has
            # key context without needing explicit memory_search calls.
            mem_svc = getattr(self.orchestrator, "_memory_v2_service", None)
            if mem_svc:
                try:
                    l1_text = await mem_svc.load_l1_facts(
                        project_id=self._active_project_id,
                    )
                    if l1_text:
                        builder.set_l1_facts(l1_text)
                except Exception:
                    pass  # graceful degradation

                # L2 Topic Context — semantic search for relevant insights.
                if l2_query:
                    try:
                        l2_text = await mem_svc.load_l2_context(
                            l2_query,
                            project_id=self._active_project_id,
                        )
                        if l2_text:
                            builder.set_l2_context(l2_text)
                    except Exception:
                        pass  # graceful degradation
        # Inject caller-provided context blocks (channel context, thread
        # context, etc.) before the tool index.
        if extra_context:
            for ctx_name, ctx_content in extra_context.items():
                builder.add_context(ctx_name, ctx_content)

        # Exclude preloaded categories from the tool index — the LLM already
        # has their full schemas, so listing names again is duplication.
        exclude_cats = set(preloaded_categories or [])
        tool_index = self._registry.get_tool_index(exclude=exclude_cats)
        if tool_index:
            builder.add_context("tool_index", f"## Tool Index\n\n{tool_index}")
        system_prompt, _ = builder.build()
        return system_prompt

    async def _load_supervisor_profile(self) -> str | None:
        """Load the supervisor profile from the vault.

        Returns the raw markdown content or ``None`` if unavailable.
        """
        profile_path = os.path.join(
            self.config.data_dir, "vault", "agent-types", "supervisor", "profile.md"
        )
        try:
            text = await asyncio.to_thread(self._read_file, profile_path)
            return text if text else None
        except Exception:
            return None

    @staticmethod
    def _read_file(path: str) -> str | None:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    async def _build_active_project_context(self, project_id: str) -> str:
        """Build a rich context block for the active project.

        Fetches project metadata from the database so the supervisor
        has immediate access to key project info (repo URL, workspace
        path, etc.) without needing to call tools.

        When ``repo_url`` is empty but the workspace has a git remote,
        auto-detects and persists the remote URL so future lookups are
        instant.
        """
        lines = [
            f"ACTIVE PROJECT: `{project_id}`. "
            f"Use this as the default project_id for all tools unless the user "
            f"explicitly specifies a different project. When creating tasks, "
            f"listing notes, or any project-scoped operation, use this project.",
        ]
        try:
            project = await self.orchestrator.db.get_project(project_id)
            if project:
                repo_url = project.repo_url
                ws_path = await self.orchestrator.db.get_project_workspace_path(project_id)

                # Auto-detect repo_url from git remote if not set
                if not repo_url and ws_path:
                    try:
                        detected = await self.orchestrator.git.aget_remote_url(ws_path)
                        if detected:
                            repo_url = detected
                            await self.orchestrator.db.update_project(project_id, repo_url=repo_url)
                    except Exception:
                        pass  # Non-fatal — proceed without URL

                if repo_url:
                    lines.append(f"Repository URL: {repo_url}")
                if ws_path:
                    lines.append(f"Workspace: {ws_path}")
                if project.repo_default_branch:
                    lines.append(f"Default branch: {project.repo_default_branch}")
        except Exception:
            pass  # graceful degradation — ID-only context still works
        return "\n".join(lines)

    async def reflect(
        self,
        trigger: str,
        action_summary: str,
        action_results: list[dict],
        messages: list[dict],
        active_tools: dict[str, dict],
        tool_names: list[str] | None = None,
    ) -> ReflectionVerdict | None:
        """Run a reflection pass for the given trigger.

        Called after actions complete. Evaluates results, checks rules,
        and may take follow-up actions (depth-limited).

        Returns a ``ReflectionVerdict`` when reflection ran, or ``None``
        when reflection was skipped (disabled, circuit breaker, etc.).
        """
        if not self._provider:
            return None
        if not self.reflection.should_reflect(trigger, tool_names=tool_names):
            return None

        depth = self.reflection.determine_depth(trigger, {})
        if not depth:
            return None

        reflection_prompt = self.reflection.build_reflection_prompt(
            depth=depth,
            trigger=trigger,
            action_summary=action_summary,
            action_results=action_results,
        )

        messages.append(
            {
                "role": "user",
                "content": f"[system reflection]: {reflection_prompt}",
            }
        )

        try:
            reflect_resp = await self._provider.create_message(
                messages=messages,
                system=await self._build_system_prompt(),
                tools=list(active_tools.values()),
                max_tokens=512,
            )

            # Collect all text from reflection (including after tool use)
            reflection_text_parts = list(reflect_resp.text_parts)

            if reflect_resp.tool_uses and self.reflection.can_reflect_deeper(1):
                messages.append({"role": "assistant", "content": reflect_resp.tool_uses})
                for tool_use in reflect_resp.tool_uses:
                    result = await self._execute_tool(tool_use.name, tool_use.input)
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_use.id,
                                    "content": json.dumps(result),
                                }
                            ],
                        }
                    )

            estimated_tokens = len(reflection_prompt) // 4
            self.reflection.record_tokens(estimated_tokens)

            # Parse verdict from reflection text
            full_text = "\n".join(reflection_text_parts)
            return self.reflection.parse_verdict(full_text)
        except Exception:
            return None  # Reflection failure never breaks the main flow

    async def chat(
        self,
        text: str,
        user_name: str,
        history: list[dict] | None = None,
        on_progress: "Callable[[str, str | None], Awaitable[None]] | None" = None,
        _reflection_trigger: str = "user.request",
        llm_config: dict | None = None,
        tool_overrides: list[str] | None = None,
        context: dict[str, str] | None = None,
    ) -> str:
        """Process a user message with tool use. Returns response text.

        Acquires ``_llm_lock`` so that only one LLM interaction runs at a
        time.  Internal callers that already hold the lock should use
        ``_chat_unlocked()`` instead.

        Args:
            llm_config: Optional dict to override LLM parameters for this
                single call.  Supported keys:

                * ``model`` — model name (e.g. ``"gemini-2.5-flash"``).
                  When specified, a one-shot provider is created for
                  this call; subsequent calls without ``llm_config``
                  revert to the default provider.
                * ``provider`` — explicit provider type
                  (``"anthropic"``, ``"gemini"``, ``"ollama"``).  If
                  omitted, inferred from ``model``.
                * ``max_tokens`` — per-call token limit (default 1024).
                * ``base_url`` — Ollama base-URL override.
                * ``api_key`` — Gemini API-key override.

                When *None* (the default), the configured provider is
                used unchanged.
            tool_overrides: Optional list of tool names to make available
                for this call.  When *None* (the default), the full
                default tool set is used (backward compatible).  An empty
                list ``[]`` means no tools (text-only response).  Unknown
                tool names raise ``ValueError`` before the LLM call.
            context: Optional dict of named context blocks to inject
                into the system prompt (e.g. channel context, thread
                context).  Keys are context names, values are content
                strings.
        """
        async with self._llm_lock:
            return await self._chat_unlocked(
                text,
                user_name,
                history,
                on_progress,
                _reflection_trigger,
                llm_config=llm_config,
                tool_overrides=tool_overrides,
                context=context,
            )

    async def _chat_unlocked(
        self,
        text: str,
        user_name: str,
        history: list[dict] | None = None,
        on_progress: "Callable[[str, str | None], Awaitable[None]] | None" = None,
        _reflection_trigger: str = "user.request",
        llm_config: dict | None = None,
        tool_overrides: list[str] | None = None,
        context: dict[str, str] | None = None,
    ) -> str:
        """Process a user message without acquiring ``_llm_lock``.

        Starts with core tools only. When the LLM calls ``load_tools``,
        the requested category's tool definitions are added to the active
        set for subsequent turns within this interaction.

        ``history`` is a list of {"role": "user"|"assistant", "content": ...}
        dicts.  The caller is responsible for building history from whatever
        source it uses (Discord channel, CLI readline, HTTP session, etc.).

        ``on_progress`` is an optional async callback for reporting progress
        during multi-turn processing.  It receives ``(event, detail)`` where
        *event* is one of ``"thinking"``, ``"tool_use"``, or ``"responding"``
        and *detail* is an optional string (e.g. tool name).  This allows the
        caller to display intermediate status in a UI (Discord thinking
        indicator, etc.).

        ``llm_config`` — see :meth:`chat` for details.

        ``tool_overrides`` — see :meth:`chat` for details.
        """
        if not self._provider:
            raise RuntimeError("LLM provider not initialized — call initialize() first")

        structlog.contextvars.bind_contextvars(component="supervisor")

        # Each chat() call gets its own cancel event on the stack so that
        # concurrent calls (hook LLM + user chat) or recursive calls
        # (reflection retry) don't clobber each other's cancellation state.
        cancel_event = asyncio.Event()
        self._cancel_events.append(cancel_event)

        try:
            response = await self._chat_inner(
                text,
                user_name,
                history,
                on_progress,
                _reflection_trigger,
                cancel_event=cancel_event,
                llm_config=llm_config,
                tool_overrides=tool_overrides,
                context=context,
            )
            # Emit event for memory extraction (background, non-blocking)
            bus = getattr(self.orchestrator, "bus", None)
            if bus and self._last_tool_actions:
                try:
                    await bus.emit("supervisor.chat.completed", {
                        "project_id": self._active_project_id or "",
                        "user_text": text,
                        "response": response or "",
                        "tools_used": list(self._last_tool_actions),
                    })
                except Exception:
                    pass  # non-critical, don't break the chat flow
            return response
        finally:
            self._cancel_events.remove(cancel_event)
            # Clear conversation context so it doesn't leak to future calls
            self.handler._current_conversation_context = None

    @staticmethod
    def _serialize_conversation_context(messages: list[dict]) -> str:
        """Extract a human-readable conversation transcript from LLM messages.

        Filters out tool-use blocks and tool-result blocks, keeping only the
        textual user/assistant exchanges so the downstream agent gets the
        conversational thread without noise from tool invocations.
        """
        lines: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            # Skip tool_result messages (list of dicts with type: tool_result)
            if isinstance(content, list):
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            if role == "user":
                lines.append(f"**User:** {content}")
            elif role == "assistant":
                lines.append(f"**Assistant:** {content}")
        return "\n\n".join(lines) if lines else ""

    async def _chat_inner(
        self,
        text: str,
        user_name: str,
        history: list[dict] | None = None,
        on_progress: "Callable[[str, str | None], Awaitable[None]] | None" = None,
        _reflection_trigger: str = "user.request",
        cancel_event: asyncio.Event | None = None,
        llm_config: dict | None = None,
        tool_overrides: list[str] | None = None,
        context: dict[str, str] | None = None,
    ) -> str:
        """Inner implementation of chat() — separated so chat() can manage
        the cancel event lifecycle in a try/finally.

        ``cancel_event`` is the per-call event created in ``chat()``.
        Using a stack-based list avoids races where concurrent or recursive
        ``chat()`` calls clobber each other's cancellation state.

        After this method returns, ``self._last_messages`` contains the
        full message history from the tool-use loop, including tool calls
        and results.  The playbook runner uses this to preserve inter-node
        context (see ``PlaybookRunner._execute_node``).

        ``llm_config`` — see :meth:`chat` for details.

        ``tool_overrides`` — see :meth:`chat` for details.
        """
        registry = self._registry

        # Resolve a per-call provider override from llm_config.  This is
        # created once and used for *all* rounds in the multi-turn loop,
        # but is not stored on `self` — subsequent calls without llm_config
        # will use the default provider.
        call_provider = self._resolve_call_provider(llm_config)

        # Determine which provider name governs schema compression.
        # When a per-call provider is active, check *its* identity instead
        # of the static config so that e.g. an ollama override still gets
        # compressed schemas.
        effective_provider_name = self.config.chat_provider.provider
        if llm_config:
            effective_provider_name = (
                llm_config.get("provider")
                or (
                    _infer_provider_from_model(llm_config["model"])
                    if llm_config.get("model")
                    else None
                )
                or self.config.chat_provider.provider
            )

        # Use compressed schemas for local LLMs with small context windows
        compressed = effective_provider_name == "ollama"

        if tool_overrides is not None:
            # Validate all requested tool names exist in the registry.
            all_known = {t["name"] for t in registry.get_all_tools()}
            unknown = set(tool_overrides) - all_known
            if unknown:
                raise ValueError(
                    f"Unknown tool names in tool_overrides: {sorted(unknown)}"
                )

            # Build tool set from only the specified tools (empty list = no tools).
            all_tools_map = {t["name"]: t for t in registry.get_all_tools()}
            active_tools: dict[str, dict] = {}
            preloaded_categories: list[str] = []
            for name in tool_overrides:
                tool = all_tools_map[name]
                if compressed:
                    tool = registry.compress_tool_schema(tool)
                active_tools[name] = tool
        else:
            # Default: start with core tools, expand via load_tools
            active_tools: dict[str, dict] = {
                t["name"]: t for t in registry.get_core_tools(compressed=compressed)
            }

            # Pre-load categories relevant to the user's prompt so the LLM
            # doesn't need to spend a turn calling load_tools.
            preloaded_categories: list[str] = []
            relevant_cats = registry.search_relevant_categories(text)
            for cat_name in relevant_cats:
                cat_tools = registry.get_category_tools(cat_name, compressed=compressed)
                if cat_tools:
                    for t in cat_tools:
                        active_tools[t["name"]] = t
                    preloaded_categories.append(cat_name)

        messages = list(history) if history else []

        # Append current message
        current = {"role": "user", "content": f"[from {user_name}]: {text}"}
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] += "\n" + current["content"]
        else:
            messages.append(current)

        # Expose the messages list so callers (e.g. PlaybookRunner) can
        # access the full conversation including tool calls and results
        # after chat() returns.  Since messages is mutated in-place during
        # the tool loop, this reference stays current automatically.
        self._last_messages = messages

        # Set the conversation context on the handler so that any tasks
        # created during this chat session inherit the thread chain.
        self.handler._current_conversation_context = self._serialize_conversation_context(messages)

        # Multi-turn tool-use loop
        tool_actions: list[str] = []
        self._last_tool_actions = tool_actions  # expose for memory extraction
        tool_names_used: list[str] = []  # bare tool names for reflection gating
        # Accumulated tool results for reflection
        accumulated_tool_results: list[dict] = []
        # Track how many times we've nudged the LLM to call reply_to_user
        nudge_count = 0

        # Build the system prompt once and cache it for the duration of
        # this conversation.  L2 semantic search uses the user's text as
        # the query; subsequent rounds reuse the cached prompt (L2 is
        # already first-round-only, and project context / L1 facts don't
        # change mid-conversation).
        cached_system_prompt = await self._build_system_prompt(
            l2_query=text,
            preloaded_categories=preloaded_categories,
            extra_context=context,
        )

        round_num = 0
        while True:  # No step limit — agents run until they finish
            # Check for cancellation before each round
            if cancel_event and cancel_event.is_set():
                if on_progress:
                    await on_progress("cancelled", None)
                return "Cancelled."

            # Notify caller that the LLM is thinking
            if on_progress:
                if round_num == 0:
                    await on_progress("thinking", None)
                else:
                    await on_progress("thinking", f"round {round_num + 1}")

            # Provider priority: per-call llm_config override > per-hook
            # contextvar override > default self._provider.
            active_provider = call_provider or _hook_provider_override.get() or self._provider

            # Apply max_tokens from llm_config (or default 1024).
            effective_max_tokens = llm_config.get("max_tokens", 1024) if llm_config else 1024

            resp = await active_provider.create_message(
                messages=messages,
                system=cached_system_prompt,
                tools=list(active_tools.values()),
                max_tokens=effective_max_tokens,
            )

            if not resp.tool_uses:
                if on_progress:
                    await on_progress("responding", None)
                response = "\n".join(resp.text_parts).strip()

                # If the LLM produced text after having used tools (without
                # calling reply_to_user), auto-deliver the text as the reply
                # instead of nudging for another round.  This eliminates a
                # full LLM round-trip (~3,000+ tokens) that almost always
                # produces the same text wrapped in reply_to_user.
                if tool_actions and response:
                    # Run reflection on the auto-delivered response
                    messages.append(
                        {"role": "assistant", "content": response}
                    )
                    verdict = await self.reflect(
                        trigger=_reflection_trigger,
                        action_summary=", ".join(tool_actions),
                        action_results=accumulated_tool_results,
                        messages=messages,
                        active_tools=active_tools,
                        tool_names=tool_names_used,
                    )
                    if (
                        verdict
                        and not verdict.passed
                        and not getattr(self, "_reflection_retry_active", False)
                    ):
                        self._reflection_retry_active = True
                        try:
                            retry_prompt = (
                                "Your previous response was evaluated and found "
                                "inadequate.\n\n"
                                f"**Reflection feedback:** {verdict.reason}\n"
                            )
                            if verdict.suggested_followup:
                                retry_prompt += (
                                    f"**Suggested followup:** {verdict.suggested_followup}\n"
                                )
                            retry_prompt += (
                                f"\n**Original user request:** {text}\n\n"
                                "Please try again, addressing the feedback above. "
                                "Remember to call reply_to_user with your response."
                            )
                            return await self._chat_unlocked(
                                text=retry_prompt,
                                user_name="system:reflection-retry",
                                history=messages,
                                on_progress=on_progress,
                                _reflection_trigger=_reflection_trigger,
                                llm_config=llm_config,
                                tool_overrides=tool_overrides,
                            )
                        finally:
                            self._reflection_retry_active = False
                    return response

                # If tools were used but LLM produced empty text, nudge once
                if tool_actions and not response and nudge_count < 1:
                    nudge_count += 1
                    messages.append(
                        {"role": "assistant", "content": "(no text)"}
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[system]: You must call the `reply_to_user` tool "
                                "to deliver your response. Do not just stop — "
                                "compose a complete answer that addresses the "
                                "user's request and call `reply_to_user` with it."
                            ),
                        }
                    )
                    continue

                # No tools were used at all — direct conversational response
                if response:
                    return response
                return "Done."

            # Check if reply_to_user is among the tool calls
            reply_message = None
            other_tool_uses = []
            for tool_use in resp.tool_uses:
                if tool_use.name == "reply_to_user":
                    reply_message = (tool_use.input or {}).get("message", "")
                else:
                    other_tool_uses.append(tool_use)

            # Execute non-reply tools first
            messages.append({"role": "assistant", "content": resp.tool_uses})
            tool_results = []

            for tool_use in resp.tool_uses:
                if tool_use.name == "reply_to_user":
                    # Acknowledge the reply tool call but don't execute it
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": json.dumps({"status": "delivered"}),
                        }
                    )
                    continue

                label = _tool_label(tool_use.name, tool_use.input)
                if on_progress:
                    await on_progress("tool_use", label)
                result = await self._execute_tool(tool_use.name, tool_use.input)
                tool_actions.append(label)
                tool_names_used.append(tool_use.name)
                accumulated_tool_results.append(
                    {
                        "tool": label,
                        "result": result,
                    }
                )

                # If load_tools was called, expand active tool set.
                # Skip expansion when tool_overrides is active — the override
                # set is the complete, fixed tool set for this call.
                if (
                    tool_overrides is None
                    and tool_use.name == "load_tools"
                    and "loaded" in result
                ):
                    if result.get("single_tool"):
                        # Single-tool mode — inject just the one tool
                        name = result["tools_added"][0]
                        tool_def = registry.get_tool_definition(
                            name, compressed=compressed,
                        )
                        if tool_def:
                            active_tools[tool_def["name"]] = tool_def
                    else:
                        # Category mode — inject all tools from the category
                        category = result["loaded"]
                        cat_tools = registry.get_category_tools(
                            category,
                            compressed=compressed,
                        )
                        if cat_tools:
                            for t in cat_tools:
                                active_tools[t["name"]] = t

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(result),
                    }
                )

            messages.append({"role": "user", "content": tool_results})

            # Check for cancellation after tool execution
            if cancel_event and cancel_event.is_set():
                if on_progress:
                    await on_progress("cancelled", None)
                return "Cancelled."

            # If reply_to_user was called, deliver the response
            if reply_message is not None:
                if on_progress:
                    await on_progress("responding", None)
                response = reply_message.strip()

                # --- Reflection pass (after tool use) ---
                if tool_actions:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": response or "Done.",
                        }
                    )
                    verdict = await self.reflect(
                        trigger=_reflection_trigger,
                        action_summary=", ".join(tool_actions),
                        action_results=accumulated_tool_results,
                        messages=messages,
                        active_tools=active_tools,
                        tool_names=tool_names_used,
                    )

                    if (
                        verdict
                        and not verdict.passed
                        and not getattr(self, "_reflection_retry_active", False)
                    ):
                        self._reflection_retry_active = True
                        try:
                            retry_prompt = (
                                "Your previous response was evaluated and found "
                                "inadequate.\n\n"
                                f"**Reflection feedback:** {verdict.reason}\n"
                            )
                            if verdict.suggested_followup:
                                retry_prompt += (
                                    f"**Suggested followup:** {verdict.suggested_followup}\n"
                                )
                            retry_prompt += (
                                f"\n**Original user request:** {text}\n\n"
                                "Please try again, addressing the feedback above. "
                                "Remember to call reply_to_user with your response."
                            )
                            return await self._chat_unlocked(
                                text=retry_prompt,
                                user_name="system:reflection-retry",
                                history=messages,
                                on_progress=on_progress,
                                _reflection_trigger=_reflection_trigger,
                                llm_config=llm_config,
                                tool_overrides=tool_overrides,
                            )
                        finally:
                            self._reflection_retry_active = False

                return response if response else "Done."

            round_num += 1

    async def summarize(
        self,
        transcript: str,
        *,
        system_prompt: str | None = None,
        instruction: str | None = None,
    ) -> str | None:
        """Summarize a conversation transcript.  Returns ``None`` on failure.

        Parameters
        ----------
        transcript:
            The text to summarize.
        system_prompt:
            Optional system prompt override.  Defaults to a generic
            summarization prompt when not provided.
        instruction:
            Optional user-message instruction that precedes the transcript.
            Defaults to a Discord-oriented summarization instruction when
            not provided.  Callers (e.g. playbook runner) can pass a
            domain-specific instruction for better summaries.
        """
        if not self._provider:
            return None
        async with self._llm_lock:
            return await self._summarize_unlocked(
                transcript,
                system_prompt=system_prompt,
                instruction=instruction,
            )

    async def _summarize_unlocked(
        self,
        transcript: str,
        *,
        system_prompt: str | None = None,
        instruction: str | None = None,
    ) -> str | None:
        """Inner summarize without lock — called by ``summarize()``."""
        # Tag logged calls with the summarize caller identity
        prev_caller = None
        if isinstance(self._provider, LoggedChatProvider):
            prev_caller = self._provider._caller
            self._provider._caller = "supervisor.summarize"

        effective_system = system_prompt or (
            "You are a helpful assistant that summarizes conversations."
        )
        effective_instruction = instruction or (
            "Summarize this Discord conversation concisely. "
            "Preserve key details: project names, task IDs, repo names, "
            "decisions made, and any pending questions or requests. "
            "Keep it factual and brief."
        )

        try:
            resp = await self._provider.create_message(
                messages=[
                    {
                        "role": "user",
                        "content": f"{effective_instruction}\n\n{transcript}",
                    }
                ],
                system=effective_system,
                max_tokens=512,
            )
            parts = resp.text_parts
            return parts[0] if parts else None
        except Exception as e:
            logger.error("Summary generation failed: %s", e)
            return None
        finally:
            if prev_caller is not None and isinstance(self._provider, LoggedChatProvider):
                self._provider._caller = prev_caller

    async def expand_rule_prompt(
        self,
        rule_content: str,
        project_id: str | None = None,
    ) -> str | None:
        """Expand a rule's natural language into a specific, actionable hook prompt.

        Makes a single LLM call (no tools) to transform vague rule intent into
        concrete operational instructions that the supervisor can execute
        reliably on each hook fire.  Returns None on failure.
        """
        if not self._provider:
            return None
        async with self._llm_lock:
            return await self._expand_rule_prompt_unlocked(rule_content, project_id)

    async def _expand_rule_prompt_unlocked(
        self,
        rule_content: str,
        project_id: str | None = None,
    ) -> str | None:
        """Inner expand_rule_prompt without lock."""
        prev_caller = None
        if isinstance(self._provider, LoggedChatProvider):
            prev_caller = self._provider._caller
            self._provider._caller = "supervisor.expand_rule"
        try:
            resp = await self._provider.create_message(
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Convert the following rule into a specific, actionable "
                            "operational prompt. This prompt will be given to an AI "
                            "supervisor agent on a recurring schedule. The agent has "
                            "access to shell commands (bash), file I/O, and task "
                            "creation tools.\n\n"
                            "Your output must be ONLY the prompt text — no "
                            "explanations, preamble, or markdown fences.\n\n"
                            "The prompt you write should:\n"
                            "1. State the objective in one sentence\n"
                            "2. List the exact shell commands to run for health/"
                            "status checks (with literal command strings)\n"
                            "3. Explain how to interpret the output of each command "
                            "(what 'healthy' vs 'unhealthy' looks like)\n"
                            "4. Specify exactly what action to take for each outcome "
                            "(including the 'everything is fine, do nothing' case)\n"
                            "5. Call out edge cases (e.g. process running but not "
                            "responding, port in use by something else)\n\n"
                            f"Rule content:\n\n{rule_content}"
                        ),
                    }
                ],
                system=(
                    "You are an expert at writing operational runbook prompts. "
                    "You produce clear, specific instructions that another AI "
                    "agent can follow without ambiguity. Prefer standard CLI "
                    "tools. Always include the 'do nothing' path so the agent "
                    "doesn't take unnecessary action."
                ),
                max_tokens=1024,
            )
            parts = resp.text_parts
            return parts[0] if parts else None
        except Exception as e:
            logger.error("Rule prompt expansion failed: %s", e)
            return None
        finally:
            if prev_caller is not None and isinstance(self._provider, LoggedChatProvider):
                self._provider._caller = prev_caller

    async def break_plan_into_tasks(
        self,
        raw_plan: str,
        parent_task_id: str,
        project_id: str,
        workspace_id: str | None = None,
        chain_dependencies: bool = True,
        requires_approval: bool = False,
        base_priority: int = 100,
        on_progress: "Callable[[str, str | None], Awaitable[None]] | None" = None,
    ) -> list[dict]:
        """Feed a plan to the supervisor LLM to break into tasks.

        Instead of algorithmically parsing plan files, this method sends
        the raw plan content to the LLM and lets it create tasks via
        ``create_task`` and ``add_dependency`` tool calls.  The LLM can
        make multiple tool calls and verify the results.

        After the LLM finishes, newly created tasks are post-processed
        to set ``parent_task_id`` and ``is_plan_subtask`` flags.

        Returns a list of dicts with ``id`` and ``title`` for each
        created task.  Never raises — returns ``[]`` on failure.
        """
        import logging

        logger = logging.getLogger(__name__)

        if not self._provider:
            logger.warning("break_plan_into_tasks: no LLM provider available")
            return []

        if project_id:
            self.set_active_project(project_id)

        # Snapshot existing task IDs so we can identify newly created ones
        existing_tasks = await self.handler.db.list_tasks(project_id=project_id)
        existing_ids = {t.id for t in existing_tasks}

        # Build the prompt for the supervisor
        dep_instructions = ""
        if chain_dependencies:
            dep_instructions = (
                "- Chain the tasks sequentially using add_dependency so each "
                "task depends on the previous one (task N+1 depends on task N). "
                "This ensures they execute in order.\n"
            )
        else:
            dep_instructions = (
                "- Use add_dependency to set dependencies between tasks based on "
                "the plan's logical ordering. If a phase builds on work from a "
                "previous phase, add a dependency so it executes after its "
                "prerequisite. Not every task needs a dependency, but tasks that "
                "depend on prior work MUST declare it.\n"
            )

        ws_instructions = ""
        if workspace_id:
            ws_instructions = (
                f'- Set preferred_workspace_id to "{workspace_id}" on every '
                f"task so they all run in the same workspace as the parent.\n"
            )

        approval_instructions = ""
        if requires_approval and chain_dependencies:
            approval_instructions = (
                "- Set requires_approval to true ONLY on the final task "
                "(so intermediate tasks don't block the chain).\n"
            )
        elif requires_approval:
            approval_instructions = "- Set requires_approval to true on every task.\n"

        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        prompt = (
            builder.render_template(
                "plan-parser-system",
                {
                    "base_priority": str(base_priority),
                    "dep_instructions": dep_instructions,
                    "ws_instructions": ws_instructions,
                    "approval_instructions": approval_instructions,
                    "parent_task_id": parent_task_id,
                    "raw_plan": raw_plan,
                },
            )
            or ""
        )

        try:
            async with self._llm_lock:
                # Tag logged calls so they're identifiable
                prev_caller = None
                if isinstance(self._provider, LoggedChatProvider):
                    prev_caller = self._provider._caller
                    self._provider._caller = "supervisor.break_plan"

                # Suppress conversation context during plan splitting — subtasks
                # should inherit the *parent's* conversation context (set in
                # post-processing below), not the plan-splitter's internal prompt.
                saved_conv_ctx = self.handler._current_conversation_context
                self.handler._current_conversation_context = None

                # Create plan subtasks directly as DEFINED so the orchestrator
                # won't schedule them before the blocking dependency on the
                # parent is established.  This eliminates the need for
                # project-wide plan processing locks.
                self.handler._plan_subtask_creation_mode = True

                try:
                    response = await self._chat_unlocked(
                        text=prompt,
                        user_name="system:plan-splitter",
                        on_progress=on_progress,
                        _reflection_trigger="plan.split",
                    )
                finally:
                    self.handler._plan_subtask_creation_mode = False

                # Restore (chat() finally-block clears it, so just ensure clean)
                self.handler._current_conversation_context = saved_conv_ctx

                if prev_caller is not None and isinstance(self._provider, LoggedChatProvider):
                    self._provider._caller = prev_caller

            logger.info(
                "break_plan_into_tasks: supervisor finished for parent %s: %s",
                parent_task_id,
                response[:200] if response else "(empty)",
            )
        except Exception as e:
            logger.error(
                "break_plan_into_tasks: supervisor chat failed for parent %s: %s",
                parent_task_id,
                e,
                exc_info=True,
            )
            self.handler._plan_subtask_creation_mode = False
            return []

        # Find newly created tasks by diffing against the snapshot
        current_tasks = await self.handler.db.list_tasks(project_id=project_id)
        new_tasks = [t for t in current_tasks if t.id not in existing_ids]

        if not new_tasks:
            logger.warning(
                "break_plan_into_tasks: supervisor created no tasks for parent %s",
                parent_task_id,
            )
            return []

        # Propagate conversation_context from the parent task to subtasks
        # so each subtask agent gets the same thread chain context.
        parent_conv_ctx = None
        try:
            parent_contexts = await self.handler.db.get_task_contexts(parent_task_id)
            parent_conv = next(
                (c for c in parent_contexts if c["type"] == "conversation_context"),
                None,
            )
            if parent_conv:
                parent_conv_ctx = parent_conv["content"]
        except Exception:
            pass  # Non-fatal

        # Post-process: set parent_task_id and is_plan_subtask on new tasks.
        # Tasks are already created as DEFINED (via _plan_subtask_creation_mode)
        # so no demotion is needed.
        created_info = []
        for task in new_tasks:
            try:
                await self.handler.db.update_task(
                    task.id,
                    parent_task_id=parent_task_id,
                    is_plan_subtask=1,
                )
                # Propagate parent conversation context to subtask
                if parent_conv_ctx:
                    await self.handler.db.add_task_context(
                        task.id,
                        type="conversation_context",
                        label="Conversation Thread Context",
                        content=parent_conv_ctx,
                    )
                created_info.append({"id": task.id, "title": task.title})
            except Exception as e:
                logger.warning(
                    "break_plan_into_tasks: failed to post-process task %s: %s",
                    task.id,
                    e,
                )

        logger.info(
            "break_plan_into_tasks: created %d tasks from plan for parent %s",
            len(created_info),
            parent_task_id,
        )
        return created_info

    async def on_task_completed(
        self,
        task_id: str,
        project_id: str,
        workspace_path: str,
    ) -> dict:
        """Handle a task.completed event.

        Called by the orchestrator's completion pipeline BEFORE merge.
        Discovers plan files, triggers reflection, and may create
        follow-up work.

        Returns a dict with "plan_found" (bool) so the orchestrator
        can transition to AWAITING_PLAN_APPROVAL if needed.

        Never raises — errors are caught, returns {"plan_found": False}.
        """
        import logging

        logger = logging.getLogger(__name__)

        async with self._llm_lock:
            return await self._on_task_completed_unlocked(
                task_id,
                project_id,
                workspace_path,
                logger,
            )

    async def _on_task_completed_unlocked(
        self,
        task_id: str,
        project_id: str,
        workspace_path: str,
        logger,
    ) -> dict:
        """Inner on_task_completed without lock."""
        try:
            if project_id:
                self.set_active_project(project_id)

            logger.info(
                "on_task_completed: processing task %s (project=%s, workspace=%s)",
                task_id,
                project_id,
                workspace_path,
            )

            result = await self.handler.execute(
                "process_task_completion",
                {
                    "task_id": task_id,
                    "workspace_path": workspace_path,
                },
            )

            # Log the result — surface errors that execute() may have wrapped
            if isinstance(result, dict) and result.get("error"):
                logger.error(
                    "on_task_completed: process_task_completion returned error for task %s: %s",
                    task_id,
                    result["error"],
                )
            elif isinstance(result, dict):
                logger.info(
                    "on_task_completed: task %s result — plan_found=%s, reason=%s",
                    task_id,
                    result.get("plan_found"),
                    result.get("reason", "n/a"),
                )
            else:
                logger.warning(
                    "on_task_completed: unexpected result type for task %s: %r",
                    task_id,
                    result,
                )

            if self._provider:
                trigger = "task.completed"
                summary = f"Task {task_id} completed"
                if isinstance(result, dict) and result.get("plan_found"):
                    summary += " — plan found, awaiting approval"

                active_tools = {t["name"]: t for t in self._registry.get_core_tools()}

                await self.reflect(
                    trigger=trigger,
                    action_summary=summary,
                    action_results=[{"tool": "process_task_completion", "result": result}],
                    messages=[],
                    active_tools=active_tools,
                )

            return result if isinstance(result, dict) else {"plan_found": False}
        except Exception as e:
            logger.error(
                "on_task_completed: unhandled exception for task %s: %s",
                task_id,
                e,
                exc_info=True,
            )
            return {"plan_found": False}

    async def observe(
        self,
        messages: list[dict],
        project_id: str,
    ) -> dict:
        """Stage 2 LLM pass for passive observation.

        Receives a batch of messages that passed the Stage 1 keyword
        filter. Makes a lightweight LLM call to decide:
        - "ignore" — nothing notable
        - "memory" — update project memory with observation
        - "suggest" — post a suggestion to the channel

        Returns a dict with "action" key and optional "content",
        "suggestion_type", "task_title" keys.

        Never raises — returns {"action": "ignore"} on any error.
        """
        if not self._provider or not messages:
            return {"action": "ignore"}

        async with self._llm_lock:
            lines = []
            for m in messages:
                author = m.get("author", "unknown")
                content = m.get("content", "")
                lines.append(f"[{author}]: {content}")
            conversation = "\n".join(lines)

            prompt = (
                f"## Passive Observation — Project: {project_id}\n\n"
                f"The following conversation happened in the project channel. "
                f"You are observing passively — do NOT take action on the project.\n\n"
                f"### Conversation\n{conversation}\n\n"
                f"### Instructions\n"
                f"Decide one of:\n"
                f'1. **ignore** — nothing notable. Respond: {{"action": "ignore"}}\n'
                f"2. **memory** — worth remembering. Respond: "
                f'{{"action": "memory", "content": "what to remember"}}\n'
                f"3. **suggest** — actionable work item. Respond: "
                f'{{"action": "suggest", "content": "suggestion text", '
                f'"suggestion_type": "task|answer|context|warning", '
                f'"task_title": "optional task title"}}\n\n'
                f"Respond with ONLY the JSON object, no other text."
            )

            try:
                resp = await self._provider.create_message(
                    messages=[{"role": "user", "content": prompt}],
                    system=(
                        "You are observing a project channel passively. "
                        "Respond with a single JSON object. No other text."
                    ),
                    max_tokens=256,
                )
                text = "\n".join(resp.text_parts).strip()
                return self._parse_observe_response(text)
            except Exception:
                return {"action": "ignore"}

    def _parse_observe_response(self, text: str) -> dict:
        """Parse the LLM's observation response into a structured dict.

        Args:
            text: Raw LLM response text (expected to be a JSON object).

        Returns:
            Parsed dict with ``action`` key, or ``{"action": "ignore"}``
            on parse failure.
        """
        import json as _json

        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(ln for ln in lines if not ln.startswith("```")).strip()
        try:
            result = _json.loads(text)
            if isinstance(result, dict) and "action" in result:
                if result["action"] in ("ignore", "memory", "suggest"):
                    return result
        except (_json.JSONDecodeError, TypeError):
            pass
        return {"action": "ignore"}

    async def _execute_tool(self, name: str, input_data: dict) -> dict:
        """Execute a tool call via the shared CommandHandler.

        Performs light pre-processing to translate LLM-friendly parameter
        aliases into the canonical names understood by CommandHandler.
        """
        if name == "list_tasks" and input_data.get("show_all"):
            # show_all is an LLM-friendly alias for include_completed.
            # Map it so CommandHandler sees the canonical parameter.
            input_data = {**input_data, "include_completed": True}
            input_data.pop("show_all", None)
        return await self.handler.execute(name, input_data)
