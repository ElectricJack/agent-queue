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
import dataclasses
import json
import logging
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

import structlog

from src.chat_providers import ChatProvider, LoggedChatProvider, create_chat_provider
from src.commands.handler import CommandHandler
from src.config import AppConfig, ChatProviderConfig
from src.llm_logger import LLMLogger
from src.models import AgentOutput, AgentResult, TaskContext
from src.orchestrator import Orchestrator
from src.runtimes.base import Capability, MessageCallback, Runtime
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

# Reflection retry guard — prevents nested reflection retries from spinning.
# Per-asyncio-task so concurrent chat() calls don't share the flag.
_reflection_retry_active_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_reflection_retry_active_var", default=False
)

# Per-call message log and tool-action list — populated by ``_chat_inner``
# during each chat round and read back by callers (PlaybookRunner's
# ``_extract_output`` reads ``supervisor._last_messages`` to extract
# structured output from tool results).  Using ContextVars instead of
# instance attributes means concurrent ``chat()`` calls get isolated
# per-asyncio-task copies — they no longer stomp each other.
_last_messages_var: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "_last_messages_var", default=None
)
_last_tool_actions_var: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "_last_tool_actions_var", default=None
)

# Per-task state for the Supervisor-as-Runtime code path.  The orchestrator
# calls Supervisor.start(task), then Supervisor.wait(), then Supervisor.stop()
# on the daemon-wide singleton.  These ContextVars carry the active
# TaskContext and per-task cancel signal so concurrent supervisor-runtime
# tasks don't race on shared instance state.
_task_var: contextvars.ContextVar[TaskContext | None] = contextvars.ContextVar(
    "_task_var", default=None
)
_cancel_var: contextvars.ContextVar["asyncio.Event | None"] = contextvars.ContextVar(
    "_cancel_var", default=None
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
SYSTEM_PROMPT_TEMPLATE = """You are Agent Q, a Discord bot that manages an AI agent task queue.
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


class Supervisor(Runtime):
    """In-process LLM supervisor — both the chat brain AND a Runtime.

    Owns the tool definitions, system prompt, LLM client, and multi-turn
    tool-use loop.  Two roles in one class:

    1. **Chat brain.** Discord/CLI/playbook-runner call ``chat()``
       (and friends like ``summarize()``, ``break_plan_into_tasks()``).
       Multi-turn conversation history flows through the caller.

    2. **Runtime** (``runtime: supervisor`` profile).  The orchestrator
       calls ``start(task) → wait() → stop()`` on the daemon-wide
       singleton — registered in :class:`RuntimeRegistry` via
       ``default_registry(supervisor=...)``.  Per-task state lives in
       module-level ContextVars (``_task_var``, ``_cancel_var``) so
       concurrent supervisor-runtime task dispatches don't race.

    Tool-call-only by design (``requires_workspace = False``): the
    supervisor never edits files on disk; the bounded tool surface
    comes from ``profile.allowed_tools``.

    Business logic is delegated to the shared CommandHandler so that
    Discord slash commands and the supervisor use the same code path.
    """

    # Runtime contract — Supervisor is registered as a singleton in the
    # RuntimeRegistry under ``name``.  The capabilities set lists what
    # supervisor-runtime tasks can rely on; "MCP" so profiles can attach
    # MCP servers, no PLAN_MODE/RESUME because supervisor doesn't run
    # subprocess sessions.
    name: ClassVar[str] = "supervisor"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.MCP, Capability.THINKING})
    requires_workspace: ClassVar[bool] = False

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
        # Reflection is part of the memory subsystem: its whole point is to
        # produce insights that land in L1/L2.  With memory paused there is
        # nowhere for a verdict to go, so force the engine to ``level="off"``
        # rather than spending tokens.  ``should_reflect()`` /
        # ``determine_depth()`` then decline at every call site.
        # See docs/specs/implementation/feature-pauses.md M5.
        _refl_cfg = config.supervisor.reflection
        if not config.memory.enabled:
            _refl_cfg = dataclasses.replace(_refl_cfg, level="off")
        self.reflection = ReflectionEngine(_refl_cfg)
        self._registry = _ToolRegistry()
        # ``_last_messages`` and ``_last_tool_actions`` are exposed via
        # properties below (backed by per-asyncio-task ContextVars).
        # Stack of cancel events — one per concurrent chat() call.
        # Using a stack instead of a single event prevents concurrent/recursive
        # chat() calls (e.g. hook LLM + user chat, or reflection retry) from
        # clobbering each other's cancel state.
        self._cancel_events: list[asyncio.Event] = []

    def initialize(self) -> bool:
        """Create LLM provider. Returns True if provider is ready.

        Supervisor-specific model/provider overrides live in the
        ``supervisor`` profile at
        ``{data_dir}/vault/agent-types/supervisor/profile.md`` — its
        ``## Config`` JSON block can set ``provider``, ``model``,
        ``max_tokens``, ``playbook_max_tokens``, and ``thinking_budget``.
        Anything not set there falls back to ``config.chat_provider``,
        which also supplies environment-specific values
        (``api_key``, ``base_url``, ``keep_alive``, ``num_ctx``).
        """
        chat_cfg = self._merge_profile_into_chat_config(self.config.chat_provider)
        # ``create_chat_provider`` may raise if provider-specific setup fails
        # eagerly (e.g. google-genai raises ``ValueError`` when its API key
        # env var is missing).  ``src/main.py:105`` treats a False return as a
        # non-fatal warning, so swallow the exception here — a raised error
        # would tear down the whole daemon just because chat isn't wired.
        try:
            provider = create_chat_provider(chat_cfg)
        except Exception as exc:
            logger.warning(
                "Supervisor.initialize: chat provider construction failed (%s: %s)",
                type(exc).__name__,
                exc,
            )
            self._provider = None
            return False
        if provider and self._llm_logger and self._llm_logger._enabled:
            provider = LoggedChatProvider(provider, self._llm_logger, caller="supervisor.chat")
        self._provider = provider
        return self._provider is not None

    def _merge_profile_into_chat_config(self, base: ChatProviderConfig) -> ChatProviderConfig:
        """Overlay the supervisor profile's Config block on top of *base*.

        Reads ``{data_dir}/vault/agent-types/supervisor/profile.md``,
        parses its ``## Config`` JSON block, and returns a new
        :class:`ChatProviderConfig` where fields set in the profile
        win over *base* (which comes from ``config.yaml``).  When the
        profile is missing, unreadable, or empty, *base* is returned
        unchanged.
        """
        try:
            from dataclasses import replace
            from pathlib import Path

            from src.profiles.parser import parse_profile
        except ImportError:
            return base

        data_dir = getattr(self.config, "data_dir", "") or os.path.expanduser("~/.agent-queue")
        profile_path = Path(data_dir) / "vault" / "agent-types" / "supervisor" / "profile.md"
        if not profile_path.is_file():
            return base

        try:
            text = profile_path.read_text(encoding="utf-8")
            parsed = parse_profile(text)
        except Exception:
            logger.debug(
                "Supervisor profile at %s could not be parsed — using config.chat_provider defaults",
                profile_path,
                exc_info=True,
            )
            return base

        if not parsed.is_valid or not parsed.config:
            return base

        # Fields the profile may set — provider-semantic only.  Things
        # like api_key and base_url are environment specific and stay
        # in config.yaml.
        #
        # ``model`` is deliberately NOT in this list.  Since the supervisor
        # became a named tmux session, ``model`` in its profile means *the
        # model the CLI is launched with* (``claude --model ...``) and is
        # necessarily a Claude model.  Feeding that to the chat provider —
        # which is Gemini here — builds a Gemini client asking for
        # ``claude-opus-5``.  One key cannot serve both paths, so the session
        # keeps ``model`` and the in-process provider takes its model from
        # ``config.chat_provider`` alone.
        overrides: dict = {}
        for key in (
            "provider",
            "max_tokens",
            "playbook_max_tokens",
            "thinking_budget",
            "num_ctx",
            "keep_alive",
        ):
            if key in parsed.config and parsed.config[key] not in ("", None):
                overrides[key] = parsed.config[key]

        if not overrides:
            return base

        logger.info(
            "Supervisor profile overriding chat_provider fields: %s",
            ", ".join(f"{k}={v!r}" for k, v in overrides.items()),
        )
        return replace(base, **overrides)

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

    # Per-call results exposed via ContextVars so concurrent ``chat()`` calls
    # don't stomp each other's transcripts.  The previous singleton attributes
    # raced under the supervisor-platform's parallel task dispatch.
    @property
    def _last_messages(self) -> list[dict]:
        msgs = _last_messages_var.get()
        return msgs if msgs is not None else []

    @_last_messages.setter
    def _last_messages(self, value: list[dict]) -> None:
        _last_messages_var.set(value)

    @property
    def _last_tool_actions(self) -> list[str]:
        actions = _last_tool_actions_var.get()
        return actions if actions is not None else []

    @_last_tool_actions.setter
    def _last_tool_actions(self, value: list[str]) -> None:
        _last_tool_actions_var.set(value)

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
            mem_svc = (
                self.orchestrator.plugin_registry.get_service("memory")
                if getattr(self.orchestrator, "plugin_registry", None) is not None
                else None
            )
            if mem_svc:
                try:
                    l1_text = await mem_svc.load_l1_facts(
                        project_id=self._active_project_id,
                        agent_type="supervisor",
                    )
                    if l1_text:
                        builder.set_l1_facts(l1_text)
                except Exception:
                    pass  # graceful degradation

                # L1 Guidance — deterministic behavioral rules.
                try:
                    l1_guid = await mem_svc.load_l1_guidance(
                        project_id=self._active_project_id,
                        agent_type="supervisor",
                    )
                    if l1_guid:
                        builder.set_l1_guidance(l1_guid)
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

        Cached: re-reads from disk only when the file's mtime changes.
        Under concurrent ``chat()`` calls this avoids hammering the
        filesystem on every ``_build_system_prompt()`` invocation while
        still picking up edits made via the vault watcher.
        """
        profile_path = os.path.join(
            self.config.data_dir, "vault", "agent-types", "supervisor", "profile.md"
        )
        try:
            return await asyncio.to_thread(self._read_supervisor_profile_cached, profile_path)
        except Exception:
            return None

    # Cache for the supervisor profile markdown.  ``(path, mtime) → text``
    # so an edit on disk invalidates the cache automatically; concurrent
    # readers share the same cached value when mtime is unchanged.
    _supervisor_profile_cache: dict[str, tuple[float, str | None]] = {}

    @classmethod
    def _read_supervisor_profile_cached(cls, path: str) -> str | None:
        """Cached file read with mtime invalidation. Runs in a thread."""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            cls._supervisor_profile_cache.pop(path, None)
            return None
        cached = cls._supervisor_profile_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        text = cls._read_file(path)
        cls._supervisor_profile_cache[path] = (mtime, text if text else None)
        return text if text else None

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
            system_prompt = await self._build_system_prompt()
            max_tokens = self.config.chat_provider.max_tokens
            reflect_resp = await self._provider.create_message(
                messages=messages,
                system=system_prompt,
                tools=list(active_tools.values()),
                max_tokens=max_tokens,
            )

            # Collect all text from reflection (including after tool use)
            reflection_text_parts = list(reflect_resp.text_parts)

            if reflect_resp.tool_uses and self.reflection.can_reflect_deeper(1):
                messages.append({"role": "assistant", "content": reflect_resp.tool_uses})
                for tool_use in reflect_resp.tool_uses:
                    # Same sandbox guard as the main chat loop — reject
                    # tool calls for names not in the active set so the
                    # reflection pass can't be coerced into invoking
                    # forbidden tools either.
                    if tool_use.name not in active_tools:
                        logger.warning(
                            "Sandbox (reflection): rejecting tool_use for %r — "
                            "not in active set (active=%s)",
                            tool_use.name,
                            sorted(active_tools.keys()),
                        )
                        result = {
                            "error": (f"Tool '{tool_use.name}' is not available in this context."),
                        }
                    else:
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

                # Call the LLM again to get the verdict now that it has
                # the tool results.  Without this, reflections that call
                # get_task/list_tasks to verify silently lose their verdict.
                followup = await self._provider.create_message(
                    messages=messages,
                    system=system_prompt,
                    tools=list(active_tools.values()),
                    max_tokens=max_tokens,
                )
                reflection_text_parts.extend(followup.text_parts)

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
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        """Process a user message with tool use. Returns response text.

        Concurrent callers (Discord messages, supervisor-platform tasks,
        hooks) run in parallel — per-call state lives in ContextVars,
        not on shared instance attributes, so no serialisation is needed.

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
            cancel_event: Optional caller-supplied cancel signal.  When
                provided, only this chat() call is interrupted by setting
                the event — sibling chats continue.  When *None*, an
                internal event is created and registered with
                ``self._cancel_events`` so ``supervisor.cancel()`` cancels
                this call along with all others.
        """
        return await self._chat_unlocked(
            text,
            user_name,
            history,
            on_progress,
            _reflection_trigger,
            llm_config=llm_config,
            tool_overrides=tool_overrides,
            context=context,
            cancel_event=cancel_event,
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
        cancel_event: asyncio.Event | None = None,
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
        # When the caller supplies their own cancel_event, we register that
        # one so they can cancel just this call independently of siblings —
        # supervisor.cancel() still cancels every chat (it iterates the list).
        if cancel_event is None:
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
                    await bus.emit(
                        "supervisor.chat.completed",
                        {
                            "project_id": self._active_project_id or "",
                            "user_text": text,
                            "response": response or "",
                            "tools_used": list(self._last_tool_actions),
                        },
                    )
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
                raise ValueError(f"Unknown tool names in tool_overrides: {sorted(unknown)}")

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

            # Pre-load the top-N individual tools most relevant to the
            # user's prompt so the LLM doesn't need to spend a turn
            # calling load_tools.  Uses the semantic ToolIndex (vector
            # similarity) when available, falls back to keyword matching.
            preloaded_categories: list[str] = []
            idx = registry.tool_index
            if idx and idx.ready:
                matches = await idx.search(text, top_k=5)
                for match in matches:
                    if match["score"] < 0.3:
                        continue
                    tool_def = registry.get_tool_definition(
                        match["name"],
                        compressed=compressed,
                    )
                    if tool_def and match["name"] not in active_tools:
                        active_tools[match["name"]] = tool_def
            else:
                # Fallback: keyword-based, but load individual tools
                # instead of entire categories.
                relevant_cats = registry.search_relevant_categories(
                    text,
                    max_categories=1,
                    min_score=0.3,
                )
                for cat_name in relevant_cats:
                    cat_tools = registry.get_category_tools(
                        cat_name,
                        compressed=compressed,
                    )
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

            # Apply max_tokens from llm_config (or config default).
            default_max = self.config.chat_provider.max_tokens
            effective_max_tokens = (
                llm_config.get("max_tokens", default_max) if llm_config else default_max
            )

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
                    messages.append({"role": "assistant", "content": response})
                    verdict = await self.reflect(
                        trigger=_reflection_trigger,
                        action_summary=", ".join(tool_actions),
                        action_results=accumulated_tool_results,
                        messages=messages,
                        active_tools=active_tools,
                        tool_names=tool_names_used,
                    )
                    if verdict and not verdict.passed and not _reflection_retry_active_var.get():
                        retry_token = _reflection_retry_active_var.set(True)
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
                            _reflection_retry_active_var.reset(retry_token)
                    return response

                # If tools were used but LLM produced empty text, nudge once
                if tool_actions and not response and nudge_count < 1:
                    nudge_count += 1
                    messages.append({"role": "assistant", "content": "(no text)"})
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
                # Defense-in-depth: providers (notably Gemini) sometimes
                # hallucinate tool_use blocks for names that were never in
                # the function-declaration schema we sent — typically when
                # the prompt mentions other tools by name.  Reject any call
                # whose name is not in the current active_tools set so a
                # sandboxed playbook (or any tool_overrides caller) can't
                # be tricked into invoking forbidden tools.
                if tool_use.name not in active_tools:
                    logger.warning(
                        "Sandbox: rejecting tool_use for %r — not in active set "
                        "(active=%s, overrides_set=%s)",
                        tool_use.name,
                        sorted(active_tools.keys()),
                        tool_overrides is not None,
                    )
                    result = {
                        "error": (
                            f"Tool '{tool_use.name}' is not available in this "
                            "context. Only call tools that appear in your "
                            "function-declaration schema."
                        ),
                    }
                else:
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
                if tool_overrides is None and tool_use.name == "load_tools" and "loaded" in result:
                    if result.get("single_tool"):
                        # Single-tool mode — inject just the one tool
                        name = result["tools_added"][0]
                        tool_def = registry.get_tool_definition(
                            name,
                            compressed=compressed,
                        )
                        if tool_def:
                            active_tools[tool_def["name"]] = tool_def
                    else:
                        # Category mode — inject exactly the tools the
                        # command reported in ``tools_added``.  That list is
                        # already filtered to tools CommandHandler can
                        # actually dispatch, so we never advertise a schema
                        # whose call would come back "Unknown command".
                        category = result["loaded"]
                        added = set(result.get("tools_added") or [])
                        cat_tools = (
                            registry.get_category_tools(
                                category,
                                compressed=compressed,
                            )
                            or []
                        )
                        for t in cat_tools:
                            if t["name"] in added:
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

                    if verdict and not verdict.passed and not _reflection_retry_active_var.get():
                        retry_token = _reflection_retry_active_var.set(True)
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
                            _reflection_retry_active_var.reset(retry_token)

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
        # Tag logged calls per-asyncio-task so concurrent Supervisor paths
        # don't stomp each other's caller label on the shared provider.
        from src.chat_providers.logged import caller_override

        token = caller_override.set("supervisor.summarize")

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
                max_tokens=self.config.chat_provider.max_tokens,
            )
            parts = resp.text_parts
            return parts[0] if parts else None
        except Exception as e:
            logger.error("Summary generation failed: %s", e)
            return None
        finally:
            caller_override.reset(token)

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
        return await self._expand_rule_prompt_unlocked(rule_content, project_id)

    async def _expand_rule_prompt_unlocked(
        self,
        rule_content: str,
        project_id: str | None = None,
    ) -> str | None:
        """Inner expand_rule_prompt without lock."""
        from src.chat_providers.logged import caller_override

        token = caller_override.set("supervisor.expand_rule")
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
                max_tokens=self.config.chat_provider.max_tokens,
            )
            parts = resp.text_parts
            return parts[0] if parts else None
        except Exception as e:
            logger.error("Rule prompt expansion failed: %s", e)
            return None
        finally:
            caller_override.reset(token)

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
            from src.chat_providers.logged import caller_override

            caller_token = caller_override.set("supervisor.break_plan")

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
                caller_override.reset(caller_token)

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

        # Post-process: set parent_task_id and is_plan_subtask on new tasks,
        # and wire the typed graph edges (work-graph design §3):
        #
        #   parent-child   — the plan task is the children's container.  It
        #                    withholds them while it is DEFINED or
        #                    AWAITING_PLAN_APPROVAL and releases them the
        #                    moment it is approved (IN_PROGRESS).  This is
        #                    exactly what the old `is_plan_subtask` special
        #                    case in _check_defined_tasks hard-coded, now
        #                    expressed as data.
        #   discovered-from — provenance: these tasks came out of that plan.
        #                    Non-blocking, so it never affects readiness.
        #
        # Tasks are already created as DEFINED (via _plan_subtask_creation_mode)
        # so no demotion is needed.
        from src.models import DepType

        created_info = []
        for task in new_tasks:
            try:
                await self.handler.db.update_task(
                    task.id,
                    parent_task_id=parent_task_id,
                    is_plan_subtask=1,
                )
                for dep_type in (
                    DepType.PARENT_CHILD.value,
                    DepType.DISCOVERED_FROM.value,
                ):
                    try:
                        await self.handler.db.add_dependency(task.id, parent_task_id, dep_type)
                    except Exception as e:
                        logger.warning(
                            "break_plan_into_tasks: failed to add %s edge %s -> %s: %s",
                            dep_type,
                            task.id,
                            parent_task_id,
                            e,
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

        State-awareness: the prompt includes ``### Active Tasks`` and
        ``### Recently Created (last 5 min)`` sections so the LLM can
        avoid suggesting work that is already in flight or that the user
        just requested.  Handler errors are swallowed — we fall back to a
        ``no active task data available`` notice rather than failing the
        whole observation (Phase 3 of the chat-analyzer overhaul plan).
        """
        if not self._provider or not messages:
            return {"action": "ignore"}

        lines = []
        for m in messages:
            author = m.get("author", "unknown")
            content = m.get("content", "")
            lines.append(f"[{author}]: {content}")
        conversation = "\n".join(lines)

        active_tasks_section, recent_tasks_section = await self._build_task_state_sections(
            project_id
        )

        prompt = (
            f"## Passive Observation — Project: {project_id}\n\n"
            f"The following conversation happened in the project channel. "
            f"You are observing passively — do NOT take action on the project.\n\n"
            f"### Conversation\n{conversation}\n\n"
            f"{active_tasks_section}\n\n"
            f"{recent_tasks_section}\n\n"
            f"### Instructions\n"
            f"Decide one of:\n"
            f'1. **ignore** — nothing notable. Respond: {{"action": "ignore"}}\n'
            f"2. **memory** — worth remembering. Respond: "
            f'{{"action": "memory", "content": "what to remember"}}\n'
            f"3. **suggest** — actionable work item. Respond: "
            f'{{"action": "suggest", "content": "suggestion text", '
            f'"suggestion_type": "task|answer|context|warning", '
            f'"task_title": "optional task title", '
            f'"intent_confidence": 0.0-1.0, '
            f'"novelty": 0.0-1.0, '
            f'"actionability": 0.0-1.0}}\n\n'
            f"**Score components (required for `suggest`).** When you "
            f"propose a suggestion, you MUST include three component "
            f"scores in `[0, 1]`:\n"
            f"  - `intent_confidence` — how sure you are this is the "
            f"user's intent (0 = guessing, 1 = explicitly asked).\n"
            f"  - `novelty` — how new this is relative to the prompt's "
            f"`### Active Tasks` and `### Recently Created` sections "
            f"(0 = literal duplicate, 1 = unrelated to anything queued).\n"
            f"  - `actionability` — how concrete and ready-to-execute the "
            f"suggestion is (0 = vague aspiration, 1 = a clearly defined "
            f"task ready to run).\n"
            f"The product of these gates whether the suggestion is shown.\n\n"
            f"**Avoid duplicative suggestions.** If your proposed `task` "
            f"or `suggest` action semantically overlaps any entry listed "
            f"under `### Active Tasks` or `### Recently Created`, you MUST "
            f'instead respond with {{"action": "ignore"}}. The user already '
            f"has that work queued or in progress; re-suggesting it is "
            f"noise.\n\n"
            f"Respond with ONLY the JSON object, no other text."
        )

        from src.chat_providers.logged import caller_override

        token = caller_override.set("supervisor.observe")
        try:
            resp = await self._provider.create_message(
                messages=[{"role": "user", "content": prompt}],
                system=(
                    "You are observing a project channel passively. "
                    "Respond with a single JSON object. No other text."
                ),
                max_tokens=1024,
            )
            text = "\n".join(resp.text_parts).strip()
            return self._parse_observe_response(text)
        except Exception:
            return {"action": "ignore"}
        finally:
            caller_override.reset(token)

    # Window for the "Recently Created" prompt section.  Five minutes is
    # the cutoff called out in the chat-analyzer overhaul plan — long
    # enough to catch the "I just asked for this seconds ago" failure
    # mode, short enough that older queued work doesn't dominate.
    _RECENT_TASK_WINDOW_SECONDS = 5 * 60

    async def _build_task_state_sections(self, project_id: str) -> tuple[str, str]:
        """Return ``(active_tasks_section, recent_tasks_section)`` for the prompt.

        Calls ``handler.execute("list_tasks", …)`` once with
        ``show_all=False`` to fetch every non-terminal task for the
        project, then partitions the result:

        * **Active Tasks** — every returned (non-terminal) task,
          rendered as ``- TITLE [STATUS]``.
        * **Recently Created (last 5 min)** — the subset whose
          ``created_at`` falls inside
          :attr:`_RECENT_TASK_WINDOW_SECONDS`.

        Any handler error degrades gracefully: both sections render the
        text ``no active task data available`` so the LLM still gets
        consistent prompt structure.
        """
        try:
            result = await self.handler.execute(
                "list_tasks",
                {"project_id": project_id, "show_all": False},
            )
            tasks = result.get("tasks", []) if isinstance(result, dict) else []
        except Exception as exc:  # pragma: no cover — narrow path, exercised by tests
            logger.warning(
                "observe(): list_tasks lookup failed for project %s: %s",
                project_id,
                exc,
            )
            fallback = "no active task data available"
            return (
                f"### Active Tasks\n{fallback}",
                f"### Recently Created (last 5 min)\n{fallback}",
            )

        if not tasks:
            return (
                "### Active Tasks\n(none)",
                "### Recently Created (last 5 min)\n(none)",
            )

        now = time.time()
        cutoff = now - self._RECENT_TASK_WINDOW_SECONDS

        active_lines: list[str] = []
        recent_lines: list[str] = []
        for t in tasks:
            title = t.get("title") or "(untitled)"
            status = t.get("status") or "?"
            active_lines.append(f"- {title} [{status}]")

            created_at = t.get("created_at")
            try:
                created_at_f = float(created_at) if created_at is not None else None
            except (TypeError, ValueError):
                created_at_f = None
            if created_at_f is not None and created_at_f >= cutoff:
                recent_lines.append(f"- {title} [{status}]")

        active_section = "### Active Tasks\n" + "\n".join(active_lines)
        recent_section = "### Recently Created (last 5 min)\n" + (
            "\n".join(recent_lines) if recent_lines else "(none)"
        )
        return active_section, recent_section

    def _parse_observe_response(self, text: str) -> dict:
        """Parse the LLM's observation response into a structured dict.

        Args:
            text: Raw LLM response text (expected to be a JSON object).

        Returns:
            Parsed dict with ``action`` key, or ``{"action": "ignore"}``
            on parse failure.

        Phase 4 — confidence scoring: every successfully parsed response
        is augmented with normalised ``intent_confidence``, ``novelty``,
        ``actionability`` (each defaulting to ``0.5`` when missing or
        non-numeric, clamped to ``[0, 1]``) plus a derived
        ``confidence = intent_confidence * novelty * actionability``.
        Downstream gates (Discord bot's confidence threshold, in-flight
        escalation) read these fields directly.
        """
        import json as _json

        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(ln for ln in lines if not ln.startswith("```")).strip()
        try:
            result = _json.loads(text)
            if isinstance(result, dict) and "action" in result:
                if result["action"] in ("ignore", "memory", "suggest"):
                    self._inject_confidence_components(result)
                    return result
        except (_json.JSONDecodeError, TypeError):
            pass
        return {"action": "ignore"}

    @staticmethod
    def _inject_confidence_components(payload: dict) -> None:
        """Mutate ``payload`` to carry normalised confidence components + product.

        For each of ``intent_confidence``, ``novelty``, ``actionability``:
          * missing or non-numeric → default ``0.5``
          * value outside ``[0, 1]`` → clamped to the nearest bound

        Then writes ``payload["confidence"]`` as the product of the
        three normalised components, also in ``[0, 1]``.
        """

        def _norm(key: str) -> float:
            raw = payload.get(key, 0.5)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = 0.5
            if value < 0.0:
                value = 0.0
            elif value > 1.0:
                value = 1.0
            payload[key] = value
            return value

        intent = _norm("intent_confidence")
        novelty = _norm("novelty")
        actionability = _norm("actionability")
        payload["confidence"] = intent * novelty * actionability

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

    # ------------------------------------------------------------------
    # Runtime contract — orchestrator dispatches profile.runtime="supervisor"
    # tasks via these methods.  Per-task state lives in ContextVars so the
    # daemon-wide singleton can run many concurrent task dispatches without
    # racing.  ``profile`` rides on TaskContext.profile (set by the
    # orchestrator) — singletons can't carry per-task profile in __init__.
    # ------------------------------------------------------------------

    async def start(self, task: TaskContext) -> None:
        """Record the task and reset cancellation for this dispatch."""
        _task_var.set(task)
        cancel = asyncio.Event()
        _cancel_var.set(cancel)
        logger.info(
            "Supervisor platform starting for task %s (profile=%s, tools=%s)",
            getattr(task, "task_id", "?"),
            task.profile.id if task.profile else "(none)",
            (task.profile.allowed_tools if task.profile else None) or "(default)",
        )

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        """Run a single ``chat()`` round for the current task and return the result."""
        from src.playbooks.token_tracker import _estimate_tokens

        task = _task_var.get()
        cancel = _cancel_var.get()
        if task is None:
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message="Supervisor.wait called before start()",
            )

        # Bridge supervisor's (event, detail) on_progress signature into the
        # MessageCallback contract (str → None) the orchestrator expects.
        async def _bridge_progress(event: str, detail: str | None) -> None:
            if not on_message:
                return
            if event == "tool_use" and detail:
                await on_message(f"-# {detail}")
            elif event == "responding":
                await on_message("-# composing response…")

        # Bound the LLM to the profile's allowed_tools when set.  None means
        # "default tool surface"; an empty list means "no tools, text-only".
        tool_overrides: list[str] | None = None
        if task.profile is not None and task.profile.allowed_tools:
            tool_overrides = list(task.profile.allowed_tools)

        user_text = task.description or ""

        try:
            response = await self.chat(
                text=user_text,
                user_name=f"task-platform:{task.task_id or 'unknown'}",
                history=None,
                on_progress=_bridge_progress,
                tool_overrides=tool_overrides,
                cancel_event=cancel,
            )
        except asyncio.CancelledError:
            return AgentOutput(
                result=AgentResult.FAILED,
                summary="Cancelled",
                error_message="Supervisor task cancelled",
            )
        except Exception as exc:
            logger.error("Supervisor platform task failed: %s", exc, exc_info=True)
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=f"Supervisor platform failed: {exc}",
            )

        if cancel is not None and cancel.is_set():
            return AgentOutput(
                result=AgentResult.FAILED,
                summary="Cancelled",
                error_message="Supervisor task was stopped",
            )

        return AgentOutput(
            result=AgentResult.COMPLETED,
            summary=response or "Completed",
            tokens_used=_estimate_tokens(user_text, response or ""),
        )

    async def stop(self) -> None:
        """Cancel only this dispatch — siblings keep running."""
        cancel = _cancel_var.get()
        if cancel is not None:
            cancel.set()

    async def is_alive(self) -> bool:
        cancel = _cancel_var.get()
        return cancel is not None and not cancel.is_set()
