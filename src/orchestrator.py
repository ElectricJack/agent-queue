"""Orchestrator — the central brain of the agent queue system.

Runs a ~5-second loop that drives the entire task lifecycle: promoting
DEFINED tasks whose dependencies are met, scheduling READY tasks onto idle
agents, launching agent execution as background asyncio tasks, managing git
workspaces (clone/link/init), parsing plan files into chained subtasks,
handling PR/approval workflows, and monitoring for stuck tasks.

Design principle: **zero LLM calls for orchestration**.  All scheduling and
state-machine logic is purely deterministic.  Every token budget goes to
actual agent work, not coordination overhead.

Heavy operations (agent execution, git clones) run as background asyncio
tasks so the main loop stays responsive and can continue checking heartbeats,
promoting tasks, and handling approvals while agents work.

Key method call hierarchy (read these to understand the full lifecycle)::

    run_one_cycle()                       # Main loop entry (~5s interval)
    ├── _check_awaiting_approval()        # Poll PR merge status
    │   ├── _handle_awaiting_no_pr()      # Auto-complete or remind
    │   └── _check_pr_status()            # Merged/closed/open detection
    ├── _resume_paused_tasks()            # Backoff timer expiry
    ├── _check_defined_tasks()            # Dependency promotion
    ├── _check_plan_parent_completion()   # Auto-complete plan parents
    ├── _check_stuck_defined_tasks()      # Monitoring alerts
    ├── _check_failed_blocked_tasks()    # Periodic failed/blocked report
    ├── _schedule()                       # Proportional fair-share assignment
    └── _execute_task_safe(action)        # Background asyncio.Task per assignment
        └── _execute_task_safe_inner()    # Timeout + crash recovery wrapper
            └── _execute_task()           # Full pipeline:
                ├── _prepare_workspace()  #   Clone + ensure clean default branch
                ├── adapter.start(ctx)    #   Launch agent process
                ├── adapter.wait()        #   Stream output + rate-limit retries
                ├── _run_completion_pipeline() # Post-completion phases:
                │   ├── _phase_plan_discover() # Plan discovery + approval
                │   └── _phase_verify()        # Verify git state, reopen if bad
                └── cleanup               #   Release workspace + free agent

Workspace locking lifecycle::

    _schedule() assigns task → _prepare_workspace() acquires lock
    → agent runs with exclusive workspace access (handles git branching,
      merging, and pushing per its prompt instructions)
    → _phase_verify() checks git state is correct
    → cleanup section releases lock via db.release_workspaces_for_task()

    If the task times out, crashes, or is admin-stopped, the lock is
    released in the error/timeout handler so the workspace isn't stuck.

Related modules:

- ``src/scheduler.py`` — Pure-function proportional fair-share scheduler.
  Called by ``_schedule()`` with a ``SchedulerState`` snapshot; returns
  ``AssignAction`` objects with zero side effects.  See that module's
  docstring for the deficit-based algorithm and min-task guarantee.

- ``src/hooks.py`` — Event-driven and periodic hook engine.  Ticked each
  cycle by ``run_one_cycle`` step 7; event hooks fire asynchronously via
  ``EventBus``.  See that module's docstring for the context pipeline,
  short-circuit checks, and LLM invocation with tool access.

- ``src/state_machine.py`` — ``VALID_TASK_TRANSITIONS`` dict defining the
  legal (status, event) → status transitions.  The orchestrator calls
  ``db.transition_task()`` which enforces these transitions.

- ``src/adapters/base.py`` / ``src/adapters/claude.py`` — Agent adapter
  interface (start/wait/stop/is_alive).  The orchestrator delegates all
  LLM interaction to adapters and only processes the resulting
  ``AgentOutput``.

See ``specs/orchestrator.md`` for the full behavioral specification.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from src.logging_config import CorrelationContext
from src.config import AppConfig, ConfigWatcher
from src.llm_logger import LLMLogger
from src.database import create_database
from src.discord.notifications import (
    format_task_started,
    format_failed_blocked_report,
    format_failed_blocked_report_embed,
)
from src.notifications.builder import build_agent_summary, build_task_detail
from src.notifications.events import (
    AgentQuestionEvent,
    BudgetWarningEvent,
    ChainStuckEvent,
    MergeConflictEvent,
    PlanAwaitingApprovalEvent,
    PRCreatedEvent,
    PushFailedEvent,
    StuckDefinedTaskEvent,
    TaskBlockedEvent,
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskMessageEvent,
    TaskStartedEvent,
    TaskStoppedEvent,
    TaskThreadCloseEvent,
    TaskThreadOpenEvent,
    TextNotifyEvent,
)
from src.event_bus import EventBus
from src.messaging.types import (
    NotifyCallback as _NotifyCallbackType,
    ThreadSendCallback as _ThreadSendCallbackType,
    CreateThreadCallback as _CreateThreadCallbackType,
)
from src.git.manager import GitError, GitManager
from src.models import (
    AgentProfile,
    AgentResult,
    AgentState,
    PhaseResult,
    PipelineContext,
    ProjectStatus,
    RepoConfig,
    RepoSourceType,
    Task,
    TaskStatus,
    TaskContext,
    TaskType,
)
from src.hooks import HookEngine
from src.plan_parser import find_plan_file, read_plan_file
from src.scheduler import AssignAction, Scheduler, SchedulerState
from src.tokens.budget import BudgetManager
from src.vault_manager import VaultManager

logger = logging.getLogger(__name__)

# Re-export callback types from the messaging abstraction layer for
# backward compatibility.  New code should import from src.messaging.types.
NotifyCallback = _NotifyCallbackType
ThreadSendCallback = _ThreadSendCallbackType
CreateThreadCallback = _CreateThreadCallbackType


class Orchestrator:
    """Coordinates the full task lifecycle across multiple projects and agents.

    The orchestrator is deliberately decoupled from any messaging transport.
    All outbound notifications are emitted as typed events on the EventBus
    via ``_emit_notify()``.  Transport handlers (e.g.
    ``DiscordNotificationHandler``) subscribe to ``notify.*`` events and
    handle formatting/delivery independently.  This makes the orchestrator
    testable in isolation and keeps the transport layer pluggable.

    Key internal state:

    * ``_running_tasks`` — maps task_id to a background ``asyncio.Task``
      for each agent execution currently in flight.  The main loop checks
      this dict every cycle to clean up finished work and avoid double-
      launching.

    * ``_adapters`` — maps agent_id to the live adapter instance (e.g.
      ``ClaudeAdapter``) so we can stop or cancel a running agent from
      admin commands like ``stop_task``.

    * ``_paused`` — when True, the scheduler is skipped entirely (no new
      work is assigned) but monitoring, approvals, and promotions continue.
    """

    def __init__(self, config: AppConfig, adapter_factory=None):
        """Initialize the orchestrator with its configuration and subsystems.

        Args:
            config: The application configuration (loaded from YAML).
            adapter_factory: Factory for creating agent adapters (e.g.
                ClaudeAdapterFactory).  When None, the orchestrator can
                manage state and scheduling but cannot execute tasks.

        The constructor wires up all subsystems but does NOT perform any
        async initialization (DB schema creation, stale-state recovery,
        hook subscriptions).  Call ``await initialize()`` before running
        cycles.
        """
        self.config = config
        self.db = create_database(config)
        self.bus = EventBus(
            env=config.env,
            validate_events=config.validate_events,
        )
        self.budget = BudgetManager(global_budget=config.global_token_budget_daily)
        self.git = GitManager()
        self._adapter_factory = adapter_factory
        # Live adapter instances keyed by agent_id.  Stored so we can call
        # adapter.stop() from admin commands (stop_task, timeout recovery).
        self._adapters: dict[str, object] = {}
        # Background asyncio Tasks for in-flight agent executions, keyed by
        # task_id.  Cleaned up each cycle; prevents double-launching.
        self._running_tasks: dict[str, asyncio.Task] = {}
        # Timestamps (time.time()) recording when each task's agent execution
        # started, keyed by task_id.  Used by _discover_and_store_plan() to
        # detect stale plan files that predate the current task's execution.
        self._task_exec_start: dict[str, float] = {}
        # Legacy callback fields — kept as no-ops for backward compatibility
        # with transports that haven't migrated to the event bus yet.
        # All orchestrator notifications now flow through _emit_notify().
        self._notify = None
        self._create_thread = None
        # Discord message objects for task-added notifications, keyed by
        # task_id.  Stored so we can delete the message when the task starts
        # to keep the chat window clean.
        self._task_added_messages: dict[str, Any] = {}
        # Discord message objects for task-started notifications, keyed by
        # task_id.  Stored so we can delete the message when the task finishes
        # to keep the chat window clean.
        self._task_started_messages: dict[str, Any] = {}
        self._paused: bool = False
        self._restart_requested: bool = False
        # Throttle: approval polling runs at most once per 60s.
        self._last_approval_check: float = 0.0
        # LLM interaction logger — records all LLM API calls (both direct
        # chat provider calls and agent sessions) to JSONL files for cost
        # analysis and prompt optimization.  See ``src/llm_logger.py``.
        self.llm_logger = LLMLogger(
            base_dir=os.path.join(config.data_dir, "logs", "llm"),
            enabled=config.llm_logging.enabled,
            retention_days=config.llm_logging.retention_days,
        )
        self._last_log_cleanup: float = 0.0
        self._last_auto_archive: float = 0.0
        self._last_memory_compact: float = 0.0
        self._last_failed_blocked_report: float = 0.0
        self._config_watcher: ConfigWatcher | None = None
        self._supervisor = None  # Set via set_supervisor() in Discord bot
        self.rule_manager = None
        # Chat provider for LLM-based plan parsing.  Optionally used by
        # ``_generate_tasks_from_plan`` to parse agent-written plan files
        # with an LLM instead of the regex parser, producing higher-quality
        # task splits.  Wrapped with ``LoggedChatProvider`` so plan-parsing
        # token usage appears in analytics under the ``plan_parser`` caller.
        self._chat_provider = None
        if config.auto_task.use_llm_parser:
            try:
                from src.chat_providers import create_chat_provider, LoggedChatProvider

                provider = create_chat_provider(config.chat_provider)
                if provider and self.llm_logger._enabled:
                    provider = LoggedChatProvider(provider, self.llm_logger, caller="plan_parser")
                self._chat_provider = provider
            except Exception:
                pass
        # Tracks the last time we sent a reminder for an AWAITING_APPROVAL
        # task that has no PR URL (keyed by task_id).
        self._no_pr_reminded_at: dict[str, float] = {}
        # Tracks the last time a "stuck DEFINED" notification was sent for
        # each task (keyed by task_id) to rate-limit alerts.
        self._stuck_notified_at: dict[str, float] = {}
        self.hooks: HookEngine | None = None
        self.vault_watcher = None
        # Semantic memory manager — optional integration with memsearch.
        # Initialized only when config.memory.enabled is True and the
        # memsearch package is installed.
        self.memory_manager: "MemoryManager | None" = None
        if hasattr(config, "memory") and config.memory.enabled:
            try:
                from src.memory import MemoryManager

                self.memory_manager = MemoryManager(config.memory, storage_root=config.data_dir)
            except Exception as e:
                logger.warning("Memory manager initialization failed: %s", e)
        # Reference to the command handler, set by the bot after initialization.
        # Used to pass handler references to interactive Discord views (e.g.
        # Retry/Skip buttons on failed task notifications).
        self._command_handler: Any = None
        # Project IDs currently undergoing plan processing (supervisor is
        # Tracks per-project budget warning thresholds already sent so we
        # don't spam the same warning.  Keyed by project_id, value is the
        # highest threshold percentage (e.g. 80, 95) already notified.
        self._budget_warned_at: dict[str, int] = {}

    def set_command_handler(self, handler: Any) -> None:
        """Store a reference to the command handler for interactive views."""
        self._command_handler = handler

    def _get_handler(self) -> Any:
        """Return the command handler or None. Used by interactive views."""
        return self._command_handler

    def set_supervisor(self, supervisor) -> None:
        """Set the Supervisor reference for post-task delegation."""
        self._supervisor = supervisor

        # Wire LLM invocation callback into the plugin registry so plugins
        # can call ctx.invoke_llm() from cron jobs and command handlers.
        if hasattr(self, "plugin_registry") and self.plugin_registry:

            async def _plugin_invoke_llm(
                prompt: str,
                plugin_name: str,
                *,
                model: str | None = None,
                provider: str | None = None,
                tools: list[dict] | None = None,
            ) -> str:
                if model or provider:
                    from src.chat_providers import create_chat_provider
                    from src.config import ChatProviderConfig

                    cfg = ChatProviderConfig(
                        provider=provider or self.config.chat_provider.provider,
                        model=model or self.config.chat_provider.model,
                    )
                    one_shot = create_chat_provider(cfg)
                    resp = await one_shot.create_message(
                        messages=[{"role": "user", "content": prompt}],
                        system=f"You are a helper for plugin:{plugin_name}.",
                    )
                    return resp.text
                # Default: use the supervisor (has tool loop)
                return await supervisor.chat(
                    prompt,
                    user_name=f"plugin:{plugin_name}",
                )

            self.plugin_registry.set_invoke_llm_callback(_plugin_invoke_llm)

        # Wire active project getter so internal plugins can resolve context
        if hasattr(self, "plugin_registry") and self.plugin_registry:
            self.plugin_registry.set_active_project_id_getter(
                lambda: supervisor.handler._active_project_id
            )
            self.plugin_registry.set_execute_command_callback(supervisor.handler.execute)

    async def _get_default_branch(self, project, workspace: str | None = None) -> str:
        """Get the default branch for a project, with dynamic detection fallback.

        First tries to use the project's configured ``repo_default_branch``.
        If not set and a workspace path is provided that contains a valid git
        checkout, attempts to detect the default branch from the repository.
        Otherwise falls back to "main" as a last resort.

        Args:
            project: The project record (may be None)
            workspace: Optional workspace path for branch detection

        Returns:
            The default branch name (e.g., "main", "master", "develop")
        """
        # Use the project's configured default branch if available
        if project and project.repo_default_branch:
            return project.repo_default_branch

        # Try to detect from the workspace if it exists and is valid
        if workspace and await self.git.avalidate_checkout(workspace):
            try:
                return await self.git.aget_default_branch(workspace)
            except Exception:
                pass  # Fall through to default

        # Last resort fallback
        return "main"

    async def _sync_profiles_from_config(self) -> None:
        """Sync agent profiles from YAML config into the database (idempotent upsert).

        Runs at startup during ``initialize()``.  For each profile defined in
        ``config.agent_profiles``, either creates a new DB row or updates the
        existing one with the latest YAML values.  This ensures the DB always
        reflects the config file as the source of truth for profile definitions.

        Profiles are referenced by ID from tasks and projects (see
        ``_resolve_profile``), so they must exist in the DB before the first
        scheduling cycle.  The upsert pattern means operators can freely edit
        profile settings in YAML and restart the daemon to apply changes.
        """
        for pc in self.config.agent_profiles:
            existing = await self.db.get_profile(pc.id)
            if existing:
                await self.db.update_profile(
                    pc.id,
                    name=pc.name,
                    description=pc.description,
                    model=pc.model,
                    permission_mode=pc.permission_mode,
                    allowed_tools=pc.allowed_tools,
                    mcp_servers=pc.mcp_servers,
                    system_prompt_suffix=pc.system_prompt_suffix,
                    install=pc.install,
                )
            else:
                await self.db.create_profile(
                    AgentProfile(
                        id=pc.id,
                        name=pc.name,
                        description=pc.description,
                        model=pc.model,
                        permission_mode=pc.permission_mode,
                        allowed_tools=pc.allowed_tools,
                        mcp_servers=pc.mcp_servers,
                        system_prompt_suffix=pc.system_prompt_suffix,
                        install=pc.install,
                    )
                )

    async def _resolve_profile(self, task: Task) -> AgentProfile | None:
        """Resolve the agent profile for a task using a three-level fallback chain.

        Resolution order (first non-None wins):
        1. **Task-level** — ``task.profile_id`` (explicit override per task)
        2. **Project-level** — ``project.default_profile_id`` (project default)
        3. **System default** — returns None, meaning the adapter uses its
           built-in defaults (no tool restrictions, no custom system prompt,
           default model).

        Profiles control: model selection, permission mode (e.g. plan-only),
        allowed tools allowlist, MCP server configuration, and a system prompt
        suffix that sets the agent's "role" for the task.

        See ``_execute_task`` where the resolved profile is passed to the
        adapter factory and injected into the agent's system context prompt.
        """
        if task.profile_id:
            return await self.db.get_profile(task.profile_id)
        project = await self.db.get_project(task.project_id)
        if project and project.default_profile_id:
            return await self.db.get_profile(project.default_profile_id)
        return None

    def pause(self) -> None:
        """Pause scheduling — no new tasks are assigned, but monitoring continues.

        When paused, ``run_one_cycle`` still runs approvals, dependency
        promotion, stuck-task detection, hooks, and archival — only the
        scheduler step (``_schedule``) is skipped.  In-flight agent work
        continues unaffected; this only prevents *new* assignments.
        """
        self._paused = True

    def resume(self) -> None:
        """Resume scheduling after a pause.  New assignments start on the next cycle."""
        self._paused = False

    # ------------------------------------------------------------------
    # Legacy callback setters (deprecated)
    # ------------------------------------------------------------------
    # These are no-ops kept for backward compatibility with transports
    # (e.g. Telegram) that still call them during startup.  All
    # notification delivery now goes through the EventBus via
    # _emit_notify().  Remove once all transports migrate to event
    # bus handlers.

    def set_notify_callback(self, callback: Any) -> None:  # noqa: ARG002
        """Deprecated — notifications now go through the EventBus."""
        self._notify = callback

    def set_create_thread_callback(self, callback: Any) -> None:  # noqa: ARG002
        """Deprecated — thread management now goes through the EventBus."""
        self._create_thread = callback

    def set_get_thread_url_callback(self, callback: Any) -> None:  # noqa: ARG002
        """Deprecated — thread URL lookup moved to DiscordNotificationHandler."""

    def set_edit_thread_root_callback(self, callback: Any) -> None:  # noqa: ARG002
        """Deprecated — thread root editing now goes through the EventBus."""

    async def skip_task(self, task_id: str) -> tuple[str | None, list[Task]]:
        """Skip a BLOCKED or FAILED task to unblock its dependency chain.

        This is an admin escape hatch: it marks the task as COMPLETED (even
        though no work was done) so that downstream dependents whose only
        remaining unmet dependency was this task can proceed.  The method
        performs a forward walk of the dependency graph to report which
        tasks will be unblocked, giving the operator visibility into the
        blast radius before the next cycle promotes them.

        Returns (error_string | None, list_of_tasks_that_will_be_unblocked).
        """
        task = await self.db.get_task(task_id)
        if not task:
            return f"Task '{task_id}' not found", []
        if task.status not in (TaskStatus.BLOCKED, TaskStatus.FAILED):
            return (
                f"Task is not BLOCKED or FAILED (status: {task.status.value}). "
                f"Only blocked/failed tasks can be skipped.",
                [],
            )

        await self.db.transition_task(
            task_id,
            TaskStatus.COMPLETED,
            context="skip_task",
        )
        await self.db.log_event(
            "task_skipped",
            project_id=task.project_id,
            task_id=task.id,
            payload=f"skipped from {task.status.value}",
        )

        # Find which downstream tasks will now become unblocked.
        # After we set this task to COMPLETED, any direct dependents
        # whose other deps are also met will be promoted by the
        # next _check_defined_tasks cycle.
        unblocked: list[Task] = []
        dependents = await self.db.get_dependents(task_id)
        for dep_id in dependents:
            dep_task = await self.db.get_task(dep_id)
            if dep_task and dep_task.status == TaskStatus.DEFINED:
                # Check if all deps (including the now-skipped one) are met
                if await self.db.are_dependencies_met(dep_id):
                    unblocked.append(dep_task)

        msg = (
            f"**Task Skipped:** `{task_id}` — {task.title}\n"
            f"Marked as COMPLETED to unblock dependency chain."
            + (
                f"\n{len(unblocked)} task(s) will be unblocked in the next cycle."
                if unblocked
                else ""
            )
        )
        await self._emit_text_notify(msg, project_id=task.project_id)

        return None, unblocked

    async def stop_task(self, task_id: str) -> str | None:
        """Forcibly stop an in-progress task and release its agent.

        Sends a stop signal to the running adapter, transitions the task to
        BLOCKED, resets the agent to IDLE, and checks whether stopping this
        task orphans any downstream dependency chain (notifying if so).

        Returns None on success, or an error string if the task cannot be stopped.
        """
        task = await self.db.get_task(task_id)
        if not task:
            return f"Task '{task_id}' not found"
        if task.status != TaskStatus.IN_PROGRESS:
            return f"Task is not in progress (status: {task.status.value})"

        # Find and stop the adapter
        agent_id = task.assigned_agent_id
        if agent_id and agent_id in self._adapters:
            adapter = self._adapters[agent_id]
            try:
                await adapter.stop()
            except Exception as e:
                logger.error("Error stopping adapter for agent %s: %s", agent_id, e)

        # Cancel the background asyncio Task so the finally block in
        # _resilient_query fires even if the transport close didn't
        # immediately take effect.  We must await the task after cancelling
        # so that any in-flight DB transaction rollback completes before we
        # issue our own DB queries — otherwise the StaticPool's single
        # aiosqlite connection can be left in a closed/corrupt state.
        bg_task = self._running_tasks.get(task_id)
        if bg_task and not bg_task.done():
            bg_task.cancel()
            # Wait for the task's cleanup (transaction rollback, etc.) to finish
            # before we issue our own DB queries.
            await asyncio.wait({bg_task}, timeout=5.0)

        # Clean up sentinel and release workspace lock
        ws = await self.db.get_workspace_for_task(task_id)
        if ws:
            self._remove_sentinel(ws.workspace_path)
        await self.db.release_workspaces_for_task(task_id)
        await self.db.transition_task(
            task_id, TaskStatus.BLOCKED, context="stop_task", assigned_agent_id=None
        )
        if agent_id:
            await self.db.update_agent(agent_id, state=AgentState.IDLE, current_task_id=None)
            self._adapters.pop(agent_id, None)

        await self._emit_task_failure(task, "stop_task", error="Manually stopped by user")
        await self._emit_notify(
            "notify.task_stopped",
            TaskStoppedEvent(
                task=build_task_detail(task),
                project_id=task.project_id,
            ),
        )
        # Delete the task-added and task-started messages to reduce chat clutter
        added_msg = self._task_added_messages.pop(task_id, None)
        if added_msg is not None:
            try:
                await added_msg.delete()
            except Exception as e:
                logger.debug("Could not delete task-added message for %s: %s", task_id, e)
        started_msg = self._task_started_messages.pop(task_id, None)
        if started_msg is not None:
            try:
                await started_msg.delete()
            except Exception as e:
                logger.debug("Could not delete task-started message for %s: %s", task_id, e)
        # Close thread and update root message
        await self._emit_notify(
            "notify.task_thread_close",
            TaskThreadCloseEvent(
                task_id=task_id,
                final_status="stopped",
                final_message=f"🛑 **Work stopped:** {task.title}",
                project_id=task.project_id,
            ),
        )
        # Check if stopping this task blocks a dependency chain
        await self._notify_stuck_chain(task)
        return None

    async def _emit_task_event(self, event_type: str, task, **extra) -> None:
        """Emit a task lifecycle event for hooks."""
        payload = {
            "task_id": task.id,
            "project_id": task.project_id,
            "title": getattr(task, "title", ""),
        }
        payload.update(extra)
        await self.bus.emit(event_type, payload)

    async def _emit_task_failure(self, task, context: str, error: str = "") -> None:
        """Emit ``task.failed`` event so hooks can react to task failures."""
        await self._emit_task_event(
            "task.failed",
            task,
            status=task.status.value if hasattr(task.status, "value") else str(task.status),
            context=context,
            error=error,
        )

    async def _emit_notify(self, event_type: str, event: Any) -> None:
        """Emit a typed notification event on the bus.

        All outbound notifications now go through the EventBus as typed
        events.  Transport handlers (Discord, WebSocket, etc.) subscribe
        to ``notify.*`` events and handle formatting/delivery.
        """
        try:
            await self.bus.emit(event_type, event.model_dump(mode="json"))
        except Exception as e:
            logger.error("Notification event emit error (%s): %s", event_type, e)

    async def _emit_text_notify(
        self,
        message: str,
        project_id: str | None = None,
    ) -> None:
        """Emit a plain-text notification event on the bus.

        Used for simple text messages that don't warrant a typed event.
        """
        await self._emit_notify(
            "notify.text",
            TextNotifyEvent(message=message, project_id=project_id),
        )

    async def _notify_agent_question(
        self,
        task: Task,
        agent: Any,
        question: str,
        project_id: str | None = None,
    ) -> None:
        """Send an agent-question notification with both text and embed.

        Called when the orchestrator detects that an agent is asking the user
        a question (e.g. via the ``AskUserQuestion`` tool).  Sends the
        notification to the project channel using both the plain-text
        formatter (for logging) and the rich embed formatter (for Discord).
        """
        from src.models import Agent

        # Ensure we have proper model objects (may receive raw DB rows)
        if not isinstance(agent, Agent):
            agent = await self.db.get_agent(getattr(agent, "id", agent))
        await self._emit_notify(
            "notify.agent_question",
            AgentQuestionEvent(
                task=build_task_detail(task),
                agent=build_agent_summary(agent),
                question=question,
                project_id=project_id or task.project_id,
            ),
        )
        await self.db.log_event(
            "agent_question",
            project_id=task.project_id,
            task_id=task.id,
            agent_id=agent.id,
            payload=question[:500],
        )

    # Budget warning thresholds — notifications are sent when project usage
    # crosses each percentage level (ascending).  Only the highest crossed
    # threshold triggers a notification; lower thresholds that were already
    # notified are not re-sent.
    #
    # NOTE: This attribute and the _check_budget_warning method below are
    # SHADOWED by a later redefinition in this class.  See
    # ``_BUDGET_THRESHOLDS`` and the second ``_check_budget_warning`` below.
    # The later definition wins at runtime; this code is currently dead.
    _BUDGET_WARNING_THRESHOLDS: list[int] = [75, 90, 95]

    async def _check_budget_warning(
        self,
        project_id: str,
        tokens_just_used: int,
    ) -> None:
        """Check whether a project's token usage has crossed a warning threshold.

        Called after recording token usage for a task.  Queries the project's
        ``budget_limit`` and current rolling-window usage, then sends a
        ``format_budget_warning_embed`` notification if the usage percentage
        has crossed one of the defined thresholds since the last notification.

        Rate-limited: each threshold level is notified at most once per
        project until the budget resets (e.g. new rolling window).
        """
        project = await self.db.get_project(project_id)
        if not project or not project.budget_limit:
            return  # No budget configured — nothing to warn about

        limit = project.budget_limit

        # Get current usage in the rolling window
        window_start = time.time() - (self.config.scheduling.rolling_window_hours * 3600)
        usage = await self.db.get_project_token_usage(
            project_id,
            since=window_start,
        )

        pct = (usage / limit * 100) if limit > 0 else 0
        if pct < self._BUDGET_WARNING_THRESHOLDS[0]:
            # Usage below the lowest threshold — clear any previous warnings
            # so they can fire again in the next budget window.
            self._budget_warned_at.pop(project_id, None)
            return

        # Find the highest threshold that has been crossed
        crossed = 0
        for threshold in self._BUDGET_WARNING_THRESHOLDS:
            if pct >= threshold:
                crossed = threshold

        if crossed == 0:
            return

        # Only notify if we haven't already notified for this threshold level
        last_warned = self._budget_warned_at.get(project_id, 0)
        if last_warned >= crossed:
            return  # Already warned at this level or higher

        self._budget_warned_at[project_id] = crossed

        project_name = project.name or project_id
        await self._emit_notify(
            "notify.budget_warning",
            BudgetWarningEvent(
                project_name=project_name,
                usage=usage,
                limit=limit,
                percentage=pct,
                project_id=project_id,
            ),
        )
        await self.db.log_event(
            "budget_warning",
            project_id=project_id,
            payload=f"usage={usage:,}/{limit:,} ({pct:.0f}%), threshold={crossed}%",
        )
        logger.info(
            "Budget warning: project %s at %.0f%% (%s/%s tokens, threshold=%d%%)",
            project_id,
            pct,
            usage,
            limit,
            crossed,
        )

    async def initialize(self) -> None:
        """Bootstrap the orchestrator and all subsystems.

        Initialization order matters — later steps depend on earlier ones:

        1. **Database** — create tables if needed, run migrations.  Must be
           first because every other step queries the DB.
        2. **Agent profiles** — sync YAML-defined profiles into DB rows so
           tasks can reference them by ID.  Depends on DB being initialized.
        3. **Stale state recovery** — after a daemon restart, no adapter
           processes are actually running, so any IN_PROGRESS tasks and BUSY
           agents left over from the previous run are reset to a schedulable
           state.  Must run before the first scheduling cycle to avoid
           ghost assignments.  See ``_recover_stale_state``.
        4. **Vault manager** — create ``VaultManager`` and ensure the vault
           directory structure exists.  Must run before plugins, hooks,
           rules, memory, or any file watchers that depend on vault paths.
        4b. **Vault watcher** — create the ``VaultWatcher`` (playbooks spec
           §17) after the vault structure exists but before subsystems that
           register handlers.  The watcher is NOT started here — it uses
           tick-driven polling via ``check()`` in ``run_one_cycle()``.
           An initial snapshot is taken at the end of ``initialize()``
           after all handlers have been registered.
        5. **Plugins** — discover and load plugin modules.
        6. **Hook engine** — subscribe to EventBus events and pre-populate
           last-run timestamps so periodic hooks don't all fire simultaneously
           on startup.  Depends on DB for reading last-run times.
        7. **Rule manager** — install default rules and start file watcher.
        8. **Config watcher** — hot-reload support for config.yaml.
        9. **Vault watcher snapshot** — take the initial filesystem snapshot
           so ``check()`` in the tick loop only detects changes after init.

        This method must be called (and awaited) before ``run_one_cycle``.
        Called by ``main.py`` during startup, after ``load_config()`` but
        before the Discord bot connects.
        """
        await self.db.initialize()
        await self._sync_profiles_from_config()
        await self._recover_stale_state()

        # Initialize VaultManager — central path resolution and directory
        # management for the vault.  Must be available before any subsystem
        # that reads vault paths (hooks, rules, memory, plugins).
        self.vault_manager = VaultManager(self.config)

        # Ensure per-project task directories exist under {data_dir}/tasks/.
        # Part of the vault migration (Phase 1) — task records live outside
        # the vault at ~/.agent-queue/tasks/{project_id}/.
        await self._ensure_task_directories()

        # Create the vault directory structure (vault spec §2).
        # Static dirs first, then per-profile and per-project subdirs.
        await self._ensure_vault_structure()

        # Create the unified vault file watcher (playbooks spec §17).
        # One watcher for the entire vault tree — specific path handlers
        # are registered by subsystems in subsequent initialization steps.
        # The watcher is NOT started here — it uses tick-driven polling
        # (like HookEngine's FileWatcher) via check() in run_one_cycle().
        # This ensures all handlers are registered before detection begins.
        from src.vault_watcher import VaultWatcher

        self.vault_watcher = VaultWatcher(
            vault_root=self.config.vault_root,
            poll_interval=5.0,
            debounce_seconds=2.0,
        )

        # Register profile.md watchers (profiles spec §3 — file → DB sync).
        # Must happen before the initial snapshot so changes are detected
        # from the first check() call.  Pass self.db so the handler can
        # upsert parsed profile fields into the agent_profiles table.
        # Pass self.bus so sync failures emit notify.profile_sync_failed.
        from src.profile_sync import register_profile_handlers

        register_profile_handlers(
            self.vault_watcher,
            db=self.db,
            event_bus=self.bus,
            data_dir=self.config.data_dir,
        )

        # Register facts.md watcher handlers (memory-plugin spec §7).
        # Detects changes to facts files across all vault scopes so they
        # can be synced to the KV backend.  Initially registered with no
        # service (log-only fallback); re-registered with the live
        # MemoryV2Service after plugins load — see post-plugin wiring below.
        from src.facts_handler import register_facts_handlers

        register_facts_handlers(self.vault_watcher)

        # Register memory/*.md watcher handlers (vault spec §5).
        # Detects changes to memory markdown files across all vault scopes
        # so they can be re-indexed into the vector DB.  Phase 1 is a
        # logging stub; actual re-indexing is wired in Phase 2/3.
        from src.memory_handler import register_memory_handlers

        register_memory_handlers(self.vault_watcher)

        # Register playbook .md watcher handlers (playbooks spec §17).
        # Detects changes to playbook files across all vault scopes so they
        # can be recompiled into executable graphs.  The PlaybookManager
        # handles compilation, versioning, and error recovery (keeping the
        # previous compiled version active on failure — roadmap 5.1.7).
        from src.playbook_handler import register_playbook_handlers
        from src.playbook_manager import PlaybookManager

        self.playbook_manager = PlaybookManager(
            chat_provider=self._chat_provider,
            event_bus=self.bus,
            data_dir=self.config.data_dir,
        )
        register_playbook_handlers(
            self.vault_watcher,
            playbook_manager=self.playbook_manager,
        )

        # Register override file watcher handlers (memory-scoping spec §5).
        # Detects changes to per-project agent-type override files so they
        # can be re-indexed into agent context.  The handler callback is
        # wired to the OverrideIndexer below (after memory collections init).
        from src.override_handler import register_override_handlers

        register_override_handlers(self.vault_watcher)

        # Register project README.md watcher handler (self-improvement spec §5).
        # Detects changes to project README files so the orchestrator can
        # update its per-project summaries.  Phase 5 is a logging stub;
        # actual summary generation is wired in Phase 6.
        from src.readme_handler import register_readme_handlers

        register_readme_handlers(self.vault_watcher)

        # Initialize plugin registry (after DB, before hooks)
        from src.plugins import PluginRegistry
        from src.plugins.services import build_internal_services

        self.plugin_registry = PluginRegistry(
            db=self.db,
            bus=self.bus,
            config=self.config,
        )

        # Build and inject services for internal plugins
        memory_mgr = getattr(self, "memory_manager", None)
        internal_services = build_internal_services(
            db=self.db,
            git=self.git,
            config=self.config,
            memory_manager=memory_mgr,
        )
        self.plugin_registry.set_internal_services(internal_services)

        try:
            discovered = await self.plugin_registry.discover_plugins()
            if discovered:
                logger.info("Discovered %d plugins: %s", len(discovered), discovered)
            loaded = await self.plugin_registry.load_all()
            if loaded:
                logger.info("Loaded %d plugins", loaded)
        except Exception as e:
            logger.error("Plugin initialization failed: %s", e, exc_info=True)

        # Wire MemoryV2Service to facts.md watcher handlers (spec §7).
        #
        # The facts handlers were registered earlier (before plugins loaded)
        # with service=None — the handler falls back to logging only.  Now
        # that the MemoryV2Plugin has been loaded and owns a live service,
        # re-register the handlers with the actual service so KV sync works.
        # register_facts_handlers() is idempotent (same handler IDs are
        # reused), so this is safe.
        self._memory_v2_service = None
        mem_v2_plugin = self.plugin_registry.get_plugin_instance("memory_v2")
        if mem_v2_plugin:
            svc = getattr(mem_v2_plugin, "service", None)
            if svc and getattr(svc, "available", False):
                from src.facts_handler import register_facts_handlers

                register_facts_handlers(self.vault_watcher, service=svc)
                self._memory_v2_service = svc
                logger.info("Wired MemoryV2Service to facts.md watcher handlers")

        # Ensure memory collections exist eagerly on startup so they're
        # available for writes and searches from the very first operation.
        # This is done after the vault structure and memory plugins are
        # initialized but before hooks/rules, since hooks may trigger
        # memory operations.
        #   - Legacy migration (roadmap 3.1.5): rename aq_{id}_memory → aq_project_{id}
        #   - aq_system (roadmap 3.1.3): cross-cutting knowledge
        #   - aq_orchestrator (roadmap 3.1.4): operational knowledge
        if self.memory_manager:
            try:
                await self.memory_manager.migrate_legacy_project_collections()
                await self.memory_manager.ensure_system_collection()
                await self.memory_manager.ensure_orchestrator_collection()
            except Exception as e:
                logger.warning("Memory collection initialization failed: %s", e)

            # Wire the override indexer into the vault watcher callback
            # (roadmap 3.2.2) so that override file changes detected by the
            # watcher trigger re-indexing into the project Milvus collection.
            try:
                wired = await self.memory_manager.setup_override_watcher()
                if wired:
                    logger.info("Override watcher wired to OverrideIndexer")
            except Exception as e:
                logger.warning("Override watcher setup failed: %s", e)

            # Index any override files that were created/modified while
            # the daemon was stopped (startup catch-up).
            try:
                vault_root = os.path.join(self.config.data_dir, "vault")
                n_chunks = await self.memory_manager.index_project_overrides(vault_root)
                if n_chunks > 0:
                    logger.info("Startup override indexing: %d chunks indexed", n_chunks)
            except Exception as e:
                logger.warning("Startup override indexing failed: %s", e)

        if self.config.hook_engine.enabled:
            self.hooks = HookEngine(self.db, self.bus, self.config)
            self.hooks.set_orchestrator(self)
            await self.hooks.initialize()

        # Initialize rule manager
        from src.rule_manager import RuleManager

        self.rule_manager = RuleManager(
            storage_root=self.config.data_dir,
            db=self.db,
            hook_engine=self.hooks if hasattr(self, "hooks") else None,
            orchestrator=self,
        )

        # Install default global rules if not already present
        # (Rule reconciliation happens later in on_ready, after the
        # supervisor is available for LLM prompt expansion.)
        try:
            installed = self.rule_manager.install_defaults()
            if installed:
                logger.info(
                    "Installed %d default global rules: %s",
                    len(installed),
                    installed,
                )
        except Exception as e:
            logger.warning("Default rule installation failed: %s", e)

        # Start rule file watcher — monitors rule directories for changes
        # and triggers per-rule reconciliation automatically.
        try:
            await self.rule_manager.start_file_watcher(self.bus)
        except Exception as e:
            logger.warning("Rule file watcher startup failed: %s", e)

        # Start config file watcher for hot-reloading
        if self.config._config_path:
            self._config_watcher = ConfigWatcher(
                config_path=self.config._config_path,
                event_bus=self.bus,
                current_config=self.config,
            )
            self.bus.subscribe("config.reloaded", self._on_config_reloaded)
            self._config_watcher.start()

        # Take the vault watcher's initial snapshot now that all subsystems
        # have had a chance to register their path handlers.  The first
        # check() call in run_one_cycle() will detect only changes that
        # occur *after* this point.
        if self.vault_watcher:
            await self.vault_watcher.check()

        # Startup scan: sync any existing profile.md files from the vault
        # to the database.  The VaultWatcher's initial check() only takes
        # a snapshot (no dispatch), so pre-existing profile files would
        # not be synced without this step.  This ensures profile DB rows
        # are always consistent with vault files after a daemon restart.
        from src.profile_sync import scan_and_sync_existing_profiles

        await scan_and_sync_existing_profiles(
            self.config.vault_root,
            self.db,
            event_bus=self.bus,
        )

    async def _recover_stale_state(self) -> None:
        """Reset any in-flight work from a previous daemon run.

        After a restart, no adapter processes are actually running, so any
        tasks marked IN_PROGRESS or agents marked BUSY are stale artifacts
        from the previous run.  This method performs three recovery actions:

        1. **Reset BUSY agents → IDLE** — so they're available for scheduling.
        2. **Release all workspace locks** — no agents hold them after restart.
        3. **Reset IN_PROGRESS tasks → READY** — so they get re-scheduled.
           Note: tasks go to READY (not BLOCKED) because the agent may have
           been interrupted at any point; a fresh retry is appropriate.

        This is intentionally aggressive: it resets *all* stale state rather
        than trying to resume interrupted work.  Agent work is designed to be
        idempotent (the agent sees the workspace as the previous agent left
        it, including any partial commits).
        """
        # Reset BUSY agents to IDLE
        agents = await self.db.list_agents()
        for a in agents:
            if a.state == AgentState.BUSY:
                logger.info("Recovery: resetting agent '%s' from %s to IDLE", a.name, a.state.value)
                await self.db.update_agent(a.id, state=AgentState.IDLE, current_task_id=None)

        # Release all workspace locks and clean orphaned sentinels.
        # After a restart no agents are running, so all DB locks are stale.
        # Also remove sentinel files from ALL workspaces — they may exist
        # even when the DB lock was already released (e.g. _prepare_workspace
        # acquired + detected sentinel + released lock, but never deleted file).
        all_workspaces = await self.db.list_workspaces()
        for ws in all_workspaces:
            if ws.locked_by_agent_id:
                logger.info(
                    "Recovery: releasing workspace lock '%s' (was locked by %s)",
                    ws.id,
                    ws.locked_by_agent_id,
                )
                await self.db.release_workspace(ws.id)
            # Always clean sentinel files on startup — no agents are running
            self._remove_sentinel(ws.workspace_path)

        # Reset IN_PROGRESS tasks back to READY so they get re-scheduled
        tasks = await self.db.list_tasks(status=TaskStatus.IN_PROGRESS)
        for t in tasks:
            logger.info(
                "Recovery: resetting task '%s' (%s) from IN_PROGRESS to READY", t.id, t.title
            )
            await self.db.transition_task(
                t.id, TaskStatus.READY, context="recovery", assigned_agent_id=None
            )

    async def _ensure_task_directories(self) -> None:
        """Create per-project task directories under ``{data_dir}/tasks/``.

        Part of vault migration Phase 1 (see ``docs/specs/design/vault.md``
        §6).  Task records live outside the vault at
        ``~/.agent-queue/tasks/{project_id}/``.

        Called during ``initialize()`` after the database is ready, so we can
        query for all existing projects and create a subdirectory for each.
        New projects also get their directory created at creation time
        (see ``CommandHandler._cmd_create_project``).
        """
        tasks_root = os.path.join(self.config.data_dir, "tasks")
        os.makedirs(tasks_root, exist_ok=True)

        all_projects = await self.db.list_projects()
        for project in all_projects:
            project_tasks_dir = os.path.join(tasks_root, project.id)
            os.makedirs(project_tasks_dir, exist_ok=True)

        if all_projects:
            logger.info(
                "Ensured task directories for %d projects under %s",
                len(all_projects),
                tasks_root,
            )

    async def _ensure_vault_structure(self) -> None:
        """Create the vault directory tree at ``{data_dir}/vault/``.

        See ``docs/specs/design/vault.md`` §2 for the full layout.

        1. Ensure the static vault directory structure exists.
        2. Check for legacy data + empty vault → auto-migrate (spec §6).
        3. Create per-profile subdirectories under ``vault/agent-types/``.

        The auto-migration logic (spec §6) only triggers when **both**
        conditions are met:

        * Legacy data exists (``notes/``, ``memory/*/rules/``, etc.)
        * The vault is empty (freshly created, no user content)

        If the vault already has content, migration is skipped to avoid
        overwriting user customizations.  This ensures a smooth transition
        for existing installs.

        All operations are idempotent — safe to call on every startup.
        Called during ``initialize()`` after the database is ready and
        ``self.vault_manager`` has been created.
        """
        from src.vault import (
            has_legacy_data,
            run_vault_migration,
            vault_has_content,
            vault_has_profile_markdown,
        )

        # Ensure the vault manager layout is created first (static dirs).
        # This must happen before checking vault_has_content so the
        # directory skeleton exists.
        self.vault_manager.ensure_layout()

        # Per-profile directories need DB access (not discoverable from FS),
        # so we handle them here.  Per-project dirs and all migrations are
        # handled by run_vault_migration using filesystem discovery.
        all_profiles = await self.db.list_profiles()

        # Collect project IDs from the database so run_vault_migration covers
        # projects that exist only in the DB (no legacy files on disk).
        all_projects = await self.db.list_projects()
        db_project_ids = [p.id for p in all_projects]

        # Auto-migration check (spec §6): only run migration when legacy
        # data exists AND the vault has no user content yet.
        data_dir = self.config.data_dir
        legacy_exists = has_legacy_data(data_dir)
        vault_populated = vault_has_content(data_dir)

        if legacy_exists and not vault_populated:
            logger.info(
                "Auto-migration triggered: legacy data detected and vault is empty — "
                "running vault migration for smooth transition"
            )
            report = run_vault_migration(data_dir, project_ids=db_project_ids)
            s = report["summary"]
            logger.info(
                "Auto-migration complete: %d moved, %d copied, %d skipped, %d errors",
                s["total_moved"],
                s["total_copied"],
                s["total_skipped"],
                s["total_errors"],
            )
        elif legacy_exists and vault_populated:
            logger.debug(
                "Vault already has content — skipping auto-migration "
                "(legacy data still present at old paths)"
            )
        else:
            logger.debug("No legacy data detected — no auto-migration needed")

        # Auto-migrate DB profiles to vault markdown (roadmap 4.2.4):
        # If DB profiles exist but no vault profile markdown files exist,
        # generate the markdown files automatically.  This is idempotent —
        # profiles that already have vault files are skipped inside the
        # migration function.  We only trigger when vault has NO profile
        # markdowns at all, to avoid interfering with user-managed content.
        if all_profiles and not vault_has_profile_markdown(data_dir):
            from src.profile_migration import migrate_db_profiles_to_vault

            logger.info(
                "Profile auto-migration triggered: %d DB profile(s) found, "
                "no vault markdown files — generating vault profile markdown",
                len(all_profiles),
            )
            try:
                profile_report = await migrate_db_profiles_to_vault(self.db, data_dir, verify=True)
                logger.info(
                    "Profile auto-migration complete: %d written, %d skipped, %d errors "
                    "(of %d total)",
                    profile_report.written,
                    profile_report.skipped,
                    profile_report.errors,
                    profile_report.total,
                )
                if profile_report.errors > 0:
                    logger.warning(
                        "Profile auto-migration had %d error(s) — "
                        "run 'migrate_profiles' command to retry",
                        profile_report.errors,
                    )
            except Exception:
                logger.exception(
                    "Profile auto-migration failed — "
                    "run 'migrate_profiles' command manually to retry"
                )
        elif all_profiles:
            logger.debug("Vault already has profile markdown — skipping profile auto-migration")

        # Per-profile directories (vault/agent-types/{profile_id}/)
        for profile in all_profiles:
            self.vault_manager.register_agent_type(profile.id)

        # Per-project directories via vault_manager (handles project
        # registration beyond what ensure_vault_project_dirs does).
        for project in all_projects:
            self.vault_manager.register_project(project.id)

        if all_profiles or all_projects:
            logger.info(
                "Vault structure ensured: %d profiles, %d projects",
                len(all_profiles),
                len(all_projects),
            )

    async def wait_for_running_tasks(self, timeout: float | None = None) -> None:
        """Wait for all background task executions to finish.

        This is primarily useful in tests where ``run_one_cycle`` fires off
        background coroutines and the caller needs to wait for them to
        complete before inspecting results.
        """
        if not self._running_tasks:
            return
        tasks = list(self._running_tasks.values())
        if timeout is not None:
            await asyncio.wait(tasks, timeout=timeout)
        else:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _format_memory_context(self, memories: list[dict]) -> str:
        """Format memsearch results as a readable context block for the agent.

        Each memory entry is rendered with its source file, section heading,
        content text, and relevance score so the agent can see where the
        information came from and how relevant it is.  The output is a single
        markdown string suitable for appending to ``TaskContext.attached_context``.

        Memory entries come from the ``MemoryManager.recall()`` call in
        ``_execute_task`` (step 4) and typically include past task results,
        project documentation, and knowledge-base entries that are
        semantically similar to the current task's description.
        """
        lines = ["## Relevant Context from Project Memory\n"]
        for i, mem in enumerate(memories, 1):
            source = mem.get("source", "unknown")
            heading = mem.get("heading", "")
            content = mem.get("content", "")
            score = mem.get("score", 0)
            lines.append(f"### Memory {i} (relevance: {score:.2f})")
            lines.append(f"*Source: {source}*")
            if heading:
                lines.append(f"*Section: {heading}*\n")
            lines.append(content)
            lines.append("")
        return "\n".join(lines)

    async def _build_memory_context_block(self, task: Any, workspace: str) -> str | None:
        """Build a tiered memory context block for agent injection.

        Uses the new ``MemoryManager.build_context()`` to produce a
        structured context with profile, notes, recent tasks, and search
        results.  Falls back to the legacy ``recall()`` + ``_format_memory_context()``
        if ``build_context()`` is not available.

        Returns the formatted context string, or ``None`` if no memory is available.
        """
        if not self.memory_manager:
            return None

        try:
            memory_ctx = await self.memory_manager.build_context(task.project_id, task, workspace)
            if not memory_ctx.is_empty:
                return memory_ctx.to_context_block()
        except Exception as e:
            logger.warning("Enhanced memory context failed for task %s: %s", task.id, e)
            # Fall back to legacy recall
            try:
                memories = await self.memory_manager.recall(task, workspace)
                if memories:
                    return self._format_memory_context(memories)
            except Exception as e2:
                logger.warning("Legacy memory recall also failed for task %s: %s", task.id, e2)

        return None

    async def shutdown(self) -> None:
        """Gracefully shut down all subsystems in dependency order.

        Shutdown order:
        1. Wait for running agent tasks (with a 10s timeout so we don't
           hang indefinitely if an adapter is stuck).
        2. Shut down the hook engine (cancels any in-flight hook tasks).
        3. Close the memory manager (flushes pending index writes).
        4. Close the database connection.

        The order matters: tasks and hooks use the DB, so we must wait
        for them to finish before closing it.
        """
        await self.wait_for_running_tasks(timeout=10)
        if self.vault_watcher:
            try:
                await self.vault_watcher.stop()
            except Exception as e:
                logger.warning("Vault watcher shutdown error: %s", e)
        if self._config_watcher:
            await self._config_watcher.stop()
        if self.rule_manager:
            try:
                await self.rule_manager.stop_file_watcher()
            except Exception as e:
                logger.warning("Rule file watcher shutdown error: %s", e)
        if self.hooks:
            await self.hooks.shutdown()
        if self.memory_manager:
            try:
                await self.memory_manager.close()
            except Exception as e:
                logger.warning("Memory manager shutdown error: %s", e)
        await self.db.close()

    async def run_one_cycle(self) -> None:
        """Run one iteration of the orchestrator's main loop.

        Called every ~5 seconds by ``main.py``'s scheduler loop.  Each cycle
        is designed to complete quickly (typically <1s of DB queries and
        state checks) — heavy work (agent execution, git operations) is
        delegated to background asyncio tasks that run concurrently.

        The cycle is organized into three phases with numbered steps:

        **Phase 1 — Promotion cascade** (steps 1-4):

        1. **Approvals** — complete tasks whose PRs were merged so
           their dependents can be promoted in the same cycle.
        2. **Resume paused** — bring back rate-limited/token-exhausted tasks
           whose backoff timers have expired.
        3. **Promote DEFINED** — check dependency satisfaction and move tasks
           to READY.  This must happen after approvals so freshly-completed
           parent tasks unblock their children immediately.
        4. **Stuck monitoring** — rate-limited alerts for DEFINED tasks that
           have been waiting too long (runs after promotion so we don't
           false-alarm on tasks that just got promoted).
        4b. **Failed/blocked report** — periodic summary of all tasks in
            FAILED or BLOCKED status, grouped by project.

        **Phase 2 — Scheduling & launch** (steps 5-6):

        5. **Schedule** — assign READY tasks to idle agents (skipped when
           the orchestrator is paused).
        6. **Launch** — fire off background asyncio tasks for each new
           assignment.  These run concurrently with future cycles.

        **Phase 3 — Housekeeping** (steps 7-11):

        7. **Hook engine tick** — process periodic hooks; event-driven hooks
           fire asynchronously via the EventBus.
        7c. **Vault watcher tick** — poll the vault directory tree for file
            changes and dispatch to registered handlers (playbooks spec §17).
            Uses the same tick-driven pattern as HookEngine's FileWatcher.
        8. **Config hot-reload** — periodically re-read non-critical settings
           from disk (scheduling, archive, monitoring, etc.) without restart.
        9. **Log cleanup** — prune old LLM interaction logs and flush
           prompt analytics for long-term cost analysis.
        10. **Auto-archive** — sweep terminal tasks older than the configured
            threshold into the archive so they no longer clutter active views.

        Ordering invariant: steps 1-3 form a "promotion cascade" where
        completing an approval can immediately unblock a DEFINED task in
        the same cycle.  Breaking this order would add a 5s delay to
        dependency chain progression.

        Error handling: the entire cycle is wrapped in a try/except so
        that a failure in one step (e.g., a DB query error) doesn't crash
        the daemon — it logs the error and retries on the next cycle.
        """
        try:
            # ── Phase 1: Promotion cascade ──────────────────────────────────
            # Steps 1-3 form a "promotion cascade": completing an approval can
            # immediately unblock a DEFINED task in the same cycle.  Breaking
            # this order would add a ~5s delay to dependency chain progression.

            # 1. Poll PR merge/close status for AWAITING_APPROVAL tasks.
            #    Merged PRs → COMPLETED, which may satisfy downstream deps.
            await self._check_awaiting_approval()

            # 2. Promote PAUSED tasks whose backoff timer has expired → READY.
            await self._resume_paused_tasks()

            # 3. Promote DEFINED/BLOCKED tasks whose dependencies are met → READY.
            #    Runs after step 1 so freshly-completed approvals can unblock
            #    dependents within the same cycle.
            await self._check_defined_tasks()

            # 3b. Auto-complete plan parents whose subtasks are all done.
            #     Runs after step 1 (which may complete the last subtask via
            #     PR merge) so plan parents can complete in the same cycle.
            await self._check_plan_parent_completion()

            # 4. Monitoring: detect DEFINED tasks stuck beyond threshold.
            #    Runs after promotion so we don't false-alarm on tasks that
            #    were just promoted in step 3.
            await self._check_stuck_defined_tasks()

            # 4b. Periodic report of all FAILED/BLOCKED tasks so operators
            #     have an at-a-glance view of tasks needing intervention.
            await self._check_failed_blocked_tasks()

            # ── Phase 2: Scheduling & launch ────────────────────────────────

            # 5. Schedule READY tasks onto idle agents (skipped when paused).
            if not self._paused:
                actions = await self._schedule()
            else:
                actions = []

            # 6. Launch assigned tasks as background asyncio coroutines.
            #
            # First, reap completed background tasks from _running_tasks.
            # We must do this BEFORE launching new tasks to free up task IDs
            # (a task that just finished shouldn't block its re-assignment
            # on a retry).  We don't inspect results here — error handling
            # is done inside _execute_task_safe_inner.
            done = [tid for tid, t in self._running_tasks.items() if t.done()]
            for tid in done:
                self._running_tasks.pop(tid)

            for action in actions:
                if action.task_id in self._running_tasks:
                    continue  # Already running — skip double-launch
                bg = asyncio.create_task(self._execute_task_safe(action))
                self._running_tasks[action.task_id] = bg

            # ── Phase 3: Housekeeping ───────────────────────────────────────

            # 7. Run hook engine tick (periodic hooks; event hooks fire async).
            if self.hooks:
                await self.hooks.tick()

            # 7b. Run plugin cron jobs (plain async functions, not LLM prompts).
            if hasattr(self, "plugin_registry") and self.plugin_registry:
                await self.plugin_registry.tick_cron()

            # 7c. Poll vault watcher for filesystem changes (playbooks spec §17).
            # Uses tick-driven polling (like HookEngine's FileWatcher) rather
            # than a background loop — check() has built-in rate limiting via
            # poll_interval and debounce, so calling it each cycle is cheap.
            if self.vault_watcher:
                try:
                    await self.vault_watcher.check()
                except Exception as e:
                    logger.warning("VaultWatcher check failed: %s", e)

            # 8. Config hot-reload is handled by ConfigWatcher (background task).

            # 9. Periodic log cleanup and analytics flush (~once per hour).
            now = time.time()
            if now - self._last_log_cleanup >= 3600:
                self._last_log_cleanup = now
                try:
                    removed = self.llm_logger.cleanup_old_logs()
                    if removed:
                        logger.info("LLM log cleanup: removed %d old directory(ies)", removed)
                    # Flush prompt analytics to disk for long-term analysis
                    self.llm_logger.flush_analytics()
                except Exception as e:
                    logger.error("LLM log cleanup error: %s", e)

            # 10. Auto-archive stale terminal tasks (~once per hour).
            await self._auto_archive_tasks()

            # 11. Periodic memory compaction — age out old task memories
            # into weekly digests.  Rate-limited by compact_interval_hours.
            await self._periodic_memory_compact()
        except Exception:
            logger.error("Scheduler cycle error", exc_info=True)

    async def _execute_task_safe(self, action: AssignAction) -> None:
        """Top-level wrapper for background task execution (layer 1 of 3).

        The task execution pipeline is wrapped in three layers, each adding
        a specific concern:

        Layer 1 (this method): **Correlation context** — sets task_id and
            project_id on the logging contextvar so every log line emitted
            during this task's execution can be filtered and traced.

        Layer 2 (_execute_task_safe_inner): **Timeout + crash recovery** —
            enforces ``stuck_timeout_seconds`` via ``asyncio.wait_for``
            and catches unexpected exceptions to reset state cleanly.

        Layer 3 (_execute_task): **Business logic** — the actual pipeline:
            workspace setup → agent launch → output streaming → result
            handling → cleanup.

        This is the coroutine stored in ``_running_tasks[task_id]``.
        """
        with CorrelationContext(
            task_id=action.task_id,
            project_id=action.project_id,
            component="orchestrator",
        ):
            await self._execute_task_safe_inner(action)

    async def _execute_task_safe_inner(self, action: AssignAction) -> None:
        """Timeout enforcement and crash recovery around ``_execute_task`` (layer 2 of 3).

        Wraps the real execution pipeline with ``asyncio.wait_for`` so that
        stuck agents are forcibly stopped after ``stuck_timeout_seconds``.

        On timeout:
          - The adapter is stopped, workspace lock released, task → BLOCKED,
            agent → IDLE, and a downstream-chain-stuck check is performed.
          - The task goes to BLOCKED (not READY) because a timeout usually
            indicates a systemic issue that won't resolve on auto-retry.

        On unexpected exception (orchestrator bug, DB error, etc.):
          - Task → READY (so it can be retried), agent → IDLE, workspace
            released.  The task is *not* counted as a retry because the
            failure was in orchestrator logic, not agent logic.
          - Task goes to READY (not BLOCKED) because the agent never ran,
            so the issue may be transient.

        The ``finally`` block always removes the task from ``_running_tasks``
        to prevent stale entries from blocking future scheduling rounds.
        """
        timeout = self.config.agents_config.stuck_timeout_seconds
        try:
            if timeout > 0:
                await asyncio.wait_for(self._execute_task(action), timeout=timeout)
            else:
                await self._execute_task(action)
        except asyncio.TimeoutError:
            logger.warning("Task %s timed out after %ds", action.task_id, timeout)
            # Stop the adapter if it's still running
            if action.agent_id in self._adapters:
                try:
                    await self._adapters[action.agent_id].stop()
                except Exception:
                    pass
            # Clean up sentinel before releasing workspace lock
            ws = await self.db.get_workspace_for_task(action.task_id)
            if ws:
                self._remove_sentinel(ws.workspace_path)
            await self.db.release_workspaces_for_task(action.task_id)
            await self.db.transition_task(
                action.task_id, TaskStatus.BLOCKED, context="timeout", assigned_agent_id=None
            )
            await self.db.update_agent(action.agent_id, state=AgentState.IDLE, current_task_id=None)
            self._adapters.pop(action.agent_id, None)
            task = await self.db.get_task(action.task_id)
            if task:
                await self._emit_task_failure(
                    task, "timeout", error=f"Task execution timed out after {timeout}s"
                )
            await self._emit_text_notify(
                f"**Task Timed Out:** `{action.task_id}` — exceeded {timeout}s. Marked as BLOCKED.",
                project_id=action.project_id,
            )
            # Check if this blocked task breaks a dependency chain
            task = await self.db.get_task(action.task_id)
            if task:
                await self._notify_stuck_chain(task)
            return
        except Exception as e:
            logger.error("Error executing task %s", action.task_id, exc_info=True)
            try:
                # Clean up sentinel before releasing workspace lock
                ws = await self.db.get_workspace_for_task(action.task_id)
                if ws:
                    self._remove_sentinel(ws.workspace_path)
                await self.db.release_workspaces_for_task(action.task_id)
                await self.db.transition_task(
                    action.task_id,
                    TaskStatus.READY,
                    context="execution_error",
                    assigned_agent_id=None,
                )
                await self.db.update_agent(
                    action.agent_id, state=AgentState.IDLE, current_task_id=None
                )
            except Exception:
                pass
            await self._emit_text_notify(
                f"**Error executing task** `{action.task_id}`: {e}",
                project_id=action.project_id,
            )
        finally:
            self._running_tasks.pop(action.task_id, None)

    async def _on_config_reloaded(self, data: dict) -> None:
        """Handle config.reloaded events from the ConfigWatcher.

        Updates the orchestrator's config reference and propagates changes
        to subsystems that cache config values (hook engine, budget manager).
        """
        config = data.get("config")
        if config is None:
            return
        self.config = config
        # Update hook engine config reference
        if self.hooks:
            self.hooks.config = config
        # Update budget manager if global budget changed
        if self.budget and config.global_token_budget_daily is not None:
            self.budget._global_budget = config.global_token_budget_daily
        logger.info(
            "Config reloaded: updated sections: %s",
            ", ".join(data.get("changed_sections", [])),
        )

    async def _resume_paused_tasks(self) -> None:
        """Check PAUSED tasks whose ``resume_after`` has elapsed and promote to READY.

        Tasks enter PAUSED when the agent hits a rate limit or token
        exhaustion, with ``resume_after`` set to a future timestamp.
        This method scans all PAUSED tasks and transitions any whose
        backoff timer has expired back to READY for re-scheduling.
        """
        paused = await self.db.list_tasks(status=TaskStatus.PAUSED)
        now = time.time()
        for task in paused:
            if task.resume_after and task.resume_after <= now:
                await self.db.transition_task(
                    task.id,
                    TaskStatus.READY,
                    context="resume_paused",
                    assigned_agent_id=None,
                    resume_after=None,
                )

    async def _check_defined_tasks(self) -> None:
        """Promote DEFINED/BLOCKED tasks to READY when all dependencies are satisfied.

        Scans all DEFINED tasks and checks their dependency list:
        - Tasks with no dependencies are immediately promoted to READY.
        - Tasks with dependencies are promoted only when every upstream
          dependency has reached COMPLETED status.

        Also scans BLOCKED tasks that have dependencies — if all deps are now
        COMPLETED, the task is promoted to READY (e.g. a task that was blocked
        on a dependency chain and the upstream has since completed).

        Special handling for plan subtasks:
        - Skipped if the parent plan is still in AWAITING_PLAN_APPROVAL.
        - If the parent plan is IN_PROGRESS (approved, subtasks running),
          the parent dependency is treated as met — only non-parent
          dependencies must be COMPLETED.

        This runs after ``_check_awaiting_approval`` so that freshly-merged
        PRs can unblock their dependents in the same cycle.
        """
        defined = await self.db.list_tasks(status=TaskStatus.DEFINED)
        # Also check BLOCKED tasks — their dependencies may have been
        # satisfied since they were blocked, allowing them to proceed.
        blocked = await self.db.list_tasks(status=TaskStatus.BLOCKED)
        for task in [*defined, *blocked]:
            # Plan subtask special handling: the parent plan transitions to
            # IN_PROGRESS (not COMPLETED) when approved, so standard
            # are_dependencies_met() would block forever.  We treat the
            # IN_PROGRESS parent dep as satisfied.
            if task.is_plan_subtask and task.parent_task_id:
                parent = await self.db.get_task(task.parent_task_id)
                if parent and parent.status == TaskStatus.AWAITING_PLAN_APPROVAL:
                    continue
                if parent and parent.status == TaskStatus.IN_PROGRESS:
                    # Parent plan is approved and active — treat parent dep as met.
                    # Check only non-parent dependencies.
                    deps = await self.db.get_dependencies(task.id)
                    non_parent_deps = deps - {task.parent_task_id}
                    if not non_parent_deps:
                        await self.db.transition_task(
                            task.id, TaskStatus.READY, context="deps_met_plan_parent_active"
                        )
                    else:
                        # All non-parent deps must be COMPLETED
                        all_met = True
                        for dep_id in non_parent_deps:
                            dep_task = await self.db.get_task(dep_id)
                            if not dep_task or dep_task.status != TaskStatus.COMPLETED:
                                all_met = False
                                break
                        if all_met:
                            await self.db.transition_task(
                                task.id, TaskStatus.READY, context="deps_met_plan_parent_active"
                            )
                    continue

            deps = await self.db.get_dependencies(task.id)
            if not deps:
                if task.status == TaskStatus.DEFINED:
                    # No dependencies — promote DEFINED to READY.
                    # (BLOCKED tasks with no deps stay blocked — they were
                    # blocked for other reasons like verification failure.)
                    await self.db.transition_task(
                        task.id, TaskStatus.READY, context="deps_met_no_deps"
                    )
            else:
                deps_met = await self.db.are_dependencies_met(task.id)
                if deps_met:
                    await self.db.transition_task(task.id, TaskStatus.READY, context="deps_met")

    async def _check_plan_parent_completion(self) -> None:
        """Auto-complete plan parent tasks when all their subtasks are done.

        When a plan is approved, the parent transitions to IN_PROGRESS (not
        COMPLETED) so its status accurately reflects that work is still in
        progress.  This method checks all IN_PROGRESS tasks that have subtasks
        and transitions them to COMPLETED once every subtask has finished.

        Runs every cycle to catch all completion paths (agent completion,
        PR merge, admin skip, etc.) without needing hooks in each path.
        """
        in_progress = await self.db.list_tasks(status=TaskStatus.IN_PROGRESS)
        for task in in_progress:
            subtasks = await self.db.get_subtasks(task.id)
            if not subtasks:
                continue  # Not a plan parent — skip
            if all(s.status == TaskStatus.COMPLETED for s in subtasks):
                await self.db.transition_task(
                    task.id, TaskStatus.COMPLETED, context="subtasks_completed"
                )
                await self.db.log_event(
                    "plan_completed",
                    project_id=task.project_id,
                    task_id=task.id,
                    payload=f"All {len(subtasks)} subtask(s) completed",
                )
                await self._emit_text_notify(
                    f"**Plan Completed:** `{task.id}` — {task.title} "
                    f"(all {len(subtasks)} subtask(s) finished).",
                    project_id=task.project_id,
                )
                logger.info(
                    "Plan parent %s auto-completed: all %d subtasks finished",
                    task.id,
                    len(subtasks),
                )

    async def _check_stuck_defined_tasks(self) -> None:
        """Monitoring: detect DEFINED tasks stuck waiting for dependencies.

        Queries for tasks that have been in DEFINED status longer than
        ``monitoring.stuck_task_threshold_seconds`` and sends a notification
        with details about which upstream dependencies are blocking them.

        Notifications are rate-limited to one per threshold period per task
        (tracked in ``_stuck_notified_at``) to avoid flooding Discord.
        The tracker is garbage-collected each cycle to remove entries for
        tasks that are no longer stuck.
        """
        threshold = self.config.monitoring.stuck_task_threshold_seconds
        if threshold <= 0:
            return  # Disabled

        stuck_tasks = await self.db.get_stuck_defined_tasks(threshold)
        if not stuck_tasks:
            return

        now = time.time()

        # Clean up notification tracker for tasks no longer DEFINED
        stuck_ids = {t.id for t in stuck_tasks}
        for tid in list(self._stuck_notified_at):
            if tid not in stuck_ids:
                del self._stuck_notified_at[tid]

        for task in stuck_tasks:
            # Rate-limit: only notify once per threshold period per task
            last_notified = self._stuck_notified_at.get(task.id, 0)
            if now - last_notified < threshold:
                continue

            # Find which dependencies are blocking this task
            blocking = await self.db.get_blocking_dependencies(task.id)

            # Calculate how long the task has been stuck
            task_created_at = await self.db.get_task_created_at(task.id)
            if not task_created_at:
                task_created_at = now  # fallback (should not happen)
            stuck_hours = (now - task_created_at) / 3600

            await self._emit_notify(
                "notify.stuck_defined_task",
                StuckDefinedTaskEvent(
                    task=build_task_detail(task),
                    blocking_deps=[
                        {"id": dep_id, "title": dep_title, "status": dep_status}
                        for dep_id, dep_title, dep_status in blocking
                    ],
                    stuck_hours=stuck_hours,
                    project_id=task.project_id,
                ),
            )

            # Log the event
            blocking_info = ", ".join(
                f"{dep_id}({dep_status})" for dep_id, _, dep_status in blocking[:10]
            )
            await self.db.log_event(
                "stuck_defined_task",
                project_id=task.project_id,
                task_id=task.id,
                payload=f"stuck_hours={stuck_hours:.1f}, blocking=[{blocking_info}]",
            )
            logger.info(
                "Stuck task detected: %s — %s (DEFINED for %.1fh, blocked by %d deps)",
                task.id,
                task.title,
                stuck_hours,
                len(blocking),
            )

            self._stuck_notified_at[task.id] = now

    async def _check_failed_blocked_tasks(self) -> None:
        """Periodic report: summarize all FAILED and BLOCKED tasks to the channel.

        Queries for tasks currently in FAILED or BLOCKED status and posts a
        consolidated summary to the notification channel so operators have an
        at-a-glance view of everything needing manual intervention.

        Rate-limited by ``monitoring.failed_blocked_report_interval_seconds``
        (default 1 hour).  Set to 0 or negative to disable.  The report is
        only sent when at least one task is in FAILED or BLOCKED status.
        """
        interval = self.config.monitoring.failed_blocked_report_interval_seconds
        if interval <= 0:
            return  # Disabled

        now = time.time()
        if now - self._last_failed_blocked_report < interval:
            return

        self._last_failed_blocked_report = now

        failed_tasks = await self.db.list_tasks(status=TaskStatus.FAILED)
        blocked_tasks = await self.db.list_tasks(status=TaskStatus.BLOCKED)

        if not failed_tasks and not blocked_tasks:
            return

        total = len(failed_tasks) + len(blocked_tasks)
        logger.info(
            "Failed/blocked report: %d failed, %d blocked (%d total)",
            len(failed_tasks),
            len(blocked_tasks),
            total,
        )

        # Group tasks by project so we can notify each project's channel
        projects: dict[str, tuple[list, list]] = {}
        for t in failed_tasks:
            projects.setdefault(t.project_id, ([], []))[0].append(t)
        for t in blocked_tasks:
            projects.setdefault(t.project_id, ([], []))[1].append(t)

        for project_id, (proj_failed, proj_blocked) in projects.items():
            msg = format_failed_blocked_report(proj_failed, proj_blocked)
            embed = format_failed_blocked_report_embed(proj_failed, proj_blocked)
            await self._emit_text_notify(msg, project_id=project_id)

    async def _auto_archive_tasks(self) -> None:
        """Automatically archive terminal tasks older than the configured threshold.

        Runs at most once per hour (rate-limited via ``_last_auto_archive``)
        and only when ``config.archive.enabled`` is True.  Tasks matching the
        configured terminal statuses whose ``updated_at`` is older than
        ``archive.after_hours`` are silently moved to the ``archived_tasks``
        table so they no longer appear in active views.

        This eliminates the need for agents or operators to manually run
        ``/archive-tasks``; the orchestrator handles it automatically.
        """
        archive_cfg = self.config.archive
        if not archive_cfg.enabled:
            return

        now = time.time()
        # Rate-limit to once per hour
        if now - self._last_auto_archive < 3600:
            return
        self._last_auto_archive = now

        older_than_seconds = archive_cfg.after_hours * 3600
        try:
            archived_ids = await self.db.archive_old_terminal_tasks(
                statuses=archive_cfg.statuses,
                older_than_seconds=older_than_seconds,
            )
        except Exception as e:
            logger.error("Auto-archive error: %s", e)
            return

        if archived_ids:
            logger.info(
                "Auto-archived %d terminal task(s) older than %.1fh: %s%s",
                len(archived_ids),
                archive_cfg.after_hours,
                ", ".join(archived_ids[:10]),
                "..." if len(archived_ids) > 10 else "",
            )
            for tid in archived_ids:
                try:
                    await self.db.log_event(
                        "task_auto_archived",
                        task_id=tid,
                    )
                except Exception:
                    pass

    async def _periodic_memory_compact(self) -> None:
        """Periodically compact old task memories into weekly digests.

        Runs at most once per ``compact_interval_hours`` (rate-limited via
        ``_last_memory_compact``).  Only active when ``compact_enabled`` is
        True and the memory manager is initialized.

        Iterates all known projects and runs compaction for each.  Errors
        are logged but never block the orchestrator cycle.
        """
        if not self.memory_manager:
            return
        if not self.config.memory.compact_enabled:
            return

        now = time.time()
        interval_seconds = self.config.memory.compact_interval_hours * 3600
        if now - self._last_memory_compact < interval_seconds:
            return
        self._last_memory_compact = now

        try:
            projects = await self.db.get_all_projects()
        except Exception as e:
            logger.error("Memory compaction: failed to list projects: %s", e)
            return

        for project in projects:
            try:
                workspace = await self.db.get_project_workspace_path(project.id)
                if not workspace:
                    continue
                result = await self.memory_manager.compact(project.id, workspace)
                if result.get("digests_created", 0) > 0 or result.get("files_removed", 0) > 0:
                    logger.info(
                        "Memory compaction for %s: %d digests created, %d files removed",
                        project.id,
                        result.get("digests_created", 0),
                        result.get("files_removed", 0),
                    )
            except Exception as e:
                logger.warning("Memory compaction failed for project %s: %s", project.id, e)

    async def _find_stuck_downstream(self, blocked_task_id: str) -> list[Task]:
        """BFS walk of the dependency graph to find orphaned DEFINED tasks.

        Starting from a BLOCKED task, walks forward through ``get_dependents``
        and collects every downstream task still in DEFINED status.  These
        tasks can never proceed because their dependency chain is broken.

        Only DEFINED tasks are collected — tasks that have already been
        promoted past the dependency gate (READY, IN_PROGRESS, etc.) are
        not affected by the upstream blockage.

        Used by ``_notify_stuck_chain`` to give operators visibility into
        the full blast radius when a task fails or is stopped.
        """
        stuck: list[Task] = []
        visited: set[str] = set()
        queue: list[str] = [blocked_task_id]

        while queue:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            dependents = await self.db.get_dependents(current_id)
            for dep_id in dependents:
                if dep_id in visited:
                    continue
                task = await self.db.get_task(dep_id)
                if not task:
                    continue
                # Only DEFINED tasks are "stuck" — tasks in other states
                # (READY, IN_PROGRESS, etc.) have already moved past the
                # dependency gate.
                if task.status == TaskStatus.DEFINED:
                    stuck.append(task)
                    # Continue walking: this stuck task may itself have
                    # downstream dependents.
                    queue.append(dep_id)

        return stuck

    async def _notify_stuck_chain(self, blocked_task: Task) -> None:
        """Check for downstream stuck tasks and send a notification.

        Uses ``_find_stuck_downstream`` to do a BFS walk of the dependency
        graph.  If any DEFINED tasks are found that are transitively blocked
        by this task, sends a single consolidated notification listing all
        affected downstream tasks so operators can decide whether to skip,
        retry, or manually unblock the chain.
        """
        stuck = await self._find_stuck_downstream(blocked_task.id)
        if not stuck:
            return

        await self._emit_notify(
            "notify.chain_stuck",
            ChainStuckEvent(
                blocked_task=build_task_detail(blocked_task),
                stuck_task_ids=[t.id for t in stuck],
                stuck_task_titles=[t.title for t in stuck],
                project_id=blocked_task.project_id,
            ),
        )
        await self.db.log_event(
            "chain_stuck",
            project_id=blocked_task.project_id,
            task_id=blocked_task.id,
            payload=f"stuck_count={len(stuck)}, stuck_ids={[t.id for t in stuck[:20]]}",
        )

    # Budget warning thresholds — notify once per threshold crossing.
    #
    # IMPORTANT: This class attribute and the ``_check_budget_warning`` method
    # below intentionally SHADOW the earlier definitions (``_BUDGET_WARNING_THRESHOLDS``
    # and the first ``_check_budget_warning`` at line ~469).  Python resolves
    # method lookups top-down within the class body, so the LAST definition
    # wins at runtime.  This version uses cumulative token usage (simpler)
    # instead of rolling-window-scoped usage.
    #
    # TODO: consolidate the two implementations into one.  The shadowed version
    # (earlier in this file) is dead code at runtime.
    _BUDGET_THRESHOLDS: list[int] = [80, 95]

    async def _check_budget_warning(
        self,
        project_id: str,
        tokens_added: int,
    ) -> None:
        """Send a budget warning if a project crosses a spending threshold.

        Called after recording token usage for a completed task.  Queries
        the project's cumulative token usage and ``budget_limit``, then
        checks whether usage has crossed any of the ``_BUDGET_THRESHOLDS``
        percentage levels.  Each threshold (80%, 95%) fires at most once
        per project; the ``_budget_warned_at`` dict tracks the highest
        threshold already notified to avoid duplicate alerts.

        Note: this method shadows an earlier definition that uses rolling-
        window scoped usage.  The shadowed version is unreachable at runtime.
        """
        project = await self.db.get_project(project_id)
        if not project or project.budget_limit is None or project.budget_limit <= 0:
            return

        usage = await self.db.get_project_token_usage(project_id)
        pct = usage / project.budget_limit * 100

        prev_threshold = self._budget_warned_at.get(project_id, 0)

        for threshold in self._BUDGET_THRESHOLDS:
            if pct >= threshold > prev_threshold:
                await self._emit_notify(
                    "notify.budget_warning",
                    BudgetWarningEvent(
                        project_name=project.name,
                        usage=usage,
                        limit=project.budget_limit,
                        percentage=pct,
                        project_id=project_id,
                    ),
                )
                await self.db.log_event(
                    "budget_warning",
                    project_id=project_id,
                    payload=f"threshold={threshold}%, usage={usage:,}/{project.budget_limit:,}",
                )
                self._budget_warned_at[project_id] = threshold

    async def _schedule(self) -> list[AssignAction]:
        """Build scheduler state snapshot and compute task-to-agent assignments.

        Gathers the current state of all projects, tasks, agents, token
        usage within the rolling window, and per-project workspace
        availability.  Passes this snapshot to the proportional fair-share
        scheduler (``Scheduler.schedule()``) which returns a list of
        ``AssignAction`` objects mapping tasks to agents.

        The scheduler is a **pure function** — it takes a ``SchedulerState``
        snapshot and returns actions with zero side effects.  This method's
        job is to build that snapshot from the database.

        Snapshot consistency: each DB query runs independently (no
        transaction wrapping the whole snapshot).  This is acceptable
        because the ~5s cycle frequency means inter-query drift is minimal,
        and the scheduler is self-correcting — any imbalance caused by a
        slightly stale snapshot will be corrected in the next cycle.

        The scheduler never makes LLM calls — it uses credit weights,
        deficit accounting, and workspace availability to decide which
        project's READY tasks should be assigned next.
        """
        # Build a consistent point-in-time snapshot of all system state the
        # scheduler needs.  Each query reads from the DB independently (no
        # transaction), but the ~5s cycle frequency means the snapshot is
        # close enough to consistent for scheduling purposes.
        projects = await self.db.list_projects()
        tasks = await self.db.list_tasks()
        agents = await self.db.list_agents()

        # Token usage within the rolling window — this is the "actual usage"
        # that the deficit-based scheduler compares against each project's
        # credit_weight target ratio to achieve proportional fairness.
        window_start = time.time() - (self.config.scheduling.rolling_window_hours * 3600)
        project_usage = {}
        for p in projects:
            project_usage[p.id] = await self.db.get_project_token_usage(p.id, since=window_start)

        # Active (BUSY) agent count per project — used to enforce each
        # project's max_concurrent_agents limit.  We look up the project
        # from the agent's current task rather than storing it on the agent
        # directly, because agent-project affinity is transient.
        active_counts: dict[str, int] = {}
        for a in agents:
            if a.state == AgentState.BUSY and a.current_task_id:
                task = await self.db.get_task(a.current_task_id)
                if task:
                    active_counts[task.project_id] = active_counts.get(task.project_id, 0) + 1

        total_used = sum(project_usage.values())

        # Workspace availability acts as a hard constraint: a project
        # cannot be assigned work if all its workspaces are locked by
        # running agents.  This prevents the scheduler from assigning
        # more tasks than can physically execute in parallel.
        workspace_counts: dict[str, int] = {}
        for p in projects:
            workspace_counts[p.id] = await self.db.count_available_workspaces(p.id)

        # NOTE: tasks_completed_in_window is empty here, which effectively
        # disables the min_task_guarantee phase of the scheduler.  All projects
        # are treated as having >0 completions, so scheduling falls through
        # directly to deficit-based proportional allocation.  This is a known
        # simplification — to enable min_task_guarantee, populate this dict
        # from a DB query like:
        #   SELECT project_id, COUNT(*) FROM task_results
        #   WHERE created_at >= :window_start GROUP BY project_id

        # Build workspace lock map for workspace affinity enforcement.
        # Tasks with preferred_workspace_id are only assignable when that
        # workspace is unlocked.
        all_workspaces = await self.db.list_workspaces()
        workspace_locks = {ws.id: ws.locked_by_task_id for ws in all_workspaces}

        state = SchedulerState(
            projects=projects,
            tasks=tasks,
            agents=agents,
            project_token_usage=project_usage,
            project_active_agent_counts=active_counts,
            tasks_completed_in_window={},
            project_available_workspaces=workspace_counts,
            workspace_locks=workspace_locks,
            global_budget=self.config.global_token_budget_daily,
            global_tokens_used=total_used,
        )

        return Scheduler.schedule(state)

    @staticmethod
    def _remove_sentinel(workspace: str) -> None:
        """Remove the .agent-queue-lock sentinel file from a workspace."""
        sentinel = os.path.join(workspace, ".agent-queue-lock")
        try:
            os.remove(sentinel)
        except OSError:
            pass

    async def _prepare_workspace(self, task: Task, agent) -> str | None:
        """Acquire a workspace lock and prepare it for the task.

        Steps:
        1. Acquire an unlocked workspace for the project via
           ``db.acquire_workspace()``.  This is an atomic DB operation that
           sets ``locked_by_agent_id`` — only one agent can hold a workspace.
        2. If no workspace is available, return ``None`` (caller returns
           the task to READY and frees the agent).
        3. Determine the branch name:
           - Root tasks: generate a fresh branch from task ID + title.
           - Plan subtasks: reuse the parent task's branch name so all
             steps accumulate commits on a single shared branch.
        4. Perform git operations based on ``workspace.source_type``:
           - CLONE: orchestrator manages the full clone lifecycle (clone on
             first use, fetch + branch on subsequent uses).
           - LINK: workspace points to a pre-existing local checkout;
             orchestrator only manages branch operations, never clones.
        5. Return the workspace path.

        Error resilience: git failures (network issues, auth errors) are
        caught and reported via Discord but do NOT prevent the workspace
        from being returned.  The agent can still work in the directory —
        it just won't have proper branch management.
        """
        project = await self.db.get_project(task.project_id)
        ws = await self.db.acquire_workspace(
            task.project_id,
            agent.id,
            task.id,
            preferred_workspace_id=task.preferred_workspace_id,
        )

        if not ws:
            return None

        workspace = ws.workspace_path

        # Layer 2: Filesystem sentinel — detect concurrent access that slipped
        # past the DB-level path lock (e.g. race condition, stale lock).
        # If the sentinel's owner task is no longer IN_PROGRESS, the sentinel
        # is stale (left behind by a crash) and safe to remove.
        sentinel = os.path.join(workspace, ".agent-queue-lock")
        if os.path.exists(sentinel):
            try:
                with open(sentinel) as f:
                    owner_info = f.read().strip()
            except OSError:
                owner_info = "(unreadable)"

            # Check if the sentinel owner is still actively running.
            # Sentinel format: "task_id\nagent_id\n"
            owner_task_id = owner_info.split("\n")[0] if owner_info else ""
            owner_active = False
            if owner_task_id:
                owner_task = await self.db.get_task(owner_task_id)
                owner_active = (
                    owner_task is not None and owner_task.status == TaskStatus.IN_PROGRESS
                )

            if owner_active:
                logger.warning(
                    "Workspace %s has active sentinel (owner: %s) — releasing lock",
                    workspace,
                    owner_info,
                )
                await self.db.release_workspace(ws.id)
                return None
            else:
                # Stale sentinel from a crashed/completed task — clean it up
                logger.info(
                    "Workspace %s has stale sentinel (owner: %s) — removing",
                    workspace,
                    owner_info,
                )
                self._remove_sentinel(workspace)
        # Write our sentinel
        try:
            os.makedirs(workspace, exist_ok=True)
            with open(sentinel, "w") as f:
                f.write(f"{task.id}\n{agent.id}\n")
        except OSError as e:
            logger.warning("Failed to write sentinel to %s: %s", workspace, e)

        repo_url = project.repo_url if project else ""
        default_branch = await self._get_default_branch(project, workspace)

        # Branch naming strategy:
        # - Root tasks get a fresh branch derived from their ID + title.
        # - Plan subtasks REUSE their parent task's branch name so all
        #   steps in the chain accumulate commits on a single branch.
        #   This is what enables the "one PR for the whole plan" workflow
        #   in _complete_workspace.
        if task.is_plan_subtask and task.parent_task_id:
            parent = await self.db.get_task(task.parent_task_id)
            branch_name = (
                parent.branch_name
                if parent and parent.branch_name
                else GitManager.make_branch_name(task.id, task.title)
            )
        else:
            branch_name = GitManager.make_branch_name(task.id, task.title)

        # Git operations may fail (network issues, auth errors, merge
        # conflicts) but should never prevent returning the workspace path.
        # The agent can still work in the directory; it just won't have
        # proper branch management.  Errors are reported via Discord
        # notification so operators are aware.
        try:
            # Workspace source types determine the git setup strategy:
            #
            # CLONE: The orchestrator manages the full clone lifecycle.
            #   - First use: clone from repo_url into the workspace path.
            #   - Subsequent uses: fetch + ensure clean default branch.
            #
            # LINK: The workspace points to a pre-existing local checkout
            #   (e.g. the developer's own repo).  The orchestrator only
            #   validates the directory exists.
            #
            # In both cases the orchestrator no longer creates task branches.
            # The agent receives branch-name + default-branch in its prompt
            # and handles branch creation, merge, and push itself.
            if ws.source_type == RepoSourceType.CLONE:
                if not await self.git.avalidate_checkout(workspace):
                    os.makedirs(os.path.dirname(workspace), exist_ok=True)
                    if repo_url:
                        await self.git.acreate_checkout(repo_url, workspace)
                    # Re-detect default branch now that the repo is cloned.
                    # The initial detection (above) may have fallen back to
                    # "main" because the workspace didn't exist yet.
                    default_branch = await self._get_default_branch(project, workspace)

            elif ws.source_type == RepoSourceType.LINK:
                if not os.path.isdir(workspace):
                    await self._emit_text_notify(
                        f"**Warning:** Linked workspace path `{workspace}` does not exist.",
                        project_id=task.project_id,
                    )

            # Ensure workspace is on a clean, up-to-date default branch.
            # The agent will create/switch to the task branch per its prompt.
            if await self.git.avalidate_checkout(workspace):
                # Discard any uncommitted changes left by a previous task
                # so the checkout to default_branch doesn't fail due to
                # conflicts.  This is especially important for LINK
                # workspaces without remotes, where no hard-reset follows.
                #
                # We first abort any in-progress operations (merge/rebase)
                # and remove stale lock files, then force-clean the workspace.
                # The old approach (checkout -- . + clean -fd) failed when
                # the workspace was in a mid-merge/rebase state or had
                # staged-but-uncommitted changes.
                if await self.git.ahas_uncommitted_changes(workspace):
                    try:
                        await self.git.aforce_clean_workspace(workspace)
                    except GitError:
                        pass  # Best-effort cleanup
                if await self.git.ahas_remote(workspace):
                    await self.git._arun(["fetch", "origin"], cwd=workspace)
                try:
                    await self.git._arun(["checkout", default_branch], cwd=workspace)
                except GitError:
                    pass  # May already be on default branch
                if await self.git.ahas_remote(workspace):
                    await self.git._arun(
                        ["reset", "--hard", f"origin/{default_branch}"],
                        cwd=workspace,
                    )

            # Update task branch in DB
            await self.db.update_task(task.id, branch_name=branch_name)
        except Exception as e:
            # Layer 3: Git failure means no launch — release workspace and
            # clean up the sentinel so another task can use this workspace.
            logger.error("Git setup failed for task %s in %s: %s", task.id, workspace, e)
            await self._emit_text_notify(
                f"**Git Error:** Task `{task.id}` — branch setup failed: {e}\n"
                f"Workspace released. Task will retry when a workspace is available.",
                project_id=task.project_id,
            )
            self._remove_sentinel(workspace)
            await self.db.release_workspace(ws.id)
            return None

        # Clean up ALL plan files from previous tasks to prevent:
        # 1. A new task from discovering a stale plan that belongs to another task
        # 2. A new task failing to write its own plan because the file already exists
        # This covers both archived plans (.claude/plans/) and primary plan files
        # (.claude/plan.md, plan.md, etc.).
        await self._cleanup_plan_files_before_task(
            workspace, task.id, branch_name=branch_name, default_branch=default_branch
        )

        return workspace

    async def _cleanup_plan_files_before_task(
        self,
        workspace: str,
        task_id: str,
        *,
        branch_name: str | None = None,
        default_branch: str = "main",
    ) -> None:
        """Remove all plan files from previous tasks before starting a new one.

        Cleans up:

        1. **Primary plan files** (``.claude/plan.md``, ``plan.md``, etc.) on
           the current (default) branch — leftover files can cause agents to
           fail to write their own plan ("file already exists") or lead to
           stale plan re-discovery.
        2. **Archived plan files** (``.claude/plans/<task_id>-plan.md``) —
           clearly attributable to specific tasks via their filename prefix.
           Any that don't belong to the current task are removed.
        3. **Plan files on the task branch** (if ``branch_name`` is provided
           and the branch already exists locally or as a remote tracking
           branch).  When a task is retried, or a plan subtask follows a
           sibling, the agent may have committed plan files directly to the
           task branch via ``git add && git commit``.  Those files would
           reappear when the new agent checks out the branch.

        .. note::

           Deletions are committed using direct ``git add -A`` + ``git commit``
           instead of :meth:`GitManager.acommit_all`, because ``acommit_all``
           excludes plan files from commits (via ``_PLAN_FILE_EXCLUDES``).
        """
        import glob as _glob

        plan_patterns = self.config.auto_task.plan_file_patterns

        def _remove_plan_files() -> bool:
            """Delete primary plan files + archived plans.  Returns True if any deleted."""
            deleted = False
            for pattern in plan_patterns:
                full_pattern = os.path.join(workspace, pattern)
                for fpath in _glob.glob(full_pattern):
                    if os.path.isfile(fpath):
                        try:
                            os.remove(fpath)
                            deleted = True
                            logger.info(
                                "Pre-task cleanup: removed plan file %s (task %s)",
                                fpath,
                                task_id,
                            )
                        except OSError as e:
                            logger.warning(
                                "Pre-task cleanup: failed to remove plan file %s: %s",
                                fpath,
                                e,
                            )

            plans_dir = os.path.join(workspace, ".claude", "plans")
            if os.path.isdir(plans_dir):
                try:
                    for entry in os.listdir(plans_dir):
                        if entry.startswith(task_id):
                            continue
                        fpath = os.path.join(plans_dir, entry)
                        if os.path.isfile(fpath) and entry.endswith(".md"):
                            os.remove(fpath)
                            deleted = True
                            logger.info(
                                "Pre-task cleanup: removed archived plan %s (task %s)",
                                fpath,
                                task_id,
                            )
                except OSError as e:
                    logger.warning(
                        "Pre-task cleanup: failed to clean plans dir %s: %s",
                        plans_dir,
                        e,
                    )
            return deleted

        async def _commit_plan_deletions(msg: str) -> None:
            """Commit all pending changes including plan file deletions.

            Uses direct ``git add -A`` + ``git commit`` instead of
            ``acommit_all`` which excludes plan files via
            ``_PLAN_FILE_EXCLUDES``.
            """
            try:
                if not await self.git.avalidate_checkout(workspace):
                    return
                await self.git._arun(["add", "-A"], cwd=workspace)
                result = await self.git._arun_subprocess(
                    ["git", "diff", "--cached", "--quiet"],
                    cwd=workspace,
                    timeout=self.git._GIT_TIMEOUT,
                )
                if result.returncode != 0:
                    await self.git._arun(["commit", "-m", msg], cwd=workspace)
            except Exception as e:
                logger.warning("Pre-task cleanup: failed to commit: %s", e)

        # ── 1. Clean current (default) branch ─────────────────────────────
        if _remove_plan_files():
            await _commit_plan_deletions(
                f"chore: clean up stale plan files before task {task_id}",
            )

        # ── 2. Clean the task branch if it already exists ─────────────────
        # When a task is retried (or a plan subtask follows a sibling),
        # the task branch may carry plan files committed by a previous
        # agent run.  Checkout that branch, purge plan files, commit the
        # deletion, and return to the default branch so the agent starts
        # from a clean state.
        if branch_name and await self.git.avalidate_checkout(workspace):
            switched = False
            try:
                await self.git._arun(["checkout", branch_name], cwd=workspace)
                switched = True
            except GitError:
                pass  # Branch does not exist — nothing to clean

            if switched:
                try:
                    if _remove_plan_files():
                        await _commit_plan_deletions(
                            f"chore: clean up stale plan files before task {task_id}",
                        )
                finally:
                    # Always return to the default branch even if cleanup failed.
                    try:
                        await self.git._arun(["checkout", default_branch], cwd=workspace)
                    except Exception as e:
                        logger.error(
                            "Pre-task cleanup: CRITICAL — failed to return to %s "
                            "after cleaning task branch %s: %s",
                            default_branch,
                            branch_name,
                            e,
                        )

    async def _is_last_subtask(self, task: Task) -> bool:
        """Check if this subtask is the final one to complete in a plan chain.

        Returns True when every sibling subtask (all tasks sharing the same
        ``parent_task_id``) has already reached COMPLETED status.  This
        determines whether the post-completion workflow should trigger the
        "final step" actions: merge to default branch or create a PR.

        Intermediate subtasks only commit to the shared branch — they do
        not merge or create PRs, keeping the chain flowing without human
        intervention until the last step.
        """
        if not task.parent_task_id:
            return True
        siblings = await self.db.get_subtasks(task.parent_task_id)
        for sibling in siblings:
            if sibling.id == task.id:
                continue
            if sibling.status != TaskStatus.COMPLETED:
                return False
        return True

    async def _merge_and_push(
        self,
        task: Task,
        repo: RepoConfig,
        workspace: str,
        *,
        _max_retries: int = 3,
    ) -> None:
        """Merge the task branch into default and push.

        .. deprecated::
            No longer called by the completion pipeline.  The agent now
            handles merging and pushing via its prompt instructions.  Kept
            for manual recovery use cases.
        """
        has_remote = await self.git.ahas_remote(workspace)

        if has_remote:
            # sync_and_merge handles fetch, hard-reset, merge, and push
            # with retry.  max_retries counts *retries* after the first
            # attempt, so subtract 1 from _max_retries (total attempts).
            success, error = await self.git.async_and_merge(
                workspace,
                task.branch_name,
                repo.default_branch,
                max_retries=max(_max_retries - 1, 0),
            )
            if not success:
                if error == "merge_conflict":
                    await self._emit_notify(
                        "notify.merge_conflict",
                        MergeConflictEvent(
                            task=build_task_detail(task),
                            branch=task.branch_name or "",
                            target_branch=repo.default_branch,
                            project_id=task.project_id,
                        ),
                    )
                else:
                    # error starts with "push_failed: …"
                    await self._emit_notify(
                        "notify.push_failed",
                        PushFailedEvent(
                            task=build_task_detail(task),
                            branch=repo.default_branch,
                            error_detail=(
                                f"Could not push after {_max_retries} attempts. "
                                f"Workspace may be diverged. Details: {error}"
                            ),
                            project_id=task.project_id,
                        ),
                    )
                # Recovery: reset workspace to origin state so it's clean
                # for the next task.  After a failed push the local default
                # branch may contain un-pushed merge commits; hard-resetting
                # to origin discards them.
                try:
                    await self.git.arecover_workspace(workspace, repo.default_branch)
                except Exception:
                    pass  # best-effort recovery
                return
        else:
            # LINK / INIT repos have no remote — just merge locally.
            merged = await self.git.amerge_branch(
                workspace,
                task.branch_name,
                repo.default_branch,
            )
            if not merged:
                # Rebase fallback: rebase the task branch onto the default
                # branch and retry the merge.  This resolves conflicts caused
                # by the task branch being based on a stale snapshot.
                rebased = await self.git.arebase_onto(
                    workspace,
                    task.branch_name,
                    repo.default_branch,
                )
                if rebased:
                    merged = await self.git.amerge_branch(
                        workspace,
                        task.branch_name,
                        repo.default_branch,
                    )
            if not merged:
                await self._emit_notify(
                    "notify.merge_conflict",
                    MergeConflictEvent(
                        task=build_task_detail(task),
                        branch=task.branch_name or "",
                        target_branch=repo.default_branch,
                        project_id=task.project_id,
                    ),
                )
                # Recovery: ensure we're on the default branch so the
                # workspace is clean for the next task.  merge_branch()
                # already aborts the merge, but we make sure we're on the
                # right branch as a safety net.
                try:
                    await self.git._arun(
                        ["checkout", repo.default_branch],
                        cwd=workspace,
                    )
                except Exception:
                    pass  # best-effort recovery
                return

            # Clean up the task branch after successful local merge
            try:
                await self.git.adelete_branch(
                    workspace,
                    task.branch_name,
                    delete_remote=False,
                )
            except Exception:
                pass  # branch cleanup is best-effort
            return

        # Clean up the task branch after successful merge + push
        try:
            await self.git.adelete_branch(
                workspace,
                task.branch_name,
                delete_remote=has_remote,
            )
        except Exception:
            pass  # branch cleanup is best-effort

    async def _create_pr_for_task(
        self,
        task: Task,
        repo: RepoConfig,
        workspace: str,
    ) -> str | None:
        """Push the task branch and create a PR. Returns the PR URL or None.

        .. deprecated::
            No longer called by the completion pipeline.  The agent now
            creates PRs via its prompt instructions.  Kept for manual use.

        Uses ``force_with_lease=True`` when pushing the task branch so that
        retries (e.g. after a failed PR creation where the push succeeded)
        don't fail with a non-fast-forward error.  ``--force-with-lease`` is
        safe here because the task branch is owned exclusively by this agent —
        no other user is expected to push to it (resolves **G5**).
        """
        if not await self.git.ahas_remote(workspace):
            # No remote — notify user to review the branch locally
            await self._emit_text_notify(
                f"**Approval Required:** Task `{task.id}` — {task.title}\n"
                f"Branch `{task.branch_name}` is ready for review in `{workspace}`.\n"
                f"Use the `approve_task` command to complete it.",
                project_id=task.project_id,
            )
            return None

        try:
            # Use --force-with-lease so the push succeeds even when the
            # branch was previously pushed (e.g. task retries or subtask
            # chains that push intermediate results).  Task branches are
            # owned by a single agent, so force-pushing is safe.
            await self.git.apush_branch(
                workspace,
                task.branch_name,
                force_with_lease=True,
                event_bus=self.bus,
                project_id=task.project_id,
            )
        except Exception as e:
            await self._emit_notify(
                "notify.push_failed",
                PushFailedEvent(
                    task=build_task_detail(task),
                    branch=task.branch_name or "",
                    error_detail=str(e),
                    project_id=task.project_id,
                ),
            )
            return None

        try:
            pr_url = await self.git.acreate_pr(
                workspace,
                branch=task.branch_name,
                title=task.title,
                body=f"Automated PR for task `{task.id}`.\n\n{task.description[:500]}",
                base=repo.default_branch,
                event_bus=self.bus,
                project_id=task.project_id,
            )
            return pr_url
        except Exception as e:
            await self._emit_text_notify(
                f"**PR Creation Failed:** Task `{task.id}` — {e}\n"
                f"Branch `{task.branch_name}` has been pushed. Create a PR manually.",
                project_id=task.project_id,
            )
            return None

    async def _task_has_code_changes(
        self, workspace: str, min_files: int = 3, min_lines: int = 50
    ) -> bool:
        """Check if the current branch has substantial non-plan code changes.

        Uses ``git diff --stat`` against the merge-base with the default branch,
        excluding plan file paths.  Returns True if the diff exceeds the given
        thresholds (files changed OR lines changed), indicating the plan was
        likely already implemented during this task.

        Args:
            workspace: Path to the git checkout.
            min_files: Minimum number of changed files to consider "substantial".
            min_lines: Minimum number of lines changed (insertions + deletions).

        Returns:
            True if the branch has substantial code changes beyond plan files.
        """
        try:
            if not await self.git.avalidate_checkout(workspace):
                return False

            default_branch = await self.git.aget_default_branch(workspace)

            # Find the merge-base between HEAD and the default branch
            try:
                merge_base = await self.git._arun(
                    ["merge-base", f"origin/{default_branch}", "HEAD"],
                    cwd=workspace,
                )
            except GitError:
                # No remote tracking or no common ancestor — can't compare
                try:
                    merge_base = await self.git._arun(
                        ["merge-base", default_branch, "HEAD"],
                        cwd=workspace,
                    )
                except GitError:
                    return False

            # Get diff stat excluding plan files and non-code artifacts
            stat_output = await self.git._arun(
                [
                    "diff",
                    "--stat",
                    f"{merge_base}..HEAD",
                    "--",
                    ".",
                    # Exclude plan files
                    ":!.claude/plan.md",
                    ":!plan.md",
                    ":!.claude/plans/",
                    ":!docs/plans/",
                    ":!docs/plan.md",
                    ":!plans/",
                    # Exclude non-code artifacts (notes, logs, test results)
                    ":!notes/",
                    ":!*.log",
                    ":!test-results*",
                ],
                cwd=workspace,
            )

            if not stat_output:
                return False

            # Parse the summary line, e.g.:
            #  "10 files changed, 200 insertions(+), 50 deletions(-)"
            lines = stat_output.strip().splitlines()
            summary = lines[-1] if lines else ""

            import re

            files_match = re.search(r"(\d+)\s+files?\s+changed", summary)
            insertions_match = re.search(r"(\d+)\s+insertions?", summary)
            deletions_match = re.search(r"(\d+)\s+deletions?", summary)

            files_changed = int(files_match.group(1)) if files_match else 0
            insertions = int(insertions_match.group(1)) if insertions_match else 0
            deletions = int(deletions_match.group(1)) if deletions_match else 0
            total_lines = insertions + deletions

            return files_changed >= min_files or total_lines >= min_lines

        except Exception as e:
            logger.debug("_task_has_code_changes failed (will proceed normally): %s", e)
            return False

    async def _discover_and_store_plan(self, task: Task, workspace: str) -> bool:
        """Discover a plan file, parse it, and store the parsed data for approval.

        Called after a task completes successfully.  Searches the workspace
        for a plan file (e.g. ``.claude/plan.md``) using configurable glob
        patterns, parses it, and stores the parsed plan data as task_context
        entries so the plan can be presented to the user for approval.

        The plan file is archived to ``.claude/plans/<task_id>-plan.md``
        after processing to prevent re-processing if the workspace is reused.

        Returns True if a plan was found, parsed, and stored for approval.
        """
        config = self.config.auto_task
        if not config.enabled:
            return False

        # Prevent recursive plan explosion: subtasks must not generate
        # further sub-plans.
        if task.is_plan_subtask:
            return False

        # Git-diff heuristic: if the task already made substantial code
        # changes (beyond the plan file itself), the plan was likely
        # already executed during this task — skip generating subtasks.
        if config.skip_if_implemented:
            try:
                project = await self.db.get_project(task.project_id) if task.project_id else None
                default_branch = await self._get_default_branch(project, workspace)
                if await self.git.ahas_non_plan_changes(workspace, default_branch):
                    logger.info(
                        "Auto-task: skipping plan approval for task %s — "
                        "branch has substantial code changes beyond the plan file, "
                        "indicating the plan was already implemented",
                        task.id,
                    )
                    return False
            except Exception as e:
                # On any error, fall through to normal behaviour
                logger.debug(
                    "Auto-task: skip_if_implemented check failed for task %s: %s",
                    task.id,
                    e,
                )

        plan_path = find_plan_file(workspace, config.plan_file_patterns)
        if not plan_path:
            logger.debug(
                "Auto-task: no plan file found for task %s in workspace %s (searched patterns: %s)",
                task.id,
                workspace,
                config.plan_file_patterns,
            )
            return False

        # Staleness check: compare the plan file's mtime against the
        # recorded execution start time.  If the plan file predates the
        # agent's execution start, it was written by a previous task and
        # should not be attributed to this one.  This prevents stale plan
        # files (left behind by failed cleanups) from being incorrectly
        # picked up by unrelated tasks sharing the same workspace.
        exec_start = self._task_exec_start.get(task.id)
        if exec_start is not None:
            try:
                plan_mtime = os.path.getmtime(plan_path)
                # Use a 2-second tolerance to account for filesystem
                # timestamp granularity (some filesystems round to 1s).
                # In practice, stale plans are minutes/hours old.
                if plan_mtime < exec_start - 2.0:
                    logger.warning(
                        "Auto-task: ignoring stale plan file %s for task %s "
                        "(plan mtime %.0f < exec start %.0f — file "
                        "predates this task's execution)",
                        plan_path,
                        task.id,
                        plan_mtime,
                        exec_start,
                    )
                    # Archive it so it's not rediscovered by future tasks
                    try:
                        plans_dir = os.path.join(workspace, ".claude", "plans")
                        os.makedirs(plans_dir, exist_ok=True)
                        stale_archive = os.path.join(plans_dir, f"stale-{task.id}-plan.md")
                        os.rename(plan_path, stale_archive)
                    except OSError:
                        pass
                    return False
            except OSError as e:
                logger.debug(
                    "Auto-task: staleness check failed for %s: %s (proceeding)",
                    plan_path,
                    e,
                )

        try:
            raw = read_plan_file(plan_path)
        except Exception as e:
            logger.warning("Auto-task: failed to read plan file %s: %s", plan_path, e)
            return False

        if not raw or not raw.strip():
            logger.info("Auto-task: plan file %s is empty", plan_path)
            return False

        logger.info("Auto-task: found plan file %s for task %s", plan_path, task.id)

        # Archive the plan file for traceability (so it won't be re-processed
        # if the workspace is reused for another task).
        archived_path = None
        try:
            plans_dir = os.path.join(workspace, ".claude", "plans")
            os.makedirs(plans_dir, exist_ok=True)
            archived_path = os.path.join(plans_dir, f"{task.id}-plan.md")
            os.rename(plan_path, archived_path)
        except OSError:
            pass

        # Store the raw plan content as task_context.  The actual
        # plan-to-task splitting is done by the supervisor LLM at
        # approval time (not by algorithmic parsing here).
        await self.db.add_task_context(
            task.id,
            type="plan_raw",
            label="Plan Raw Content",
            content=raw,
        )
        if archived_path:
            await self.db.add_task_context(
                task.id,
                type="plan_archived_path",
                label="Plan Archived Path",
                content=archived_path,
            )

        logger.info(
            "Auto-task: stored plan for task %s — awaiting user approval",
            task.id,
        )
        return True

    # ── Completion pipeline ────────────────────────────────────────────────
    #
    # The completion pipeline runs: commit → plan_generate → merge.
    # Plan generation runs BEFORE merge so the plan file is archived
    # (and the archival committed) before the branch is merged to the
    # default branch.  This prevents the plan file from persisting on
    # main and being re-discovered by subsequent tasks.
    # Each phase receives a PipelineContext and returns a PhaseResult.

    async def _run_completion_pipeline(self, ctx: PipelineContext) -> tuple[str | None, bool]:
        """Run the post-completion pipeline. Returns (pr_url, completed_ok).

        Phase execution strategy:
        - **plan_discover**: Non-critical — if it crashes, log and continue
          to the verify phase.  Plan discovery failure should not prevent
          git verification and auto-remediation from running.
        - **verify**: Critical — if it crashes or returns STOP, the task
          cannot be marked completed.
        """
        # Phase 1: Plan discovery (non-critical)
        try:
            result = await self._phase_plan_discover(ctx)
            if result == PhaseResult.STOP or result == PhaseResult.ERROR:
                logger.warning(
                    "Pipeline phase 'plan_discover' returned %s for task %s — continuing to verify",
                    result,
                    ctx.task.id,
                )
        except Exception as e:
            logger.error(
                "Pipeline phase 'plan_discover' failed for task %s: %s — continuing to verify",
                ctx.task.id,
                e,
                exc_info=True,
            )

        # Phase 2: Git verification (critical)
        try:
            result = await self._phase_verify(ctx)
        except Exception as e:
            logger.error(
                "Pipeline phase 'verify' failed for task %s: %s",
                ctx.task.id,
                e,
                exc_info=True,
            )
            return (ctx.pr_url, False)
        if result == PhaseResult.STOP:
            return (ctx.pr_url, False)
        if result == PhaseResult.ERROR:
            return (ctx.pr_url, False)

        return (ctx.pr_url, True)

    async def _phase_verify(self, ctx: PipelineContext) -> PhaseResult:
        """Pipeline phase: verify the agent left the workspace in the expected git state.

        Replaces the old _phase_commit + _phase_merge.  The agent is now
        responsible for committing, merging, and pushing via its prompt
        instructions.  This phase only *checks* the result and reopens the
        task with specific feedback when something is off.

        Verification scenarios:

        * **Intermediate subtask** — expect: on task branch, no uncommitted.
        * **Final task / final subtask, requires_approval** — expect: on task
          branch, branch pushed, PR exists.
        * **Final task / final subtask, no approval** — expect: on default
          branch, no uncommitted, in sync with origin.
        * **No-change task** — on default branch with no diff → pass.
        """
        workspace = ctx.workspace_path
        task = ctx.task

        # Skip verification if the agent exited with an error — bad git state
        # is a symptom, not the root cause.  Let normal error handling deal
        # with the task instead of reopening for git fixes.
        if ctx.output.exit_code and ctx.output.exit_code != 0:
            logger.info(
                "Task %s: agent exited with non-zero exit code (%d), skipping git verification",
                task.id,
                ctx.output.exit_code,
            )
            # Still auto-remediate uncommitted changes so the workspace is
            # clean for the next task.  Without this, a crashed agent leaves
            # dirty state that bleeds into subsequent tasks.
            if workspace and await self.git.avalidate_checkout(workspace):
                try:
                    if await self.git.ahas_uncommitted_changes(workspace):
                        current = await self.git.aget_current_branch(workspace)
                        await self._auto_remediate_uncommitted(
                            workspace,
                            task.id,
                            current,
                            project_id=task.project_id,
                            agent_id=ctx.agent.id,
                        )
                except Exception as e:
                    logger.warning(
                        "Task %s: auto-remediation during skip failed: %s",
                        task.id,
                        e,
                    )
            return PhaseResult.CONTINUE

        # Skip verification if the task opted out (e.g. research/investigation tasks)
        if task.skip_verification:
            logger.info("Task %s: skip_verification=True, skipping git verification", task.id)
            return PhaseResult.CONTINUE

        if not workspace or not await self.git.avalidate_checkout(workspace):
            return PhaseResult.CONTINUE
        if not task.branch_name:
            return PhaseResult.CONTINUE

        default_branch = ctx.default_branch
        has_remote = await self.git.ahas_remote(workspace)
        current_branch = await self.git.aget_current_branch(workspace)
        has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)

        # Determine which scenario we're in
        is_intermediate = task.is_plan_subtask and not await self._is_last_subtask(task)
        if task.is_plan_subtask and task.parent_task_id:
            parent = await self.db.get_task(task.parent_task_id)
            requires_approval = parent.requires_approval if parent else task.requires_approval
        else:
            requires_approval = task.requires_approval

        # ── Auto-remediate: commit uncommitted changes ──────────────────
        # Agents frequently forget to commit their work before completing.
        # Rather than reopening the task (which often repeats the same
        # mistake, causing retry loops), commit the changes automatically
        # and continue verification.
        #
        # We pass exclude_plans=False because this is a system-level
        # auto-remediation — we need to commit ALL changes including plan
        # files.  The plan file exclusion is meant to prevent agent-initiated
        # commits from including plans, but auto-remediation must clean up
        # everything to avoid verification failures.
        #
        # We use no_verify=True to bypass pre-commit hooks which can
        # reject the auto-commit (e.g. ruff formatting) and cause the
        # very retry loops we're trying to prevent.
        if has_uncommitted:
            has_uncommitted = await self._auto_remediate_uncommitted(
                workspace,
                task.id,
                current_branch,
                project_id=task.project_id,
                agent_id=ctx.agent.id,
            )

        # ── Auto-remediate: merge to default branch ────────────────────
        # For normal tasks (not intermediate, not PR workflow), the agent
        # should have merged to the default branch.  If they forgot, do
        # it automatically to avoid retry loops.
        if (
            not is_intermediate
            and not requires_approval
            and not has_uncommitted
            and current_branch != default_branch
            and current_branch == task.branch_name
        ):
            try:
                await self.git._arun(["checkout", default_branch], cwd=workspace)
                await self.git._arun(["merge", current_branch, "--no-edit"], cwd=workspace)
                logger.info(
                    "Task %s: auto-merged branch '%s' into '%s'",
                    task.id,
                    current_branch,
                    default_branch,
                )
                current_branch = default_branch
                # Check for uncommitted changes after merge (e.g. conflicts
                # that resulted in a dirty state)
                has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
                if has_uncommitted:
                    has_uncommitted = await self._auto_remediate_uncommitted(
                        workspace,
                        task.id,
                        current_branch,
                        project_id=task.project_id,
                        agent_id=ctx.agent.id,
                    )
            except Exception as e:
                logger.warning(
                    "Task %s: auto-merge of '%s' into '%s' failed: %s",
                    task.id,
                    current_branch,
                    default_branch,
                    e,
                )
                # Abort merge if it left us in a conflicted state
                try:
                    await self.git._arun(["merge", "--abort"], cwd=workspace)
                except Exception:
                    pass
                # Try to get back to the branch we were on
                try:
                    current_branch = await self.git.aget_current_branch(workspace)
                except Exception:
                    pass
                # Re-check for uncommitted changes — the failed merge or
                # abort may have left the workspace dirty.
                try:
                    has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
                    if has_uncommitted:
                        has_uncommitted = await self._auto_remediate_uncommitted(
                            workspace,
                            task.id,
                            current_branch,
                            project_id=task.project_id,
                            agent_id=ctx.agent.id,
                        )
                except Exception:
                    pass

        # ── Auto-remediate: merge task branch to default ─────────────────
        # For normal tasks (no approval, not intermediate), the agent is
        # expected to merge the task branch into the default branch. Agents
        # frequently forget this step, leaving the workspace on the task
        # branch.  Rather than reopening (which often repeats the mistake),
        # perform the merge automatically.
        if (
            not is_intermediate
            and not requires_approval
            and not has_uncommitted
            and current_branch != default_branch
            and task.branch_name
        ):
            try:
                # Checkout default branch
                await self.git._arun(["checkout", default_branch], cwd=workspace)
                # Merge the task branch into default
                await self.git._arun(["merge", current_branch], cwd=workspace)
                logger.info(
                    "Task %s: auto-merged branch '%s' into '%s'",
                    task.id,
                    current_branch,
                    default_branch,
                )
                # Delete the task branch (best-effort)
                try:
                    await self.git._arun(["branch", "-d", current_branch], cwd=workspace)
                except Exception:
                    pass  # Non-critical — branch delete can fail safely
                current_branch = default_branch
            except Exception as e:
                logger.warning(
                    "Task %s: auto-merge of '%s' into '%s' failed: %s",
                    task.id,
                    current_branch,
                    default_branch,
                    e,
                )
                # Abort any partial merge and switch back to the task branch
                try:
                    await self.git._arun(["merge", "--abort"], cwd=workspace)
                except Exception:
                    pass
                try:
                    await self.git._arun(["checkout", current_branch], cwd=workspace)
                except Exception:
                    pass
                # Re-check for uncommitted changes — the failed merge or
                # abort may have left the workspace dirty.
                try:
                    has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
                    if has_uncommitted:
                        has_uncommitted = await self._auto_remediate_uncommitted(
                            workspace,
                            task.id,
                            current_branch,
                            project_id=task.project_id,
                            agent_id=ctx.agent.id,
                        )
                except Exception:
                    pass

        # ── Auto-remediate: push unpushed commits ───────────────────────
        # After auto-committing/merging (or if agent committed but forgot
        # to push), push to the remote to avoid unnecessary retries.
        if has_remote and not has_uncommitted:
            # Determine the expected branch for this task type
            if is_intermediate or requires_approval:
                expected_push_branch = task.branch_name
            else:
                expected_push_branch = default_branch
            if current_branch == expected_push_branch:
                try:
                    ahead_output = await self.git._arun(
                        ["rev-list", f"origin/{current_branch}..HEAD", "--count"],
                        cwd=workspace,
                    )
                    if ahead_output.strip() != "0":
                        await self.git.apush_branch(
                            workspace,
                            current_branch,
                            event_bus=self.bus,
                            project_id=task.project_id,
                        )
                        logger.info(
                            "Task %s: auto-pushed %s commit(s) on branch '%s'",
                            task.id,
                            ahead_output.strip(),
                            current_branch,
                        )
                except Exception as e:
                    logger.warning(
                        "Task %s: auto-push on branch '%s' failed: %s",
                        task.id,
                        current_branch,
                        e,
                    )

        # ── Final safety net: one last remediation sweep ─────────────────
        # Intermediate steps (merge, merge-abort, push attempts) may have
        # introduced new uncommitted changes that weren't caught by the
        # earlier remediation.  Re-check and remediate one more time before
        # building the failure list.
        try:
            has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
            if has_uncommitted:
                current_branch = await self.git.aget_current_branch(workspace)
                has_uncommitted = await self._auto_remediate_uncommitted(
                    workspace,
                    task.id,
                    current_branch,
                    project_id=task.project_id,
                    agent_id=ctx.agent.id,
                )
        except Exception:
            pass

        # Failures are (message, fixable) tuples. Fixable means the agent can
        # resolve the issue (uncommitted changes, missing merge/push/PR).
        # Unfixable issues (behind origin, diverged history) block immediately.
        failures: list[tuple[str, bool]] = []

        if is_intermediate:
            # Intermediate subtask: should be on task branch with work committed
            if has_uncommitted:
                failures.append(
                    (
                        "You left uncommitted changes in the workspace. "
                        f"Please `git add` and `git commit` your changes on branch "
                        f"`{task.branch_name}`.",
                        True,  # fixable — agent can commit
                    )
                )
            if current_branch != task.branch_name and current_branch != default_branch:
                failures.append(
                    (
                        f"Expected workspace to be on branch `{task.branch_name}` "
                        f"but found `{current_branch}`. "
                        f"Please switch to `{task.branch_name}` and commit your work.",
                        True,  # fixable — agent can switch branches
                    )
                )
        elif requires_approval:
            # PR workflow: should be on task branch, branch pushed, PR exists
            if has_uncommitted:
                failures.append(
                    (
                        "You left uncommitted changes. Please commit them on "
                        f"branch `{task.branch_name}` and push.",
                        True,  # fixable — agent can commit and push
                    )
                )
            # Allow being on default if no changes were made (research task)
            if current_branch == default_branch:
                # No-change task — acceptable, skip PR checks
                pass
            else:
                if has_remote:
                    pr_url = await self.git.afind_open_pr(workspace, task.branch_name)
                    if pr_url:
                        ctx.pr_url = pr_url
                    else:
                        failures.append(
                            (
                                f"No open PR found for branch `{task.branch_name}`. "
                                f"Please push your branch and create a PR: "
                                f"`git push origin {task.branch_name}` then "
                                f"`gh pr create --base {default_branch} "
                                f"--head {task.branch_name}`.",
                                True,  # fixable — agent can push and create PR
                            )
                        )
        else:
            # Normal task / final subtask: should be on default, merged, pushed
            if current_branch == default_branch:
                # On default branch — check it's clean and in sync
                if has_uncommitted:
                    failures.append(
                        (
                            "You left uncommitted changes on "
                            f"`{default_branch}`. Please commit or discard them.",
                            True,  # fixable — agent can commit
                        )
                    )
                if has_remote:
                    try:
                        behind = await self.git._arun(
                            ["rev-list", "HEAD..origin/" + default_branch, "--count"],
                            cwd=workspace,
                        )
                        if behind.strip() != "0":
                            # Auto-pull when the agent made no changes (no-op task).
                            # Being behind origin is not the agent's fault — other
                            # agents may have pushed while this task ran.
                            if not has_uncommitted:
                                try:
                                    await self.git._arun(
                                        ["pull", "--ff-only", "origin", default_branch],
                                        cwd=workspace,
                                    )
                                    logger.info(
                                        "Task %s: auto-pulled %s commit(s) on '%s' "
                                        "(no-change task was behind origin)",
                                        task.id,
                                        behind.strip(),
                                        default_branch,
                                    )
                                except Exception as pull_err:
                                    logger.warning(
                                        "Task %s: auto-pull failed: %s",
                                        task.id,
                                        pull_err,
                                    )
                                    failures.append(
                                        (
                                            f"Local `{default_branch}` is behind "
                                            f"`origin/{default_branch}` and auto-pull "
                                            f"failed. Please `git pull origin "
                                            f"{default_branch}`.",
                                            False,  # unfixable
                                        )
                                    )
                            else:
                                failures.append(
                                    (
                                        f"Local `{default_branch}` is behind "
                                        f"`origin/{default_branch}`. "
                                        f"Please `git pull origin {default_branch}`.",
                                        False,  # unfixable — external changes
                                    )
                                )
                    except GitError:
                        pass
                    try:
                        ahead = await self.git._arun(
                            ["rev-list", "origin/" + default_branch + "..HEAD", "--count"],
                            cwd=workspace,
                        )
                        if ahead.strip() != "0":
                            failures.append(
                                (
                                    f"Local `{default_branch}` has unpushed commits. "
                                    f"Please `git push origin {default_branch}`.",
                                    True,  # fixable — agent can push
                                )
                            )
                    except GitError:
                        pass
            else:
                # Not on default — the agent forgot to merge
                failures.append(
                    (
                        f"Workspace is on branch `{current_branch}` instead of "
                        f"`{default_branch}`. Please merge your work into "
                        f"`{default_branch}` and push:\n"
                        f"  `git checkout {default_branch} && "
                        f"git merge {task.branch_name} && "
                        f"git push origin {default_branch}`",
                        True,  # fixable — agent can merge and push
                    )
                )

        if not failures:
            logger.info("Task %s: git verification passed", task.id)
            return PhaseResult.CONTINUE

        # Separate fixable vs unfixable failures
        fixable = [(msg, f) for msg, f in failures if f]
        unfixable = [(msg, f) for msg, f in failures if not f]
        all_msgs = [msg for msg, _ in failures]

        if unfixable:
            # Unfixable issues present — block immediately, don't waste retries
            unfixable_msgs = [msg for msg, _ in unfixable]
            logger.warning(
                "Task %s: git verification found unfixable issues (%d), blocking: %s",
                task.id,
                len(unfixable),
                "; ".join(unfixable_msgs),
            )
            bullet_list = "\n".join(f"- {msg}" for msg in all_msgs)
            await self._emit_text_notify(
                f"⛔ **Verification Blocked:** Task `{task.id}` — "
                f"git state has unfixable issues (not reopening):\n{bullet_list}",
                project_id=task.project_id,
            )
            ctx.verification_reopened = False
            return PhaseResult.STOP

        # Only fixable issues — attempt to reopen with feedback
        logger.warning(
            "Task %s: git verification failed (%d fixable issues): %s",
            task.id,
            len(fixable),
            "; ".join(all_msgs),
        )
        reopened = await self._reopen_with_verification_feedback(task, fixable)
        ctx.verification_reopened = reopened
        return PhaseResult.STOP

    async def _auto_remediate_uncommitted(
        self,
        workspace: str,
        task_id: str,
        current_branch: str,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> bool:
        """Try to commit uncommitted changes using a robust fallback cascade.

        Returns True if uncommitted changes still remain after all attempts,
        False if the workspace is now clean.

        When *project_id* and/or *agent_id* are given, a ``git.commit`` event
        is emitted on the orchestrator's event bus after a successful commit.

        Fallback cascade:
        0. Abort any in-progress git operations (merge/rebase/cherry-pick)
           and remove stale lock files left by crashed processes.
        1. ``git commit`` with ``--no-verify`` to bypass pre-commit hooks.
        2. ``git stash`` to save changes without committing.
        3. ``git reset --hard HEAD && git clean -fdx`` to discard ALL changes.
        """
        # Attempt 0: Clear any in-progress operations and lock files that
        # would cause all subsequent git operations to fail.  This handles
        # the common case where a killed agent left the workspace in a
        # mid-merge/rebase state or left a stale index.lock.
        try:
            await self.git.aabort_in_progress_operations(workspace)
        except Exception as e:
            logger.warning(
                "Task %s: abort in-progress operations failed: %s",
                task_id,
                e,
            )

        # Attempt 1: commit with --no-verify to bypass pre-commit hooks.
        # Hooks (e.g. ruff formatting) are the most common reason
        # auto-commit fails, causing retry loops.
        try:
            committed = await self.git.acommit_all(
                workspace,
                f"auto-commit: uncommitted changes from task {task_id}",
                exclude_plans=False,
                no_verify=True,
                event_bus=self.bus,
                project_id=project_id,
                agent_id=agent_id,
            )
            if committed:
                logger.info(
                    "Task %s: auto-committed uncommitted changes on branch '%s'",
                    task_id,
                    current_branch,
                )
            # Re-check after commit attempt — handles edge cases where
            # acommit_all returns False but some changes remain (e.g.
            # gitignored files that show in porcelain output).
            has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
            if not has_uncommitted:
                return False
        except Exception as e:
            logger.warning(
                "Task %s: auto-commit (--no-verify) failed: %s",
                task_id,
                e,
            )

        # Attempt 2: stash changes (preserves work, less accessible).
        try:
            await self.git._arun(
                [
                    "stash",
                    "--include-untracked",
                    "-m",
                    f"auto-stash: uncommitted changes from task {task_id}",
                ],
                cwd=workspace,
            )
            logger.info(
                "Task %s: stashed uncommitted changes on branch '%s'",
                task_id,
                current_branch,
            )
            has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
            if not has_uncommitted:
                return False
        except Exception as e:
            logger.warning(
                "Task %s: auto-stash failed: %s",
                task_id,
                e,
            )

        # Attempt 3: nuclear option — hard-reset and clean everything
        # including ignored files.  Uses git reset --hard HEAD (resets
        # index + working tree for all tracked files) instead of
        # git checkout -- . (which misses staged changes, deleted files,
        # and fails during merge conflicts).
        try:
            clean = await self.git.aforce_clean_workspace(workspace)
            if clean:
                logger.info(
                    "Task %s: force-cleaned workspace on branch '%s'",
                    task_id,
                    current_branch,
                )
                return False
        except Exception as e:
            logger.warning(
                "Task %s: force-clean workspace failed: %s",
                task_id,
                e,
            )

        return True

    async def _reopen_with_verification_feedback(
        self,
        task,
        failures: list[tuple[str, bool]],
    ) -> bool:
        """Reopen a task with git verification feedback.

        Args:
            task: The task to reopen.
            failures: List of (message, fixable) tuples. Only fixable failures
                should be passed here — unfixable ones are handled by the caller.

        Returns True if the task was reopened (transitioned to READY),
        False if max retries were exceeded (task left for caller to block).
        """
        max_retries = self.config.auto_task.max_verification_retries
        # Count previous verification attempts from task_context
        contexts = await self.db.get_task_contexts(task.id)
        retry_count = sum(1 for c in contexts if c.get("type") == "verification_feedback")

        if retry_count >= max_retries:
            logger.warning(
                "Task %s: verification retries exhausted (%d/%d)",
                task.id,
                retry_count,
                max_retries,
            )
            await self._emit_text_notify(
                f"**Verification Failed:** Task `{task.id}` — "
                f"git state is incorrect after {retry_count} retries. "
                f"Manual resolution needed.",
                project_id=task.project_id,
            )
            return False

        # Build feedback message
        bullet_list = "\n".join(f"- {msg}" for msg, _ in failures)
        feedback = (
            f"**Git Verification Feedback (auto-retry "
            f"{retry_count + 1}/{max_retries}):**\n"
            f"The system verified the git state after your work and found "
            f"issues:\n{bullet_list}\n"
            f"Please fix these issues when the task restarts."
        )

        separator = "\n\n---\n"
        updated_description = task.description + separator + feedback

        await self.db.transition_task(
            task.id,
            TaskStatus.READY,
            context="verification_reopen",
            description=updated_description,
            retry_count=0,
            assigned_agent_id=None,
            pr_url=None,
            requires_approval=task.requires_approval,
        )
        await self.db.add_task_context(
            task.id,
            type="verification_feedback",
            label="Git Verification Feedback",
            content=feedback,
        )
        await self._emit_text_notify(
            f"🔄 **Verification reopen:** Task `{task.id}` — "
            f"reopened with feedback (attempt {retry_count + 1}/{max_retries})",
            project_id=task.project_id,
        )
        logger.info(
            "Task %s: reopened for verification (attempt %d/%d)",
            task.id,
            retry_count + 1,
            max_retries,
        )
        return True

    async def _cleanup_workspace_for_next_task(
        self,
        workspace: str | None,
        default_branch: str,
        task_id: str,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Ensure the workspace is clean and on the default branch.

        Called when a task reaches a terminal non-success state (BLOCKED,
        FAILED with retries exhausted) or when a task is reopened for
        verification retry, to prevent dirty workspace state from blocking
        or confusing the next task assigned to this workspace.

        Cleanup strategy (best-effort, most-conservative-first):
        0. Abort any in-progress git operations and remove stale lock files.
        1. Commit any uncommitted changes (preserves work).
        2. Stash if commit fails (preserves work, less accessible).
        3. Force-clean workspace as last resort (reset --hard + clean -fdx).
        4. Switch to the default branch.
        """
        if not workspace:
            return
        try:
            if not await self.git.avalidate_checkout(workspace):
                return
        except Exception:
            return

        # Step 0: Abort any in-progress operations and remove lock files
        # so that subsequent git commands don't fail due to stale state.
        try:
            await self.git.aabort_in_progress_operations(workspace)
        except Exception as e:
            logger.warning(
                "Task %s: abort in-progress operations failed during cleanup: %s",
                task_id,
                e,
            )

        # Step 1: Handle uncommitted changes
        try:
            has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
            if has_uncommitted:
                # Try to commit first (safest — preserves work).
                # Use --no-verify to bypass pre-commit hooks which can
                # reject the commit (e.g. ruff formatting) and prevent
                # workspace cleanup.
                try:
                    await self.git.acommit_all(
                        workspace,
                        f"auto-commit: workspace cleanup after task {task_id}",
                        exclude_plans=False,
                        no_verify=True,
                        event_bus=self.bus,
                        project_id=project_id,
                        agent_id=agent_id,
                    )
                    logger.info(
                        "Task %s: auto-committed changes during workspace cleanup",
                        task_id,
                    )
                except Exception:
                    # Commit failed — try stash (still preserves work)
                    try:
                        await self.git._arun(
                            [
                                "stash",
                                "--include-untracked",
                                "-m",
                                f"auto-stash: workspace cleanup for task {task_id}",
                            ],
                            cwd=workspace,
                        )
                        logger.info(
                            "Task %s: stashed changes during workspace cleanup",
                            task_id,
                        )
                    except Exception:
                        # Last resort — force-clean (reset --hard + clean -fdx)
                        try:
                            clean = await self.git.aforce_clean_workspace(workspace)
                            if clean:
                                logger.info(
                                    "Task %s: force-cleaned workspace during cleanup",
                                    task_id,
                                )
                            else:
                                logger.warning(
                                    "Task %s: force-clean did not fully clean workspace",
                                    task_id,
                                )
                        except Exception as force_err:
                            logger.warning(
                                "Task %s: all cleanup attempts failed: %s",
                                task_id,
                                force_err,
                            )
                            return
        except Exception as e:
            logger.warning(
                "Task %s: failed to check uncommitted changes during cleanup: %s",
                task_id,
                e,
            )

        # Step 2: Switch to default branch
        try:
            current = await self.git.aget_current_branch(workspace)
            if current != default_branch:
                await self.git._arun(["checkout", default_branch], cwd=workspace)
                logger.info(
                    "Task %s: switched to '%s' during workspace cleanup",
                    task_id,
                    default_branch,
                )
        except Exception as e:
            logger.warning(
                "Task %s: failed to switch to '%s' during cleanup: %s",
                task_id,
                default_branch,
                e,
            )

    async def _phase_plan_discover(self, ctx: PipelineContext) -> PhaseResult:
        """Delegate plan discovery to the Supervisor."""
        if not hasattr(self, "_supervisor") or not self._supervisor:
            logger.info(
                "Task %s: no supervisor available, using legacy plan discovery",
                ctx.task.id,
            )
            return await self._phase_plan_generate(ctx)  # Legacy fallback

        logger.info(
            "Task %s: starting plan discovery via supervisor (workspace=%s)",
            ctx.task.id,
            ctx.workspace_path,
        )
        result = await self._supervisor.on_task_completed(
            task_id=ctx.task.id,
            project_id=ctx.task.project_id or "",
            workspace_path=ctx.workspace_path,
        )
        if result and result.get("plan_found"):
            logger.info(
                "Task %s: plan found — will present for approval",
                ctx.task.id,
            )
            ctx.plan_needs_approval = True
            # The supervisor archived the plan file (renamed plan.md →
            # .claude/plans/), which dirties the working tree.  Commit the
            # archival so _phase_verify doesn't see it as uncommitted agent
            # changes and incorrectly reopen the task.
            #
            # Must use exclude_plans=False because the archived files live
            # under .claude/plans/ which is in _PLAN_FILE_EXCLUDES.
            #
            # Must use no_verify=True to bypass pre-commit hooks (e.g. ruff)
            # which can reject the commit and crash the pipeline before
            # _phase_verify even runs — causing the task to be blocked with
            # a misleading "verification failed" error.
            if ctx.workspace_path and await self.git.avalidate_checkout(ctx.workspace_path):
                await self.git.acommit_all(
                    ctx.workspace_path,
                    f"chore: archive plan file\n\nTask-Id: {ctx.task.id}",
                    exclude_plans=False,
                    no_verify=True,
                    event_bus=self.bus,
                    project_id=ctx.task.project_id,
                    agent_id=ctx.agent.id,
                )
        else:
            reason = (
                result.get("reason", "unknown") if isinstance(result, dict) else "non-dict result"
            )
            logger.info(
                "Task %s: no plan found (reason: %s)",
                ctx.task.id,
                reason,
            )
        return PhaseResult.CONTINUE

    async def _phase_plan_generate(self, ctx: PipelineContext) -> PhaseResult:
        """Pipeline phase: discover plan files and store for approval.

        Runs BEFORE merge so the plan file archival is committed to the
        branch before it reaches the default branch.  After archiving the
        plan file, a cleanup commit is made to ensure the deletion of the
        original plan file is in git history.

        Instead of auto-creating subtasks, this phase stores the parsed plan
        data in ``task_context`` and sets ``ctx.plan_needs_approval = True``
        so the caller can transition the task to AWAITING_PLAN_APPROVAL
        and present the plan to the user for approval.
        """
        if not ctx.workspace_path:
            return PhaseResult.CONTINUE
        plan_stored = await self._discover_and_store_plan(ctx.task, ctx.workspace_path)
        # If a plan was stored, the plan file was archived (renamed).
        # Commit the archival so the merge won't carry the plan file to main.
        # Use no_verify=True to bypass pre-commit hooks that could crash the
        # pipeline before _phase_verify runs.
        if plan_stored and ctx.task.branch_name:
            if await self.git.avalidate_checkout(ctx.workspace_path):
                await self.git.acommit_all(
                    ctx.workspace_path,
                    f"chore: archive plan file\n\nTask-Id: {ctx.task.id}",
                    exclude_plans=False,
                    no_verify=True,
                    event_bus=self.bus,
                    project_id=ctx.task.project_id,
                    agent_id=ctx.agent.id,
                )
            ctx.plan_needs_approval = True
        return PhaseResult.CONTINUE

    # ── Approval polling constants ─────────────────────────────────────────
    #
    # These control the behavior of _check_awaiting_approval and its helpers
    # (_handle_awaiting_no_pr, _check_pr_status).  The approval check itself
    # is throttled to once per 60s (see _last_approval_check in __init__).
    #
    # How often (seconds) to re-send reminders for tasks awaiting manual
    # approval (no PR URL).  Prevents notification spam for tasks that
    # legitimately need manual review.
    _NO_PR_REMINDER_INTERVAL: int = 3600  # 1 hour
    # After this many seconds without approval, escalate the notification
    # tone from "awaiting review" to "stuck task" with stronger language.
    _NO_PR_ESCALATION_THRESHOLD: int = 86400  # 24 hours
    # Tasks that don't require approval and have no PR URL are auto-completed
    # after this grace period (seconds).  The grace period avoids a race
    # condition: _complete_workspace transitions the task to AWAITING_APPROVAL
    # before the PR URL is set, and _create_pr_for_task sets the URL shortly
    # after.  Without the grace period, we might auto-complete a task that
    # was about to get a PR URL.
    _NO_PR_AUTO_COMPLETE_GRACE: int = 120  # 2 minutes

    async def _check_awaiting_approval(self) -> None:
        """Poll PR merge status for tasks in AWAITING_APPROVAL. Throttled to once per 60s.

        Two paths:

        * **Tasks with a PR URL** — check whether the PR has been merged
          (complete the task) or closed without merge (block the task and
          alert about orphaned downstream dependents).
        * **Tasks without a PR URL** — either auto-complete them after a
          grace period (if they don't actually require approval, which can
          happen for intermediate plan subtasks), or send periodic reminders
          so they don't rot silently in the queue.
        """
        now = time.time()
        if now - self._last_approval_check < 60:
            return
        self._last_approval_check = now

        tasks = await self.db.list_tasks(status=TaskStatus.AWAITING_APPROVAL)

        # Clean up reminder tracking for tasks that are no longer AWAITING_APPROVAL.
        active_ids = {t.id for t in tasks}
        for tid in list(self._no_pr_reminded_at):
            if tid not in active_ids:
                del self._no_pr_reminded_at[tid]

        for task in tasks:
            if not task.pr_url:
                await self._handle_awaiting_no_pr(task, now)
                continue

            await self._check_pr_status(task)

    async def _handle_awaiting_no_pr(self, task: Task, now: float) -> None:
        """Handle an AWAITING_APPROVAL task that has no PR URL.

        * If the task doesn't actually require approval, auto-complete it after
          a short grace period (avoids a race with slow PR creation).
        * If the task *does* require approval, send periodic reminders so it
          doesn't rot silently.
        """
        updated_at = await self.db.get_task_updated_at(task.id)
        age = (now - updated_at) if updated_at else 0

        # --- Auto-complete path ---------------------------------------------------
        if not task.requires_approval:
            if age >= self._NO_PR_AUTO_COMPLETE_GRACE:
                await self.db.transition_task(
                    task.id, TaskStatus.COMPLETED, context="auto_complete_no_pr"
                )
                await self.db.log_event(
                    "task_completed",
                    project_id=task.project_id,
                    task_id=task.id,
                    payload="auto-completed: no PR and approval not required",
                )
                await self._emit_text_notify(
                    f"**Auto-completed:** Task `{task.id}` — {task.title} "
                    f"(no PR created, approval not required).",
                    project_id=task.project_id,
                )
                self._no_pr_reminded_at.pop(task.id, None)
            return

        # --- Manual-approval path -------------------------------------------------
        last_reminded = self._no_pr_reminded_at.get(task.id, 0.0)
        if now - last_reminded < self._NO_PR_REMINDER_INTERVAL:
            return  # throttle reminders

        self._no_pr_reminded_at[task.id] = now

        if age >= self._NO_PR_ESCALATION_THRESHOLD:
            hours = int(age // 3600)
            await self._emit_text_notify(
                f"⚠️ **Stuck Task:** `{task.id}` — {task.title} has been "
                f"AWAITING_APPROVAL for **{hours}h** with no PR URL.\n"
                f"Use `approve_task {task.id}` to complete it or investigate "
                f"why no PR was created.",
                project_id=task.project_id,
            )
            await self.db.log_event(
                "approval_stuck",
                project_id=task.project_id,
                task_id=task.id,
                payload=f"no_pr_url, age={hours}h",
            )
        else:
            await self._emit_text_notify(
                f"🔍 **Awaiting manual approval:** Task `{task.id}` — "
                f"{task.title}\nNo PR URL — use `approve_task {task.id}` "
                f"to complete.",
                project_id=task.project_id,
            )

    async def _check_pr_status(self, task: Task) -> None:
        """Check whether a PR-backed AWAITING_APPROVAL task has been merged.

        Uses ``GitManager.check_pr_merged()`` (which shells out to ``gh``)
        to determine the PR's current state.  Three outcomes:

        - **True** — PR was merged → task transitions to COMPLETED, and the
          remote task branch is cleaned up.
        - **None** — PR was closed *without* merge → task transitions to
          BLOCKED, and downstream dependents are checked for orphaning.
        - **False** — PR is still open → no action (check again next cycle).

        Requires a valid git checkout path to run ``gh pr view``.  Falls back
        to any workspace associated with the project if the task's own
        workspace has already been released.
        """
        # Need a checkout path to run gh commands
        checkout_path = None
        # Try workspace locked by this task first
        ws = await self.db.get_workspace_for_task(task.id)
        if ws:
            checkout_path = ws.workspace_path
        # Fall back to any workspace for this project
        if not checkout_path:
            workspaces = await self.db.list_workspaces(project_id=task.project_id)
            if workspaces:
                checkout_path = workspaces[0].workspace_path
        if not checkout_path:
            return

        try:
            merged = await self.git.acheck_pr_merged(checkout_path, task.pr_url)
        except Exception as e:
            logger.warning("Error checking PR for task %s: %s", task.id, e)
            return

        if merged is True:
            await self.db.transition_task(task.id, TaskStatus.COMPLETED, context="pr_merged")
            await self.db.log_event("task_completed", project_id=task.project_id, task_id=task.id)
            await self._emit_text_notify(
                f"**PR Merged:** Task `{task.id}` — {task.title} is now COMPLETED.",
                project_id=task.project_id,
            )
            # Clean up the task branch (remote may already be deleted by GitHub)
            if task.branch_name:
                try:
                    await self.git.adelete_branch(
                        checkout_path,
                        task.branch_name,
                        delete_remote=True,
                    )
                except Exception:
                    pass  # branch cleanup is best-effort
        elif merged is None:
            # Closed without merge
            await self.db.transition_task(task.id, TaskStatus.BLOCKED, context="pr_closed")
            await self._emit_task_failure(task, "pr_closed", error="PR was closed without merging")
            await self._emit_text_notify(
                f"**PR Closed:** Task `{task.id}` — {task.title} "
                f"was closed without merging. Marked as BLOCKED.",
                project_id=task.project_id,
            )
            await self._notify_stuck_chain(task)

    # ------------------------------------------------------------------ #
    # Task context assembly helpers
    # ------------------------------------------------------------------ #

    def _get_execution_rules(
        self,
        *,
        task,
        branch_name: str,
        default_branch: str,
        has_remote: bool,
        is_final_subtask: bool,
        requires_approval: bool,
    ) -> str:
        """Return execution rules tailored to the task type and git context.

        Produces three prompt variants:
        A) Normal / final subtask (no approval) — branch, merge to default, push
        B) Requires approval — branch, push, create PR
        C) Intermediate subtask — branch, commit, stay on branch
        """
        # -- Non-git execution rules (shared) --
        if task.is_plan_subtask:
            behaviour = (
                "## Important: Execution Rules\n"
                "You are running autonomously — there is NO interactive user.\n"
                "Do NOT use plan mode or EnterPlanMode.\n"
                "Do NOT write implementation plans or plan files.\n"
                "Your task is one step of an existing implementation plan — "
                "write code, not plans.\n"
                "Implement the changes described below DIRECTLY.\n"
                "If you encounter ambiguity, make reasonable decisions and "
                "document in code comments."
            )
        else:
            behaviour = (
                "## Important: Execution Rules\n"
                "You are running autonomously — there is NO interactive user "
                "to approve plans.\n"
                "Do NOT use plan mode or EnterPlanMode. "
                "Implement the changes DIRECTLY.\n"
                "If the task description contains a plan, execute it "
                "immediately — do not re-plan."
            )

        # -- Git workflow rules (varies by scenario) --
        if not branch_name:
            # No branch assigned (e.g. no repo configured) — skip git rules
            git_rules = ""
        elif task.is_plan_subtask and not is_final_subtask:
            # Intermediate subtask: commit on shared branch, don't merge
            git_rules = (
                f"\n\n## Important: Git Workflow (Subtask — more steps follow)\n"
                f"Shared branch: `{branch_name}`. "
                f"Default branch: `{default_branch}`.\n"
                f"\nWhen you start:\n"
                f"1. Switch to the task branch: `git checkout {branch_name}`\n"
                f"   If it does not exist yet: "
                f"`git checkout -b {branch_name}`\n"
                f"\nWhen you finish:\n"
                f"1. `git add` the files you changed\n"
                f"2. `git commit` with a descriptive message\n"
                f"3. Stay on `{branch_name}` — do NOT merge to "
                f"`{default_branch}`. A later subtask handles the final merge."
            )
        elif requires_approval:
            # PR workflow: push branch, create PR, don't merge
            push_cmd = f"`git push origin {branch_name}`"
            pr_cmd = (
                f"`gh pr create --base {default_branch} "
                f"--head {branch_name} "
                f'--title "<descriptive title>" '
                f'--body "<summary of changes>"`'
            )
            git_rules = (
                f"\n\n## Important: Git Workflow (PR Required)\n"
                f"Default branch: `{default_branch}`. "
                f"Task branch: `{branch_name}`.\n"
                f"\nWhen you start:\n"
                f"1. `git checkout -b {branch_name}` "
                f"(or `git checkout {branch_name}` if it already exists)\n"
                f"\nWhen you finish (if you made code changes):\n"
                f"1. Commit all remaining changes on `{branch_name}`\n"
                f"2. Push your branch: {push_cmd}\n"
                f"3. Create a pull request: {pr_cmd}\n"
                f"4. Stay on `{branch_name}` — do NOT merge to "
                f"`{default_branch}`\n"
                f"\nIf this task requires NO code changes "
                f"(research, analysis, investigation):\n"
                f"- Do not create a branch. Stay on `{default_branch}`.\n"
                f"- Do not create a PR."
            )
        else:
            # Normal task / final subtask: merge to default + push
            push_line = f"4. `git push origin {default_branch}`\n" if has_remote else ""
            delete_step = "5" if has_remote else "4"
            git_rules = (
                f"\n\n## Important: Git Workflow\n"
                f"Default branch: `{default_branch}`. "
                f"Task branch: `{branch_name}`.\n"
                f"\nWhen you start:\n"
                f"1. `git checkout -b {branch_name}` "
                f"(or `git checkout {branch_name}` if it already exists)\n"
                f"\nWhen you finish (if you made code changes):\n"
                f"1. Commit all remaining changes on `{branch_name}`\n"
                f"2. `git checkout {default_branch}`\n"
                f"3. `git merge {branch_name}` — resolve any conflicts "
                f"during the merge\n"
                f"{push_line}"
                f"{delete_step}. "
                f"`git branch -d {branch_name}`\n"
                f"\nIf this task requires NO code changes "
                f"(research, analysis, investigation):\n"
                f"- Do not create a branch. Stay on `{default_branch}`.\n"
                f"\nIf you encounter merge conflicts:\n"
                f"- Resolve them during the merge step. You wrote the code — "
                f"you are best positioned to resolve conflicts.\n"
                f"- After resolving, complete the merge commit"
                f"{' and push' if has_remote else ''}."
            )

        # -- Plan-writing rules (root tasks only) --
        plan_rules = ""
        if not task.is_plan_subtask:
            plan_rules = (
                "\n\n## CRITICAL: Writing Implementation Plans\n"
                "Most tasks do NOT require writing a plan — just implement "
                "the changes directly.\n"
                "Only write a plan if the task explicitly asks you to create "
                "an implementation plan,\n"
                "investigate and propose changes, or produce a multi-step "
                "strategy for follow-up work.\n"
                "\n"
                "If you DO need to write a plan, you MUST follow these rules "
                "exactly:\n"
                "1. Write the plan to **`.claude/plan.md`** in the workspace "
                "root (preferred)\n"
                "   or `plan.md` — these are the ONLY locations the system "
                "checks first\n"
                "2. Do NOT write plans to `notes/`, `docs/`, or any other "
                "directory — plans\n"
                "   written elsewhere may not be detected for automatic task "
                "splitting\n"
                "3. Name each implementation phase clearly: "
                "`## Phase 1: <title>`,\n"
                "   `## Phase 2: <title>`, etc.\n"
                "4. Put ALL background/reference material (design specs, "
                "constraints,\n"
                "   architecture notes) BEFORE the phase headings, NOT as "
                "separate phases\n"
                "5. Keep each phase focused on a single actionable "
                "implementation step\n"
                "6. If you implement the plan yourself (i.e., you both plan "
                "AND execute the work\n"
                "   in a single task), DELETE the plan file before completing. "
                "Only leave a plan\n"
                "   file in the workspace if you want the system to create "
                "follow-up tasks from it.\n"
                "   Alternatively, add `auto_tasks: false` to the plan's YAML "
                "frontmatter.\n"
                "\n"
                "NOTE: Any plan file left in the workspace when your task "
                "completes will be\n"
                "automatically parsed and converted into follow-up subtasks. "
                "If you already\n"
                "did the work described in the plan, this creates "
                "duplicate/unnecessary tasks.\n"
                "\n"
                "This is required for the system to automatically split your "
                "plan into\n"
                "follow-up tasks. Plans that mix reference sections with "
                "implementation\n"
                "phases will produce low-quality task splits."
            )

        return behaviour + git_rules + plan_rules

    async def _build_task_context_with_prompt_builder(
        self,
        task,
        workspace: str,
        project,
        profile,
    ) -> str:
        """Build the task description using PromptBuilder.

        Replaces the inline string concatenation that was in _execute_task().
        """
        from src.prompt_builder import PromptBuilder

        builder = PromptBuilder(project_id=task.project_id)

        # System metadata
        sys_parts = [f"- Workspace directory: {workspace}"]
        if project:
            sys_parts.append(f"- Project: {project.name} (id: {project.id})")
            repo_url = project.repo_url
            # Auto-detect repo_url from git remote if not set
            if not repo_url:
                try:
                    detected = await self.git.aget_remote_url(workspace)
                    if detected:
                        repo_url = detected
                        await self.db.update_project(project.id, repo_url=repo_url)
                except Exception:
                    pass  # Non-fatal
            if repo_url:
                sys_parts.append(f"- Repository URL: {repo_url}")
            if project.repo_default_branch:
                sys_parts.append(f"- Default branch: {project.repo_default_branch}")
        if task.branch_name:
            sys_parts.append(f"- Git branch: {task.branch_name}")
        builder.add_context("system_context", "## System Context\n" + "\n".join(sys_parts))

        # Execution rules — parameterized by git context
        default_branch = await self._get_default_branch(project, workspace)
        has_remote = (
            await self.git.ahas_remote(workspace)
            if await self.git.avalidate_checkout(workspace)
            else False
        )
        is_final = (not task.is_plan_subtask) or await self._is_last_subtask(task)
        if task.is_plan_subtask and task.parent_task_id:
            parent = await self.db.get_task(task.parent_task_id)
            needs_approval = parent.requires_approval if parent else task.requires_approval
        else:
            needs_approval = task.requires_approval
        builder.add_context(
            "execution_rules",
            self._get_execution_rules(
                task=task,
                branch_name=task.branch_name or "",
                default_branch=default_branch,
                has_remote=has_remote,
                is_final_subtask=is_final,
                requires_approval=needs_approval,
            ),
        )

        # Upstream dependency summaries
        dep_ids = await self.db.get_dependencies(task.id)
        if dep_ids:
            dep_sections = []
            for dep_id in sorted(dep_ids):
                dep_task = await self.db.get_task(dep_id)
                dep_result = await self.db.get_task_result(dep_id)
                if not dep_task or not dep_result:
                    continue
                title = dep_task.title or dep_id
                summary = dep_result.get("summary") or "(no summary recorded)"
                if len(summary) > 2000:
                    summary = summary[:2000] + "... [truncated]"
                files = dep_result.get("files_changed") or []
                section = f"### {title}\n**Summary:** {summary}"
                if files:
                    file_list = "\n".join(f"  - `{f}`" for f in files)
                    section += f"\n**Files changed:**\n{file_list}"
                dep_sections.append(section)
            if dep_sections:
                builder.add_context(
                    "upstream_work",
                    "## Completed Upstream Work\n"
                    "The following tasks were direct dependencies of your task "
                    "and have already been completed:\n\n" + "\n\n".join(dep_sections),
                )

        # L0 Identity tier and L1 Critical Facts tier are now injected via
        # TaskContext fields (l0_role, l1_facts) and handled by the adapter's
        # prompt builder.  See Roadmap 3.3.5.
        #
        # Project-specific override — freeform English that supplements or tweaks
        # the base profile for this project.  Injected right after L0 role so the
        # LLM sees base profile + override together.
        # See docs/specs/design/memory-scoping.md §5.
        if profile and task.project_id:
            override_text = await self._load_project_override(task.project_id, profile.id)
            if override_text:
                builder.set_override_content(override_text)

        # Task description
        builder.add_context("task", f"## Task\n{task.description}")

        # Conversation thread context — if the supervisor stored the
        # conversation that led to this task's creation, include it so
        # the agent understands the user's intent and broader context.
        try:
            contexts = await self.db.get_task_contexts(task.id)
            conv_ctx = next((c for c in contexts if c["type"] == "conversation_context"), None)
            if conv_ctx and conv_ctx["content"]:
                builder.add_context(
                    "additional_context",
                    "## Additional Context\n"
                    "The following is the conversation thread between the user and "
                    "the supervisor that led to the creation of this task. Use it to "
                    "understand the user's intent, any clarifications, and the "
                    "broader goal behind the task:\n\n" + conv_ctx["content"],
                )
        except Exception:
            pass  # Non-fatal — proceed without conversation context

        return builder.build_task_prompt()

    async def _load_project_override(
        self,
        project_id: str,
        agent_type: str,
    ) -> str | None:
        """Load override ``.md`` file content for a project + agent type.

        Override files live at
        ``vault/projects/{project_id}/overrides/{agent_type}.md``.
        Returns the raw file content (including frontmatter — the caller
        is responsible for stripping it), or ``None`` if no override exists.

        See ``docs/specs/design/memory-scoping.md`` §5 for the override model.
        """
        override_path = os.path.join(
            self.config.vault_root,
            "projects",
            project_id,
            "overrides",
            f"{agent_type}.md",
        )
        if not os.path.isfile(override_path):
            return None
        try:
            with open(override_path, encoding="utf-8") as f:
                content = f.read()
            if content.strip():
                logger.info(
                    "Loaded override for project=%s agent_type=%s from %s",
                    project_id,
                    agent_type,
                    override_path,
                )
                return content
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "Failed to load override for project=%s agent_type=%s: %s",
                project_id,
                agent_type,
                exc,
            )
        return None

    # ------------------------------------------------------------------ #
    # Sync Workflow — orchestrator-managed workspace synchronization
    # ------------------------------------------------------------------ #

    async def _execute_sync_workflow(
        self,
        action: AssignAction,
        task: Task,
        agent,
    ) -> None:
        """Orchestrator-managed sync workflow (bypasses normal agent execution).

        This method handles tasks with ``task_type=SYNC``.  Instead of
        launching a regular agent, it coordinates a multi-phase workflow:

        1. **Pause project** — prevent new tasks from being queued.
        2. **Wait for active tasks** — poll until all IN_PROGRESS tasks
           (other than this sync task) have completed.
        3. **Merge feature branches** — acquire a workspace and launch a
           Claude Code agent to merge all feature branches into the default
           branch across all project workspaces.
        4. **Cleanup & resume** — release workspaces, resume the project.
        """
        project = await self.db.get_project(action.project_id)
        if not project:
            logger.error("Sync workflow: project %s not found", action.project_id)
            await self.db.transition_task(
                action.task_id, TaskStatus.FAILED, context="project_not_found"
            )
            await self._emit_task_failure(
                task, "project_not_found", error=f"Project {action.project_id} not found"
            )
            await self.db.update_agent(action.agent_id, state=AgentState.IDLE, current_task_id=None)
            return

        default_branch = project.repo_default_branch or "main"
        workspaces = await self.db.list_workspaces(project_id=action.project_id)
        merge_succeeded = False  # Track merge outcome for final status

        async def _notify(msg: str) -> None:
            await self._emit_text_notify(msg, project_id=action.project_id)

        await _notify(
            f"🔄 **Sync Workspaces started:** `{task.id}`\n"
            f"Phase 1/4 — Pausing project `{action.project_id}`…"
        )

        # ── Phase 1: Pause the project ──────────────────────────────────
        await self.db.update_project(action.project_id, status=ProjectStatus.PAUSED)
        logger.info("Sync workflow %s: project %s paused", task.id, action.project_id)

        try:
            # ── Phase 2: Wait for active tasks ──────────────────────────
            await _notify(
                f"🔄 **Sync `{task.id}`** — Phase 2/4: Waiting for active tasks to complete…"
            )

            max_wait = 3600  # 1 hour max wait
            poll_interval = 10  # seconds between checks
            waited = 0

            while waited < max_wait:
                active_tasks = await self.db.list_active_tasks(
                    project_id=action.project_id,
                    exclude_statuses={TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED},
                )
                # Filter out this sync task itself and tasks that aren't running
                running = [
                    t
                    for t in active_tasks
                    if t.id != task.id and t.status in (TaskStatus.IN_PROGRESS, TaskStatus.ASSIGNED)
                ]

                if not running:
                    break

                if waited % 60 == 0 and waited > 0:  # Log progress every minute
                    running_ids = ", ".join(f"`{t.id}`" for t in running[:5])
                    await _notify(
                        f"🔄 **Sync `{task.id}`** — Still waiting for "
                        f"{len(running)} task(s): {running_ids}"
                    )

                await asyncio.sleep(poll_interval)
                waited += poll_interval

            if waited >= max_wait:
                await _notify(
                    f"⚠️ **Sync `{task.id}`** — Timed out waiting for active tasks "
                    f"after {max_wait}s. Aborting sync."
                )
                await self.db.transition_task(
                    action.task_id, TaskStatus.FAILED, context="sync_timeout_waiting_for_tasks"
                )
                await self._emit_task_failure(
                    task,
                    "sync_timeout_waiting_for_tasks",
                    error=f"Timed out waiting for active tasks after {max_wait}s",
                )
                return

            logger.info("Sync workflow %s: all active tasks completed", task.id)

            # ── Early-out: check if workspaces are already synced ───────
            # After waiting for active tasks, re-check whether any workspace
            # actually has feature branches.  If all workspaces are already
            # on the default branch with no feature branches, skip the
            # expensive merge agent entirely.
            needs_merge = False
            for ws in workspaces:
                ws_path = ws.workspace_path
                if not os.path.isdir(ws_path):
                    continue  # skip missing paths
                try:
                    current = await self.git.aget_current_branch(ws_path)
                    branches = await self.git.alist_branches(ws_path)
                    clean_branches = [b.lstrip("* ").strip() for b in branches if b.strip()]
                    non_default = [b for b in clean_branches if b and b != default_branch]
                    if current != default_branch or non_default:
                        needs_merge = True
                        break
                except Exception:
                    # Can't check — assume merge is needed
                    needs_merge = True
                    break

            if not needs_merge:
                logger.info(
                    "Sync workflow %s: all workspaces already on %s — skipping merge",
                    task.id,
                    default_branch,
                )
                await self.db.transition_task(
                    action.task_id,
                    TaskStatus.COMPLETED,
                    context="sync_already_synced",
                )
                await _notify(
                    f"✅ **Sync `{task.id}`** — All workspaces already on "
                    f"`{default_branch}` with no feature branches. Skipping merge."
                )
                # Jump to cleanup (the finally block handles resume).
                # Task is already COMPLETED, so the finally block's
                # IN_PROGRESS check won't send a duplicate notification.
                return

            # ── Phase 3: Merge feature branches ─────────────────────────
            await _notify(
                f"🔄 **Sync `{task.id}`** — Phase 3/4: "
                f"Merging feature branches into `{default_branch}`…"
            )

            # Gather workspace info for the merge task description.
            workspace_info_lines = []
            for ws in workspaces:
                workspace_info_lines.append(
                    f"  - Path: {ws.workspace_path} (id: {ws.id}, name: {ws.name or '—'})"
                )
            workspace_info = "\n".join(workspace_info_lines)

            merge_description = f"""## Merge All Feature Branches — {action.project_id}

You are a workspace synchronization agent. Your job is to merge ALL feature
branches into the default branch (`{default_branch}`) across all project workspaces,
then ensure every workspace is on `{default_branch}` with no remaining feature branches.

### Project Workspaces (absolute paths):
{workspace_info}

### Default Branch: `{default_branch}`

### Instructions:

For EACH workspace listed above, perform these steps IN ORDER:

1. **Navigate to the workspace directory** using the absolute path above.
2. **Fetch latest changes**: `git fetch origin`
3. **Identify all local branches**: `git branch` — look for any branches other than `{default_branch}`.
4. **Checkout the default branch**: `git checkout {default_branch} && git pull origin {default_branch}`
5. **For each feature branch** (branches that are NOT `{default_branch}`):
   a. Ensure the feature branch has all its changes committed and pushed.
   b. Merge the feature branch into `{default_branch}`:
      `git merge <feature-branch> --no-ff -m "Merge <feature-branch> into {default_branch}"`
   c. If there are merge conflicts, resolve them intelligently:
      - Prefer to preserve feature functionality
      - Keep both changes when possible
      - If truly duplicated code, keep the more complete version
   d. After successful merge, delete the feature branch:
      `git branch -d <feature-branch>`
      `git push origin --delete <feature-branch>` (if it exists on remote)
6. **Push the updated default branch**: `git push origin {default_branch}`
7. **Pull updates before moving to the next workspace** to ensure each subsequent
   workspace has the latest merged changes.

### CRITICAL Rules:
- Process workspaces ONE AT A TIME, in the order listed above
- ALWAYS pull the latest `{default_branch}` before starting work on each workspace
- Preserve ALL feature work unless it's genuinely duplicated or made irrelevant
- After completion, every workspace should be on `{default_branch}` with no feature branches
- If a workspace has no feature branches, just ensure it's on `{default_branch}` and up to date
- Commit and push after merging each feature branch (don't batch)

### Error Handling:
- If a merge has unresolvable conflicts, document what you tried and move on
- If a workspace path doesn't exist or isn't a git repo, skip it and report the issue
- Always try to complete as many workspaces as possible even if some fail
"""

            # Acquire a workspace for the merge agent to execute in.
            # All workspaces should be free since we waited for active tasks.
            workspace = None
            try:
                workspace = await self._prepare_workspace(task, agent)
            except Exception as e:
                logger.error("Sync workflow %s: workspace prep failed: %s", task.id, e)

            if not workspace:
                # Try to find any workspace path to use as working dir
                if workspaces:
                    workspace = workspaces[0].workspace_path
                    logger.warning(
                        "Sync workflow %s: using fallback workspace %s", task.id, workspace
                    )
                else:
                    await _notify(
                        f"❌ **Sync `{task.id}`** — No workspace available for merge agent."
                    )
                    await self.db.transition_task(
                        action.task_id, TaskStatus.FAILED, context="no_workspace_for_merge"
                    )
                    await self._emit_task_failure(
                        task,
                        "no_workspace_for_merge",
                        error="No workspace available for merge agent",
                    )
                    return

            # Launch the Claude Code agent for the merge work.
            if not self._adapter_factory:
                await _notify(f"❌ **Sync `{task.id}`** — No adapter factory configured.")
                await self.db.transition_task(
                    action.task_id, TaskStatus.FAILED, context="no_adapter_factory"
                )
                await self._emit_task_failure(
                    task, "no_adapter_factory", error="No agent adapter factory configured"
                )
                return

            profile = await self._resolve_profile(task)
            adapter = self._adapter_factory.create("claude", profile=profile)
            self._adapters[action.agent_id] = adapter

            ctx = TaskContext(
                task_id=task.id,
                description=merge_description,
                checkout_path=workspace,
                branch_name=default_branch,
            )

            # Create a thread for streaming output via event.
            start_msg = (
                f"🔄 **Sync Workspaces** — Merge agent starting\n"
                f"Task: `{task.id}` | Agent: {agent.name}"
            )
            thread_name = f"{task.id} | Sync Workspaces"[:100]
            await self._emit_notify(
                "notify.task_thread_open",
                TaskThreadOpenEvent(
                    task_id=task.id,
                    thread_name=thread_name,
                    initial_message=start_msg,
                    project_id=action.project_id,
                ),
            )

            await adapter.start(ctx)

            # Stream agent output via event.
            async def forward_msg(text: str) -> None:
                await self._emit_notify(
                    "notify.task_message",
                    TaskMessageEvent(
                        task_id=task.id,
                        message=text,
                        message_type="agent_output",
                        project_id=action.project_id,
                    ),
                )

            output = await adapter.wait(on_message=forward_msg)

            # Record token usage.
            if output.tokens_used > 0:
                await self.db.record_token_usage(
                    project_id=action.project_id,
                    task_id=task.id,
                    agent_id=action.agent_id,
                    tokens=output.tokens_used,
                )

            merge_succeeded = output.result == AgentResult.COMPLETED
            if merge_succeeded:
                logger.info("Sync workflow %s: merge completed successfully", task.id)
            else:
                logger.warning("Sync workflow %s: merge agent result=%s", task.id, output.result)
                await _notify(
                    f"⚠️ **Sync `{task.id}`** — Merge agent finished with "
                    f"result: {output.result.value}. Proceeding to cleanup."
                )

        finally:
            # ── Phase 4: Cleanup & Resume ───────────────────────────────
            await _notify(f"🔄 **Sync `{task.id}`** — Phase 4/4: Cleanup & resume…")

            # Release all workspace locks for this project (belt and suspenders).
            for ws in workspaces:
                if ws.locked_by_task_id == task.id:
                    try:
                        await self.db.release_workspace(ws.id)
                    except Exception:
                        pass

            # Also release via standard task cleanup.
            await self.db.release_workspaces_for_task(action.task_id)

            # Resume the project.
            await self.db.update_project(action.project_id, status=ProjectStatus.ACTIVE)
            logger.info("Sync workflow %s: project %s resumed", task.id, action.project_id)

            # Free the agent.
            post_agent = await self.db.get_agent(action.agent_id)
            next_state = (
                AgentState.PAUSED
                if post_agent and post_agent.state == AgentState.PAUSED
                else AgentState.IDLE
            )
            await self.db.update_agent(action.agent_id, state=next_state, current_task_id=None)

            # Remove adapter reference.
            self._adapters.pop(action.agent_id, None)

            # Complete or fail the sync task.
            # Check if we got here via the normal path or an exception.
            try:
                current_task = await self.db.get_task(task.id)
                if current_task and current_task.status == TaskStatus.IN_PROGRESS:
                    if merge_succeeded:
                        await self.db.transition_task(
                            action.task_id, TaskStatus.COMPLETED, context="sync_completed"
                        )
                        await _notify(
                            f"✅ **Sync Workspaces completed:** `{task.id}` — "
                            f"All workspaces synchronized to `{default_branch}`."
                        )
                    else:
                        await self.db.transition_task(
                            action.task_id,
                            TaskStatus.COMPLETED,
                            context="sync_completed_with_warnings",
                        )
                        await _notify(
                            f"⚠️ **Sync Workspaces finished:** `{task.id}` — "
                            f"Completed with warnings. Check thread for details."
                        )
            except Exception as e:
                logger.error("Sync workflow %s: final status update failed: %s", task.id, e)

    async def _execute_task(self, action: AssignAction) -> None:
        """The full task execution pipeline (layer 3 of 3), run as a background asyncio task.

        This is the core method that drives a single task from assignment to
        completion.  It runs as an ``asyncio.Task`` concurrently with the main
        loop, so multiple tasks can execute in parallel (one per agent).

        Steps:
        1. **Assign** — mark task IN_PROGRESS and agent BUSY in the DB.
        2. **Workspace setup** — clone/link/init the repo, create or switch
           to the task branch (see ``_prepare_workspace``).  If no workspace
           is available, the task is returned to READY for retry next cycle.
        3. **Agent context assembly** — build a structured markdown prompt
           containing: system metadata (workspace, project, branch), execution
           rules (subtask vs. root behavior, commit/rebase instructions),
           upstream dependency results (for chain continuity), optional agent
           profile role instructions, and the task description itself.  This
           prompt is what the agent "sees" as its assignment.
        4. **Memory recall** — if the memory subsystem is enabled, inject
           semantically relevant historical context (past task results,
           project notes) into the agent's context.  Non-fatal on failure.
        5. **Agent launch** — create an adapter (e.g. ClaudeAdapter), pass
           the assembled ``TaskContext``, and start the agent process.
        6. **Stream + wait** — forward agent output messages to the Discord
           thread in real time.  Detect ``AskUserQuestion`` tool use in the
           stream and send dedicated rich notifications.  If the agent hits
           a rate limit, an exponential-backoff retry loop re-initializes
           and retries (up to ``rate_limit_max_retries``) before giving up.
        7. **Token accounting** — record tokens used, check budget warnings.
        8. **Result handling** — branch on the ``AgentResult`` enum:
           - COMPLETED: run ``_complete_workspace`` (commit/merge/PR), then
             ``_discover_and_store_plan`` to detect plan files for approval,
             then save to memory for future task context.
           - FAILED: increment retry count; if exhausted, mark BLOCKED and
             notify about orphaned downstream tasks.
           - PAUSED (rate limit or tokens): set a ``resume_after`` timestamp
             so the task is automatically retried after the backoff.
           - WAITING_INPUT: transition to WAITING_INPUT and send a question
             notification with interactive response buttons.
        9. **Cleanup** — release workspace lock, free agent (IDLE or PAUSED
           if admin-paused mid-execution), remove adapter reference.
        """
        if not self._adapter_factory:
            logger.error("Cannot execute task %s: no adapter factory configured", action.task_id)
            await self._emit_text_notify(
                f"**Error:** Cannot execute task `{action.task_id}` — no agent adapter configured.",
                project_id=action.project_id,
            )
            return

        # Assign
        await self.db.assign_task_to_agent(action.task_id, action.agent_id)

        # Start agent
        await self.db.transition_task(
            action.task_id, TaskStatus.IN_PROGRESS, context="agent_started"
        )
        await self.db.update_agent(action.agent_id, state=AgentState.BUSY)

        task = await self.db.get_task(action.task_id)
        agent = await self.db.get_agent(action.agent_id)
        await self._emit_task_event("task.started", task, agent_id=action.agent_id)

        # ── Sync workflow interception ───────────────────────────────────
        # Sync tasks (task_type=SYNC) are orchestrator-managed workflows,
        # not regular agent tasks.  They coordinate: pause project → wait
        # for active tasks → launch merge agent → resume project.
        if task.task_type == TaskType.SYNC:
            await self._execute_sync_workflow(action, task, agent)
            return

        # Prepare workspace (repo checkout/worktree/init)
        project = await self.db.get_project(action.project_id)
        try:
            workspace = await self._prepare_workspace(task, agent)
        except Exception as e:
            await self._emit_text_notify(
                f"**Workspace Error:** Task `{task.id}` — {e}",
                project_id=action.project_id,
            )
            workspace = None

        if not workspace:
            # No workspace available — PAUSE the task with a backoff timer
            # instead of returning to READY.  Returning to READY causes an
            # infinite assign→fail→READY→assign loop that spams Discord every
            # orchestrator cycle (~5s).  PAUSED + resume_after lets
            # _resume_paused_tasks() promote it back to READY after a delay,
            # giving time for workspaces to free up.
            no_ws_backoff = 60  # seconds before retrying workspace acquisition
            await self.db.transition_task(
                action.task_id,
                TaskStatus.PAUSED,
                context="no_workspace_available",
                resume_after=time.time() + no_ws_backoff,
            )
            await self._emit_task_event(
                "task.paused",
                task,
                reason="no_workspace",
                resume_after=time.time() + no_ws_backoff,
            )
            await self.db.update_agent(action.agent_id, state=AgentState.IDLE)
            await self._emit_text_notify(
                f"**No Workspace:** Task `{task.id}` paused for "
                f"{no_ws_backoff}s — project `{action.project_id}` has no "
                f"available workspaces. Use `/add-workspace` to create one.",
                project_id=action.project_id,
            )
            return

        # Re-fetch task/agent in case _prepare_workspace updated them
        task = await self.db.get_task(action.task_id)
        agent = await self.db.get_agent(action.agent_id)

        # Fetch the workspace object for display in notifications
        ws_obj = await self.db.get_workspace_for_task(task.id)

        # Detect whether this is a reopened task (via thread feedback) so we
        # can suppress noisy main-channel notifications for reopened work.
        _is_reopened = False
        contexts: list[dict] = []
        try:
            contexts = await self.db.get_task_contexts(task.id)
            _is_reopened = any(
                c.get("type") in ("reopen_feedback", "thread_feedback") for c in contexts
            )
        except Exception:
            pass

        # Notify that work is starting via typed event.
        # The DiscordNotificationHandler stores the returned message for
        # later deletion and handles embed/view creation.
        start_msg = format_task_started(task, agent, workspace=ws_obj)
        if not _is_reopened:
            await self._emit_notify(
                "notify.task_started",
                TaskStartedEvent(
                    task=build_task_detail(task),
                    agent=build_agent_summary(agent),
                    workspace_path=ws_obj.workspace_path if ws_obj else workspace,
                    workspace_name=(ws_obj.name or "") if ws_obj else "",
                    is_reopened=False,
                    task_description=task.description or "",
                    task_contexts=contexts if contexts else None,
                    project_id=action.project_id,
                ),
            )

        # Delete the task-added notification from Discord to reduce chat
        # clutter — the task-started message supersedes it.
        added_msg = self._task_added_messages.pop(task.id, None)
        if added_msg is not None:
            try:
                await added_msg.delete()
            except Exception as e:
                logger.debug("Could not delete task-added message for %s: %s", task.id, e)

        # Open a thread for streaming agent output via event.
        # The notification handler creates the thread and stores callbacks
        # internally, keyed by task_id.  Subsequent task_message events
        # are routed to the correct thread automatically.
        thread_name = f"{task.id} | {task.title}"[:100]
        await self._emit_notify(
            "notify.task_thread_open",
            TaskThreadOpenEvent(
                task_id=task.id,
                thread_name=thread_name,
                initial_message=start_msg,
                project_id=action.project_id,
            ),
        )

        # Resolve the agent profile (task-level → project-level → system default)
        # and create an adapter instance.  The profile controls model selection,
        # tool allowlists, MCP servers, and system prompt augmentation.
        # See ``_resolve_profile`` for the fallback chain.
        profile = await self._resolve_profile(task)
        if profile:
            logger.info(
                "Task %s: profile='%s' tools=%s mcp=%s",
                task.id,
                profile.id,
                profile.allowed_tools or "(default)",
                list(profile.mcp_servers.keys()) if profile.mcp_servers else "(none)",
            )
        else:
            logger.info("Task %s: no profile (using system defaults)", task.id)
        adapter = self._adapter_factory.create("claude", profile=profile)
        # Store adapter reference so admin commands (stop_task, timeout handler)
        # can call adapter.stop() to terminate the agent process.
        self._adapters[action.agent_id] = adapter

        # ------------------------------------------------------------------ #
        # Build the agent's system context prompt.
        #
        # The context is assembled via PromptBuilder as named markdown
        # sections injected as the first part of the task description sent
        # to the adapter.  It includes:
        #   - System metadata (workspace path, project, branch)
        #   - Execution rules (subtask vs. root task behavior)
        #   - Upstream dependency results (so the agent has continuity)
        #   - Profile-specific role instructions (if a profile is set)
        #   - The actual task description (appended last)
        #
        # The prompt is intentionally opinionated: it tells the agent NOT to
        # use plan mode, to commit its work, and to rebase before starting.
        # These instructions are critical for the orchestrator's post-
        # completion git workflow to function correctly.
        # ------------------------------------------------------------------ #
        full_description = await self._build_task_context_with_prompt_builder(
            task, workspace, project, profile
        )

        # ------------------------------------------------------------------ #
        # L0 Identity tier and L1 Critical Facts tier.
        #
        # These are computed here and passed as first-class TaskContext fields
        # so the adapter can inject them at the correct position in its own
        # PromptBuilder (L0 → L1 → description).  They are *always* present
        # at task start — see docs/specs/design/memory-scoping.md §2.
        # ------------------------------------------------------------------ #
        l0_role = ""
        if profile and profile.system_prompt_suffix:
            l0_role = profile.system_prompt_suffix.strip()

        l1_facts = ""
        if self._memory_v2_service:
            try:
                l1_text = await self._memory_v2_service.load_l1_facts(
                    project_id=task.project_id,
                    agent_type=profile.id if profile else None,
                )
                if l1_text:
                    l1_facts = l1_text
            except Exception as e:
                logger.warning("L1 facts injection failed for task %s: %s", task.id, e)

        # Merge MCP servers: start with the daemon's own MCP server (if
        # inject_into_tasks is enabled), then layer profile-specific servers
        # on top.  Profile servers win on name collisions.
        task_mcp: dict[str, dict] = dict(self.config.mcp_server.task_mcp_entry())
        if profile and profile.mcp_servers:
            task_mcp.update(profile.mcp_servers)

        ctx = TaskContext(
            task_id=task.id,
            description=full_description,
            l0_role=l0_role,
            l1_facts=l1_facts,
            checkout_path=workspace,
            branch_name=task.branch_name or "",
            image_paths=task.attachments if task.attachments else [],
            mcp_servers=task_mcp,
        )

        # On reopened tasks, pass the previous session ID so the adapter can
        # fork the session and give the agent full prior context.
        if _is_reopened:
            try:
                prev_session = await self.db.get_task_meta(task.id, "last_session_id")
                if prev_session:
                    ctx.resume_session_id = prev_session
                    logger.info(
                        "Task %s: reopened — will fork session %s",
                        task.id,
                        prev_session,
                    )
            except Exception as e:
                logger.warning("Task %s: failed to look up session_id: %s", task.id, e)

        # Memory recall: inject relevant historical context from memsearch.
        # Uses the enhanced tiered context (profile → notes → recent tasks →
        # search results) when available, falling back to legacy flat recall.
        # Failures are non-fatal.
        if self.memory_manager:
            try:
                memory_block = await self._build_memory_context_block(task, workspace)
                if memory_block:
                    ctx.attached_context.append(memory_block)
            except Exception as e:
                logger.warning("Memory recall failed for task %s: %s", task.id, e)

        # Record execution start time so _discover_and_store_plan() can
        # detect stale plan files that predate this task's agent execution.
        self._task_exec_start[action.task_id] = time.time()

        await adapter.start(ctx)

        # ------------------------------------------------------------------ #
        # Agent message streaming and question detection.
        #
        # The adapter's ``wait(on_message=...)`` callback fires for each
        # chunk of agent output.  We forward these to the Discord thread
        # (created above) so humans can watch agent progress in real time.
        #
        # We also watch for the AskUserQuestion tool marker in the stream.
        # When detected, a dedicated rich notification is sent to the
        # project's notifications channel with an interactive view, because
        # thread messages alone are easy to miss.  The ``_question_notified``
        # flag deduplicates: at most one question notification per task run.
        # ------------------------------------------------------------------ #
        _question_notified = False

        async def forward_agent_message(text: str) -> None:
            nonlocal _question_notified
            # Stream agent output via event — the notification handler
            # routes to the task's thread if one exists, otherwise to the
            # main channel.
            await self._emit_notify(
                "notify.task_message",
                TaskMessageEvent(
                    task_id=task.id,
                    message=text,
                    message_type="agent_output",
                    project_id=action.project_id,
                ),
            )

            # Detect agent questions — the Claude adapter formats
            # AskUserQuestion tool use as "**[AskUserQuestion...]**".
            # When detected, send a dedicated rich notification.
            if not _question_notified and "**[AskUserQuestion" in text:
                _question_notified = True
                # Extract the question text from the message.  The
                # full question details follow the tool-use marker in
                # subsequent lines; use the entire text as context.
                question_text = text.replace("**[AskUserQuestion]**", "").strip()
                if not question_text:
                    question_text = (
                        "(Agent is requesting user input — check the task thread for details.)"
                    )
                try:
                    await self._notify_agent_question(
                        task,
                        agent,
                        question_text,
                        project_id=action.project_id,
                    )
                except Exception as e:
                    logger.warning("Agent question notification failed: %s", e)

        # ------------------------------------------------------------------ #
        # Exponential-backoff retry loop for Claude API rate limits.
        #
        # On every PAUSED_RATE_LIMIT result we:
        #   1. Post an immediate "rate-limited" notice to Discord.
        #   2. Sleep for an exponentially-growing delay (base * 2^attempt,
        #      capped at rate_limit_max_backoff_seconds).
        #   3. Post a "resuming now" notice to Discord.
        #   4. Re-initialise the adapter and retry the query.
        #
        # After rate_limit_max_retries consecutive rate-limit hits we give
        # up and fall through to the normal PAUSED_RATE_LIMIT path, which
        # pauses the task in the DB and retries it in the next scheduler
        # cycle.
        #
        # NOTE: The total sleep time across all retries may exceed
        # agents_config.stuck_timeout_seconds.  If you enable multiple
        # retries, raise stuck_timeout_seconds in your config accordingly.
        # ------------------------------------------------------------------ #
        _rl_base = self.config.pause_retry.rate_limit_backoff_seconds
        _rl_max_backoff = self.config.pause_retry.rate_limit_max_backoff_seconds
        _rl_max_retries = self.config.pause_retry.rate_limit_max_retries
        _rl_attempt = 0

        while True:
            output = await adapter.wait(on_message=forward_agent_message)

            if output.result != AgentResult.PAUSED_RATE_LIMIT:
                break  # Completed, failed, or token-exhausted — leave the loop.

            _rl_attempt += 1
            if _rl_attempt > _rl_max_retries:
                # Auto-retries exhausted; let the normal PAUSED handling take over.
                logger.info(
                    "Task %s: rate-limit retries exhausted (%d), pausing task.",
                    task.id,
                    _rl_max_retries,
                )
                break

            _backoff = min(_rl_base * (2 ** (_rl_attempt - 1)), _rl_max_backoff)
            logger.info(
                "Task %s: rate limited (attempt %d/%d), waiting %ds before retry.",
                task.id,
                _rl_attempt,
                _rl_max_retries,
                _backoff,
            )

            await self._emit_notify(
                "notify.task_message",
                TaskMessageEvent(
                    task_id=task.id,
                    message="⏳ Claude is currently rate-limited. We will try again in a moment.",
                    message_type="status",
                    project_id=action.project_id,
                ),
            )

            await asyncio.sleep(_backoff)

            await self._emit_notify(
                "notify.task_message",
                TaskMessageEvent(
                    task_id=task.id,
                    message="✅ Rate limit cleared — resuming now.",
                    message_type="status",
                    project_id=action.project_id,
                ),
            )

            # Re-initialise the adapter so the next call starts a fresh query.
            await adapter.start(ctx)

        # ------------------------------------------------------------------ #
        # Token accounting and result persistence.
        #
        # Token usage is recorded regardless of result (COMPLETED, FAILED,
        # etc.) because even failed runs consume API tokens.  Budget warnings
        # are checked after recording so the threshold calculation includes
        # the tokens just used.
        # ------------------------------------------------------------------ #
        if output.tokens_used > 0:
            await self.db.record_token_usage(
                action.project_id,
                action.agent_id,
                action.task_id,
                output.tokens_used,
            )
            # Check if the project's budget usage has crossed a warning threshold
            try:
                await self._check_budget_warning(
                    action.project_id,
                    output.tokens_used,
                )
            except Exception as e:
                logger.warning("Budget warning check failed: %s", e)

        # Persist task result
        try:
            await self.db.save_task_result(action.task_id, action.agent_id, output)
        except Exception as e:
            logger.error("Failed to save task result: %s", e)

        # Persist session ID for potential session forking on reopen
        if output.session_id:
            try:
                await self.db.set_task_meta(action.task_id, "last_session_id", output.session_id)
            except Exception as e:
                logger.warning("Failed to persist session_id: %s", e)

        # Re-fetch task in case retry_count changed
        task = await self.db.get_task(action.task_id)

        # Helper: post to task thread (agent_output type) or to channel.
        # Used for in-progress updates (e.g. git errors, paused notices).
        async def _post(msg: str, *, embed: Any = None) -> None:
            await self._emit_notify(
                "notify.task_message",
                TaskMessageEvent(
                    task_id=task.id,
                    message=msg,
                    message_type="agent_output",
                    project_id=action.project_id,
                ),
            )

        # Helper: post a brief notification to the main (notifications) channel.
        # When a thread exists the handler replies to the thread-root message.
        async def _notify_brief(msg: str, *, embed: Any = None) -> None:
            await self._emit_notify(
                "notify.task_message",
                TaskMessageEvent(
                    task_id=task.id,
                    message=msg,
                    message_type="brief",
                    project_id=action.project_id,
                ),
            )

        # ------------------------------------------------------------------ #
        # Result handling — branch on the agent's exit status.
        #
        # Each branch follows the same pattern:
        #   1. Transition the task to its next state in the DB.
        #   2. Perform any post-completion work (git merge/PR, plan parsing).
        #   3. Post a detailed summary to the task thread (or channel).
        #   4. Post a brief one-liner to the notifications channel.
        #   5. Check for downstream dependency-chain impacts.
        #   6. Save to memory for future task context (if enabled).
        #
        # The branches are:
        #   COMPLETED        → verify, commit/merge/PR, auto-task, memory
        #   FAILED           → increment retry counter, block if exhausted
        #   PAUSED_*         → schedule a resume_after timestamp
        #   WAITING_INPUT    → pause and notify for human response
        # ------------------------------------------------------------------ #

        # Track the final root text for updating the thread root message
        # on completion.  Set in the result branches below; consumed in the
        # cleanup section.
        _final_root_content: str | None = None  # replaces "Agent working: ..." text

        if output.result == AgentResult.COMPLETED:
            # Build pipeline context
            ws = await self.db.get_workspace_for_task(task.id)
            project = await self.db.get_project(task.project_id)
            default_branch = await self._get_default_branch(
                project, ws.workspace_path if ws else workspace
            )
            has_repo = bool(project and project.repo_url)

            repo = (
                RepoConfig(
                    id=f"project-{task.project_id}",
                    project_id=task.project_id,
                    source_type=ws.source_type if ws else RepoSourceType.LINK,
                    url=project.repo_url if project else "",
                    default_branch=default_branch,
                )
                if (has_repo or ws) and ws
                else None
            )

            ctx = PipelineContext(
                task=task,
                agent=agent,
                output=output,
                workspace_path=ws.workspace_path if ws else workspace,
                workspace_id=ws.id if ws else None,
                repo=repo,
                default_branch=default_branch,
                project=project,
            )

            # Run completion pipeline (commit → plan_discover → merge)
            logger.info("Task %s: running completion pipeline", task.id)
            pr_url, completed_ok = await self._run_completion_pipeline(ctx)

            if ctx.plan_needs_approval and completed_ok:
                logger.info(
                    "Task %s: plan needs approval — sending notification",
                    task.id,
                )
                # Plan was discovered — present it to the user for approval
                # instead of auto-creating subtasks.
                await self.db.transition_task(
                    action.task_id,
                    TaskStatus.AWAITING_PLAN_APPROVAL,
                    context="plan_found",
                )
                await self.db.log_event(
                    "plan_found",
                    project_id=action.project_id,
                    task_id=action.task_id,
                    agent_id=action.agent_id,
                )
                # Notify in the task thread that a plan was found
                await self._emit_notify(
                    "notify.task_message",
                    TaskMessageEvent(
                        task_id=task.id,
                        message="📋 **Plan detected** — processing for approval...",
                        message_type="agent_output",
                        project_id=action.project_id,
                    ),
                )

                # Retrieve the stored plan content for the event
                plan_contexts = await self.db.get_task_contexts(task.id)
                raw_ctx = next(
                    (c for c in plan_contexts if c["type"] == "plan_raw"),
                    None,
                )
                if not raw_ctx:
                    logger.warning(
                        "Task %s: plan_needs_approval=True but no plan_raw "
                        "context found — approval embed will be empty",
                        task.id,
                    )
                # Generate a URL to view the full plan in a browser
                plan_url = ""
                if self.config.mcp_server.enabled:
                    from src.api.health import get_plan_url

                    plan_url = get_plan_url(task.id)

                # Auto pre-create draft subtasks so approval uses the fast
                # path (plan_draft_subtasks context exists).  This mirrors
                # the logic in _cmd_process_plan.  Failure is non-fatal —
                # approval can still fall back to the legacy path.
                created_info: list[dict] = []
                try:
                    supervisor = self._supervisor
                    if supervisor and supervisor.is_ready and raw_ctx:
                        config = self.config.auto_task
                        workspace_id = ws.id if ws else None
                        self._plan_processing_locks.add(action.project_id)
                        try:
                            created_info = await supervisor.break_plan_into_tasks(
                                raw_plan=raw_ctx["content"],
                                parent_task_id=task.id,
                                project_id=action.project_id,
                                workspace_id=workspace_id,
                                chain_dependencies=config.chain_dependencies,
                                requires_approval=(
                                    task.requires_approval if config.inherit_approval else False
                                ),
                                base_priority=task.priority,
                            )

                            if created_info:
                                # Block first subtask on parent so chain stays
                                # blocked until plan is approved.
                                first_subtask_id = created_info[0]["id"]
                                try:
                                    await self.db.add_dependency(
                                        first_subtask_id, depends_on=task.id
                                    )
                                    logger.info(
                                        "Task %s: added blocking dep %s → %s (parent)",
                                        task.id,
                                        first_subtask_id,
                                        task.id,
                                    )
                                except Exception as e:
                                    logger.warning(
                                        "Task %s: failed to add blocking dep %s → %s: %s",
                                        task.id,
                                        first_subtask_id,
                                        task.id,
                                        e,
                                    )

                                # Store draft subtask IDs for approve/delete/reject
                                import json as _json

                                await self.db.add_task_context(
                                    task.id,
                                    type="plan_draft_subtasks",
                                    label="Draft Subtask IDs",
                                    content=_json.dumps(created_info),
                                )

                                logger.info(
                                    "Task %s: auto-created %d draft subtasks",
                                    task.id,
                                    len(created_info),
                                )
                        finally:
                            self._plan_processing_locks.discard(action.project_id)
                except Exception:
                    logger.exception(
                        "Task %s: failed to auto-create draft subtasks "
                        "(approval will use legacy path)",
                        task.id,
                    )

                # ── Auto-approve if task has auto_approve_plan set ──
                if task.auto_approve_plan and created_info:
                    logger.info(
                        "Task %s: auto_approve_plan=True — auto-approving plan with %d subtask(s)",
                        task.id,
                        len(created_info),
                    )
                    handler = self._get_handler()
                    approve_result = await handler._cmd_approve_plan({"task_id": task.id})
                    if "error" in approve_result:
                        logger.warning(
                            "Task %s: auto-approve failed: %s — falling back to manual approval",
                            task.id,
                            approve_result["error"],
                        )
                        # Fall through to manual approval below
                    else:
                        await self._emit_notify(
                            "notify.task_message",
                            TaskMessageEvent(
                                task_id=task.id,
                                message=(
                                    f"✅ **Plan auto-approved** — "
                                    f"{len(created_info)} subtask(s) activated"
                                ),
                                message_type="agent_output",
                                project_id=action.project_id,
                            ),
                        )
                        await self._emit_text_notify(
                            f"✅ **Plan auto-approved:** `{task.id}` — "
                            f"{task.title} ({len(created_info)} subtask(s))",
                            project_id=action.project_id,
                        )
                        brief = (
                            f"✅ Plan auto-approved: {task.title} "
                            f"(`{task.id}`) — {len(created_info)} subtask(s)"
                        )
                        await _notify_brief(brief)
                        # Skip manual approval flow — jump to the next branch
                        # (the elif/else below handles pr_url and normal completion)
                        # We need to skip past the manual approval notification,
                        # so we use a flag.
                        ctx.plan_needs_approval = False

                if ctx.plan_needs_approval:
                    # Populate parsed_steps from auto-created subtasks (if any).
                    # If none were created, the embed shows raw content via plan_url.
                    parsed_steps: list[dict] = [
                        {"title": t["title"], "description": ""} for t in created_info
                    ]

                    # Thread URL is resolved by the notification handler
                    # (e.g. DiscordNotificationHandler) at delivery time,
                    # keeping the orchestrator transport-agnostic.
                    await self._emit_notify(
                        "notify.plan_awaiting_approval",
                        PlanAwaitingApprovalEvent(
                            task=build_task_detail(task),
                            subtasks=parsed_steps,
                            plan_url=plan_url,
                            raw_content=raw_ctx["content"] if raw_ctx else "",
                            project_id=action.project_id,
                        ),
                    )
                    brief = f"📋 Plan awaiting approval: {task.title} (`{task.id}`)"
                    await _notify_brief(brief)
            elif pr_url:
                # PR-based approval workflow
                await self.db.transition_task(
                    action.task_id,
                    TaskStatus.AWAITING_APPROVAL,
                    context="pr_created",
                    pr_url=pr_url,
                )
                await self.db.log_event(
                    "pr_created",
                    project_id=action.project_id,
                    task_id=action.task_id,
                    agent_id=action.agent_id,
                    payload=pr_url,
                )
                await self._emit_notify(
                    "notify.pr_created",
                    PRCreatedEvent(
                        task=build_task_detail(task),
                        pr_url=pr_url,
                        project_id=action.project_id,
                    ),
                )
                brief = f"🔍 PR created for review: {task.title} (`{task.id}`)\n{pr_url}"
                await _notify_brief(brief)
            elif task.requires_approval and not pr_url and completed_ok:
                # Approval required but no PR (e.g. LINK repo)
                await self.db.transition_task(
                    action.task_id,
                    TaskStatus.AWAITING_APPROVAL,
                    context="approval_required_no_pr",
                )
                brief = f"🔍 Awaiting manual approval: {task.title} (`{task.id}`)"
                await _notify_brief(brief)
            elif completed_ok:
                # No approval needed — mark completed
                await self.db.transition_task(
                    action.task_id, TaskStatus.COMPLETED, context="completed_no_approval"
                )
                await self.db.log_event(
                    "task_completed",
                    project_id=action.project_id,
                    task_id=action.task_id,
                    agent_id=action.agent_id,
                )
                brief = f"✅ Task completed: {task.title} (`{task.id}`)"
                # Post brief to thread and emit completion event for main channel
                await _post(brief)
                await self._emit_notify(
                    "notify.task_completed",
                    TaskCompletedEvent(
                        task=build_task_detail(task),
                        agent=build_agent_summary(agent),
                        summary=output.summary or "",
                        files_changed=output.files_changed or [],
                        tokens_used=output.tokens_used or 0,
                        project_id=action.project_id,
                    ),
                )
                await self.bus.emit(
                    "task.completed",
                    {
                        "task_id": task.id,
                        "project_id": task.project_id,
                        "title": task.title,
                    },
                )

                # Auto-reload plugin if the task modified a plugin workspace
                await self._check_plugin_workspace_update(task, ws)
                # Mark for thread root update
                _final_root_content = f"✅ **Work completed:** {task.title}"
            elif ctx.verification_reopened:
                # Task was already reopened to READY by _phase_verify —
                # don't transition to BLOCKED.
                brief = f"🔄 Task reopened for git verification: {task.title} (`{task.id}`)"
                await _post(brief)
                await _notify_brief(brief)
                # Clean up workspace so the next attempt starts with a
                # clean working tree.  Without this, uncommitted changes
                # persist and cause the same verification failure on retry
                # (especially for LINK workspaces without remotes, where
                # _prepare_workspace cannot do a hard reset).
                await self._cleanup_workspace_for_next_task(
                    ctx.workspace_path,
                    ctx.default_branch,
                    task.id,
                    project_id=task.project_id,
                    agent_id=ctx.agent.id,
                )
            else:
                # Pipeline stopped and could not reopen — last-ditch attempt
                # to clean the workspace before blocking.  This catches the
                # case where auto-remediation mostly worked but a remaining
                # issue (e.g. unpushed commits) exhausted retries.
                if ctx.workspace_path:
                    try:
                        has_dirty = await self.git.ahas_uncommitted_changes(ctx.workspace_path)
                        if has_dirty:
                            cur = await self.git.aget_current_branch(ctx.workspace_path)
                            still_dirty = await self._auto_remediate_uncommitted(
                                ctx.workspace_path,
                                task.id,
                                cur,
                                project_id=task.project_id,
                                agent_id=ctx.agent.id,
                            )
                            if not still_dirty:
                                logger.info(
                                    "Task %s: last-ditch remediation cleaned workspace",
                                    task.id,
                                )
                    except Exception as e:
                        logger.warning(
                            "Task %s: last-ditch remediation failed: %s",
                            task.id,
                            e,
                        )

                await self.db.transition_task(
                    action.task_id,
                    TaskStatus.BLOCKED,
                    context="verification_failed",
                )
                await self._emit_task_failure(
                    task,
                    "verification_failed",
                    error="Post-task verification failed, max retries exhausted",
                )
                await _post(
                    f"**Verification failed** for `{task.id}` — "
                    f"max retries exhausted, manual resolution needed."
                )
                # Clean up workspace so it's ready for the next task
                await self._cleanup_workspace_for_next_task(
                    ctx.workspace_path,
                    ctx.default_branch,
                    task.id,
                    project_id=task.project_id,
                    agent_id=ctx.agent.id,
                )

            # Ensure workspace is clean for the next task.  The
            # verification_reopened and verification_blocked paths above
            # already call _cleanup_workspace_for_next_task.  For all other
            # completion outcomes (normal completion, plan approval, PR
            # approval), clean up here so dirty workspace state doesn't
            # bleed into the next task assigned to this workspace.
            if not ctx.verification_reopened and completed_ok and ctx.workspace_path:
                await self._cleanup_workspace_for_next_task(
                    ctx.workspace_path,
                    ctx.default_branch,
                    task.id,
                    project_id=task.project_id,
                    agent_id=ctx.agent.id,
                )

            # Re-check DEFINED tasks so newly created subtasks get promoted
            await self._check_defined_tasks()

            # Save completed task result as a memory for future recall,
            # then revise the project profile and optionally generate notes.
            if self.memory_manager and completed_ok:
                try:
                    await self.memory_manager.remember(task, output, workspace)
                except Exception as e:
                    logger.warning("Memory remember failed for task %s: %s", task.id, e)

                # Post-task profile revision — only for COMPLETED tasks.
                # Failed tasks still get remember() but don't revise the profile
                # since they may contain incorrect understanding.
                try:
                    await self.memory_manager.revise_profile(
                        task.project_id, task, output, workspace
                    )
                except Exception as e:
                    logger.warning("Profile revision failed for task %s: %s", task.id, e)

                # Auto-generate notes if enabled
                try:
                    note_paths = await self.memory_manager.generate_task_notes(
                        task.project_id, task, output, workspace
                    )
                    if note_paths and self.bus:
                        for note_path in note_paths:
                            await self.bus.emit(
                                "note.created",
                                {
                                    "project_id": task.project_id,
                                    "task_id": task.id,
                                    "note_path": note_path,
                                },
                            )
                except Exception as e:
                    logger.warning("Note generation failed for task %s: %s", task.id, e)

                # Extract structured facts for later consolidation
                try:
                    staging_path = await self.memory_manager.extract_task_facts(
                        task.project_id, task, output, workspace
                    )
                    if staging_path and self.bus:
                        await self.bus.emit(
                            "facts.extracted",
                            {
                                "project_id": task.project_id,
                                "task_id": task.id,
                                "staging_path": staging_path,
                            },
                        )
                except Exception as e:
                    logger.warning("Fact extraction failed for task %s: %s", task.id, e)

        elif output.result == AgentResult.FAILED:
            new_retry = task.retry_count + 1
            if new_retry >= task.max_retries:
                await self.db.transition_task(
                    action.task_id, TaskStatus.BLOCKED, context="max_retries", retry_count=new_retry
                )
                await self._emit_task_failure(
                    task,
                    "max_retries",
                    error=f"Max retries ({task.max_retries}) exhausted",
                )
                brief = (
                    f"🚫 Task blocked: {task.title} (`{task.id}`) — "
                    f"max retries ({task.max_retries}) exhausted"
                )
            else:
                await self.db.transition_task(
                    action.task_id,
                    TaskStatus.READY,
                    context="retry",
                    retry_count=new_retry,
                    assigned_agent_id=None,
                )
                brief = (
                    f"⚠️ Task failed: {task.title} (`{task.id}`) — "
                    f"retry {new_retry}/{task.max_retries}"
                )
            # Emit typed failure/blocked event — the notification handler
            # routes detailed info to thread and brief to main channel.
            if new_retry >= task.max_retries:
                await self._emit_notify(
                    "notify.task_blocked",
                    TaskBlockedEvent(
                        task=build_task_detail(task),
                        last_error=output.error_message or "",
                        project_id=action.project_id,
                    ),
                )
            else:
                await self._emit_notify(
                    "notify.task_failed",
                    TaskFailedEvent(
                        task=build_task_detail(task),
                        agent=build_agent_summary(agent),
                        error_label="",
                        error_detail=output.error_message or "",
                        fix_suggestion="",
                        retry_count=new_retry,
                        max_retries=task.max_retries,
                        project_id=action.project_id,
                    ),
                )
            # Brief notification → main channel (reply to thread or standalone)
            await _notify_brief(brief)

            # Mark for thread root update
            if new_retry >= task.max_retries:
                _final_root_content = f"🚫 **Work blocked:** {task.title}"
            else:
                _final_root_content = f"⚠️ **Work failed (retrying):** {task.title}"

            # Check if this blocked task breaks a dependency chain
            if new_retry >= task.max_retries:
                await self._notify_stuck_chain(task)

            # Save failed task result as a memory — failures are valuable
            # context for future tasks working in the same area.
            if self.memory_manager:
                try:
                    await self.memory_manager.remember(task, output, workspace)
                except Exception as e:
                    logger.warning("Memory remember failed for task %s: %s", task.id, e)

            # Clean up workspace git state so it's ready for the next task.
            # For retries, this ensures the workspace isn't left dirty from
            # a failed agent run; for blocked tasks, it ensures the workspace
            # is available in a clean state.
            if workspace:
                try:
                    fail_project = await self.db.get_project(task.project_id)
                    fail_default_branch = await self._get_default_branch(fail_project, workspace)
                    await self._cleanup_workspace_for_next_task(
                        workspace,
                        fail_default_branch,
                        task.id,
                        project_id=task.project_id,
                        agent_id=task.assigned_agent_id,
                    )
                except Exception as e:
                    logger.warning(
                        "Task %s: workspace cleanup after failure failed: %s",
                        task.id,
                        e,
                    )

        elif output.result in (AgentResult.PAUSED_TOKENS, AgentResult.PAUSED_RATE_LIMIT):
            # PAUSED path — the agent hit an API limit (rate or context window).
            # We set a future resume_after timestamp; _resume_paused_tasks()
            # will promote the task back to READY once the backoff expires.
            # Note: if the in-loop rate-limit retries above were exhausted,
            # we end up here for PAUSED_RATE_LIMIT as a final fallback.
            retry_secs = (
                self.config.pause_retry.rate_limit_backoff_seconds
                if output.result == AgentResult.PAUSED_RATE_LIMIT
                else self.config.pause_retry.token_exhaustion_retry_seconds
            )
            await self.db.transition_task(
                action.task_id,
                TaskStatus.PAUSED,
                context="tokens_exhausted",
                resume_after=time.time() + retry_secs,
            )
            reason = (
                "rate limit"
                if output.result == AgentResult.PAUSED_RATE_LIMIT
                else "token exhaustion"
            )
            await self._emit_task_event(
                "task.paused",
                task,
                reason=reason,
                resume_after=time.time() + retry_secs,
            )
            await _post(
                f"**Task Paused:** `{task.id}` — {task.title}\n"
                f"Reason: {reason}. Will retry in {retry_secs}s."
            )

            # Clean up workspace git state so the next task (or the resumed
            # version of this task) starts with a clean working tree.  The
            # workspace lock is released below for all result types, so any
            # dirty state left here would bleed into the next occupant.
            if workspace:
                try:
                    pause_project = await self.db.get_project(task.project_id)
                    pause_default_branch = await self._get_default_branch(pause_project, workspace)
                    await self._cleanup_workspace_for_next_task(
                        workspace,
                        pause_default_branch,
                        task.id,
                        project_id=task.project_id,
                        agent_id=task.assigned_agent_id,
                    )
                except Exception as e:
                    logger.warning(
                        "Task %s: workspace cleanup after pause failed: %s",
                        task.id,
                        e,
                    )

        elif output.result == AgentResult.WAITING_INPUT:
            # Agent is blocked on a question — transition to WAITING_INPUT
            # and notify so a human can respond.
            question_text = output.question or output.summary or "(no question text)"
            await self.db.transition_task(
                action.task_id,
                TaskStatus.WAITING_INPUT,
                context="agent_question",
            )
            await self._emit_task_event(
                "task.waiting_input",
                task,
                question=question_text,
            )
            await self.db.log_event(
                "agent_question",
                project_id=action.project_id,
                task_id=action.task_id,
                agent_id=action.agent_id,
                payload=question_text[:500],
            )
            await self._emit_notify(
                "notify.agent_question",
                AgentQuestionEvent(
                    task=build_task_detail(task),
                    agent=build_agent_summary(agent),
                    question=question_text,
                    project_id=action.project_id,
                ),
            )

        # ------------------------------------------------------------------ #
        # Cleanup — runs regardless of which result branch was taken above.
        #
        # Order matters here:
        #   1. Release workspace lock FIRST — the scheduler (which runs in
        #      the main loop, potentially concurrently with this background
        #      task's completion) checks workspace availability before
        #      assigning new work.  Releasing before freeing the agent
        #      ensures the workspace count is accurate when the scheduler
        #      next evaluates this project.
        #   2. Free agent SECOND — transitions the agent from BUSY back to
        #      IDLE (or PAUSED if an admin paused it mid-execution).
        #   3. Remove adapter reference — allows garbage collection of the
        #      adapter process handle.
        # ------------------------------------------------------------------ #

        # Close the task thread — update the root message with final status
        # and clean up thread references in the notification handler.
        if _final_root_content:
            await self._emit_notify(
                "notify.task_thread_close",
                TaskThreadCloseEvent(
                    task_id=task.id,
                    final_status=task.status.value
                    if hasattr(task.status, "value")
                    else str(task.status),
                    final_message=_final_root_content,
                    project_id=action.project_id,
                ),
            )

        # Clean up the sentinel file before releasing the workspace lock.
        # The workspace variable is from earlier in _execute_task (the local).
        if workspace:
            self._remove_sentinel(workspace)

        # Release the workspace lock so other tasks can use this workspace.
        await self.db.release_workspaces_for_task(action.task_id)

        # Free the agent for new work.  We re-read the agent's current state
        # from the DB because an admin may have paused the agent (via
        # /pause-agent) while it was BUSY.  If so, we respect the PAUSED
        # state instead of blindly resetting to IDLE.
        post_agent = await self.db.get_agent(action.agent_id)
        next_state = (
            AgentState.PAUSED
            if post_agent and post_agent.state == AgentState.PAUSED
            else AgentState.IDLE
        )
        await self.db.update_agent(action.agent_id, state=next_state, current_task_id=None)

        # Remove adapter reference — the adapter's subprocess has already
        # exited by this point (wait() returned), so this is just cleanup.
        self._adapters.pop(action.agent_id, None)
        self._task_exec_start.pop(action.task_id, None)

        # Delete the task-added notification — it's fully superseded.
        added_msg = self._task_added_messages.pop(action.task_id, None)
        if added_msg is not None:
            try:
                await added_msg.delete()
            except Exception as e:
                logger.debug("Could not delete task-added message for %s: %s", action.task_id, e)

        # Delete the Task Started message — the Task Completed/Failed embed
        # posted above is the only one we want to keep.
        started_msg = self._task_started_messages.pop(action.task_id, None)
        if started_msg is not None:
            try:
                await started_msg.delete()
            except Exception as e:
                logger.debug("Could not delete task-started message for %s: %s", action.task_id, e)

    # ------------------------------------------------------------------
    # Plugin self-update detection
    # ------------------------------------------------------------------

    async def _check_plugin_workspace_update(self, task: Any, ws: Any) -> None:
        """Check if a completed task modified a plugin workspace and auto-reload.

        When agents work inside a plugin's install directory, the plugin
        source has likely changed.  This method detects that situation and
        performs a hot-reload (shutdown -> load -> initialize) so the new
        code takes effect immediately — enabling an agent-driven plugin
        development workflow.

        Args:
            task: The completed task object (needs ``id`` and ``title``).
            ws: The workspace record (may be ``None``).  Must expose
                ``workspace_path`` when present.
        """
        if not ws or not getattr(ws, "workspace_path", None):
            return

        registry = getattr(self, "plugin_registry", None)
        if registry is None:
            return

        workspace_path = str(ws.workspace_path)

        # Identify which plugin (if any) owns this workspace path.
        # Plugin install paths live under ~/.agent-queue/plugins/{name}/.
        target_plugin: str | None = None
        for name, loaded in registry._plugins.items():
            plugin_dir = str(loaded.install_path)
            # Check if the workspace is inside (or equal to) the plugin dir
            if workspace_path == plugin_dir or workspace_path.startswith(plugin_dir + "/"):
                target_plugin = name
                break

        if target_plugin is None:
            return

        logger.info(
            "Task %s (%s) modified plugin workspace '%s' — auto-reloading plugin",
            task.id,
            task.title,
            target_plugin,
        )

        try:
            await registry.reload_plugin(target_plugin)
            logger.info("Plugin '%s' reloaded successfully after task %s", target_plugin, task.id)
        except Exception as e:
            logger.error(
                "Failed to auto-reload plugin '%s' after task %s: %s",
                target_plugin,
                task.id,
                e,
                exc_info=True,
            )
            # Emit a failure event so hooks/monitors can react
            await self.bus.emit(
                "plugin.reload_failed",
                {
                    "plugin": target_plugin,
                    "task_id": task.id,
                    "error": str(e),
                },
            )
