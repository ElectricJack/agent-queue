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
import uuid
from pathlib import Path
from typing import Any

from src.config import AppConfig, ConfigWatcher
from src.llm_logger import LLMLogger
from src.logging_config import CorrelationContext
from src.database import create_database
from src.notifications.builder import build_task_detail
from src.notifications.events import (
    TaskStoppedEvent,
    TaskThreadCloseEvent,
)
from src.event_bus import EventBus
from src.messaging.types import (
    NotifyCallback as _NotifyCallbackType,
    ThreadSendCallback as _ThreadSendCallbackType,
    CreateThreadCallback as _CreateThreadCallbackType,
)
from src.git.manager import GitManager
from src.models import (
    AgentProfile,
    AgentState,
    RepoSourceType,
    Task,
    TaskStatus,
)
from src.scheduler import AssignAction, Scheduler, SchedulerState
from src.tokens.budget import BudgetManager
from src.vault_manager import VaultManager

# Mixin imports — each provides one domain of methods
from src.orchestrator.workspace import WorkspaceMixin
from src.orchestrator.execution import ExecutionMixin
from src.orchestrator.monitoring import MonitoringMixin
from src.orchestrator.git_ops import GitOpsMixin
from src.orchestrator.approval import ApprovalMixin
from src.orchestrator.context import ContextMixin
from src.orchestrator.events import EventsMixin
from src.orchestrator.sync_workflow import SyncWorkflowMixin

logger = logging.getLogger(__name__)


def _parse_reset_time(error_msg: str) -> float | None:
    """Extract a session-limit reset timestamp from an error message.

    Handles: "You've hit your limit · resets 2pm (America/Los_Angeles)"
    Returns a Unix timestamp, or None if not found.
    """
    import re
    from datetime import datetime

    match = re.search(
        r"resets\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*\(([^)]+)\)",
        error_msg,
        re.IGNORECASE,
    )
    if not match:
        return None
    time_str, tz_name = match.group(1), match.group(2)
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        fmt = "%I:%M%p" if ":" in time_str else "%I%p"
        parsed = datetime.strptime(time_str.strip(), fmt).replace(
            year=now.year, month=now.month, day=now.day, tzinfo=tz
        )
        if parsed <= now:
            from datetime import timedelta

            parsed += timedelta(days=1)
        return parsed.timestamp()
    except Exception:
        return None


# Re-export callback types from the messaging abstraction layer for
# backward compatibility.  New code should import from src.messaging.types.
NotifyCallback = _NotifyCallbackType
ThreadSendCallback = _ThreadSendCallbackType
CreateThreadCallback = _CreateThreadCallbackType


def _idle_by_project(state: "SchedulerState") -> dict[str, int]:
    """Count idle agents grouped by project id."""
    counts: dict[str, int] = {}
    for agent in state.agents:
        if agent.state != AgentState.IDLE:
            continue
        pid = getattr(agent, "project_id", None)
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
    return counts


class Orchestrator(
    WorkspaceMixin,
    ExecutionMixin,
    MonitoringMixin,
    GitOpsMixin,
    ApprovalMixin,
    ContextMixin,
    EventsMixin,
    SyncWorkflowMixin,
):
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

    def __init__(self, config: AppConfig, runtimes=None):
        """Initialize the orchestrator with its configuration and subsystems.

        Args:
            config: The application configuration (loaded from YAML).
            runtimes: :class:`~src.runtimes.RuntimeRegistry` used to
                instantiate agent runtimes.  When None, the orchestrator can
                manage state and scheduling but cannot execute tasks.

        The constructor wires up all subsystems but does NOT perform any
        async initialization (DB schema creation, stale-state recovery,
        event subscriptions).  Call ``await initialize()`` before running
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
        self.git.set_lock_provider(self._resolve_git_lock)
        self._runtimes = runtimes
        # Lazy-creates agent rows when work needs them; runs at the top of
        # each scheduling tick before Scheduler.schedule().  See
        # docs/superpowers/specs/2026-05-07-agent-reconciliation-design.md.
        from src.orchestrator.agent_reconciler import AgentReconciler

        self._agent_reconciler = AgentReconciler(
            self.db, worktrees_enabled=config.worktrees.enabled
        )
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
        # Git HEAD SHA recorded just before the agent starts, keyed by
        # task_id.  Used by write_task_summary to show only the commits
        # the agent actually made (git log pre_sha..HEAD).
        self._task_pre_exec_sha: dict[str, str] = {}
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
        # Daemon-wide doctor check registry (``src.doctor.DoctorRegistry``).
        # Populated by ``src/main.py`` before ``initialize()`` so the plugin
        # registry can hand it to plugin contexts; stays None for embedded /
        # test orchestrators, in which case ``_cmd_doctor`` reports
        # "not configured".  See docs/specs/design/trust-and-ops.md §5.5.
        self.doctor_registry = None
        # Provider-level cooldowns: maps profile_id (e.g. "claude-opus") to the
        # Unix timestamp when scheduling should resume.  Set when a session
        # limit is detected; the scheduler skips agents whose profile is
        # cooled-down until the cooldown expires.  Supports per-profile limits
        # so exhausting
        # one provider doesn't block others.
        self._provider_cooldowns: dict[str, float] = {}
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
        self._last_memory_compact: float = 0.0  # TODO: remove once v2 compaction is wired
        self._last_failed_blocked_report: float = 0.0
        # Throttle: gate sweep (WG-3) runs at most once per
        # config.work_graph.gate_sweep_interval_seconds.
        self._last_gate_sweep: float = 0.0
        # Cached snapshot from the most recent ``_schedule`` tick so
        # ``_cmd_explain_task`` can build capacity reasons between ticks
        # (WG-4, work-graph §6.3).
        self._last_scheduler_state = None
        self._last_scheduler_workspace_counts: dict[str, int] = {}
        self._last_scheduler_idle_by_project: dict[str, int] = {}
        # EventBus subscription that resolves ``event`` gates live.  Set by
        # ``_subscribe_event_gates`` on initialize; kept as an attribute so
        # tests can toggle it deterministically.
        self._gate_event_unsub = None
        self._config_watcher: ConfigWatcher | None = None
        self._supervisor = None  # Set via set_supervisor() in Discord bot
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
        self.vault_watcher = None
        # MCP server registry — populated from vault/mcp-servers/*.md and
        # vault/projects/*/mcp-servers/*.md on startup, kept current by the
        # vault watcher.  Resolves the ``list[str]`` of names on each
        # profile to the dicts the agent adapter expects at task launch.
        from src.profiles.mcp_catalog import McpToolCatalog
        from src.profiles.mcp_registry import McpRegistry

        self.mcp_registry = McpRegistry()
        # ---- Session runtime (docs/specs/design/session-runtime.md) ------
        # Harness descriptions (vault/harnesses/*.md), the provider
        # registry, the spec builder and the reconciler.  All constructed
        # unconditionally so `aq session list` and the tests work with the
        # feature flag off; nothing spawns until ``sessions.enabled``.
        from src.sessions import default_session_registry
        from src.sessions.harness_registry import HarnessRegistry
        from src.sessions.reconciler import SessionReconciler
        from src.sessions.spec import SessionSpecBuilder

        self.harness_registry = HarnessRegistry()
        self.session_providers = default_session_registry(config)
        self.session_spec_builder = SessionSpecBuilder(config, self.harness_registry)
        # AQ_DAEMON_EPOCH: identifies this daemon *run*.  Provenance for
        # adoption, never a validity test — an older-epoch session is still
        # adoptable, and the instance token is what fences kills.
        self.daemon_epoch = uuid.uuid4().hex[:12]
        self.session_reconciler = SessionReconciler(
            self.db,
            config,
            self.session_providers,
            harnesses=self.harness_registry,
            spec_builder=self.session_spec_builder,
            bus=self.bus,
            # The reconciler needs the orchestrator, not the command
            # handler: every terminal verdict runs the same cleanup tail as
            # the happy path (``release_session_task_resources``).
            orchestrator=self,
            epoch=self.daemon_epoch,
        )
        # Tool catalog snapshot keyed by (project_id, server_name).  Probed
        # once at startup; refreshed per-server by the vault watcher hook
        # and the probe-mcp-server command.
        self.mcp_tool_catalog = McpToolCatalog()
        self.workspace_spec_watcher = None  # WorkspaceSpecWatcher | None (vault.md §4)
        self.timer_service = None  # TimerService | None — initialized in initialize()
        # Reference to the command handler, set by the bot after initialization.
        # Used to pass handler references to interactive Discord views (e.g.
        # Retry/Skip buttons on failed task notifications).
        self._command_handler: Any = None
        # Project IDs currently undergoing plan processing (supervisor is
        # Tracks per-project budget warning thresholds already sent so we
        # don't spam the same warning.  Keyed by project_id, value is the
        # highest threshold percentage (e.g. 80, 95) already notified.
        self._budget_warned_at: dict[str, int] = {}
        # Per-workspace-path asyncio.Lock for serializing shared git
        # operations (fetch, gc) across branch-isolated worktrees.
        # Keyed by the base workspace path (the parent repo directory).
        self._git_mutexes: dict[str, asyncio.Lock] = {}
        # slot/worktree path -> base repo path, for the *sync* git-lock
        # resolver.  Replaces the retired ``.worktrees-<base>/`` filename
        # parsing (worktree-execution §6.2): the relationship is data now,
        # populated from slot rows as workspaces are prepared.
        self._worktree_base_paths: dict[str, str] = {}
        # Per-task workspace attachments captured at acquisition time so the
        # runtime layer can consume them later without re-acquiring.  Keyed
        # by task id.  See workspaces-v2 spec §6 + §8.  Cleared on task
        # completion in _release_workspaces_for_task.
        self._task_attachments: dict[str, "WorkspaceAttachmentSet"] = {}  # noqa: F821
        # Why the last _prepare_workspace for a task returned None, when the
        # answer is "wait, this is normal" rather than "something is wrong".
        # Read and cleared by _execute_task's no-workspace backoff so it can
        # stay quiet about an expected wait.  Keyed by task id.
        self._workspace_wait_reasons: dict[str, str] = {}

    def _git_mutex(self, workspace_path: str) -> asyncio.Lock:
        """Get or create an asyncio.Lock for shared git operations on a workspace."""
        if workspace_path not in self._git_mutexes:
            self._git_mutexes[workspace_path] = asyncio.Lock()
        return self._git_mutexes[workspace_path]

    def _resolve_git_lock(self, cwd: str) -> asyncio.Lock | None:
        """Lock provider for :class:`GitManager`.

        Resolves a ``cwd`` path to the shared git mutex of the repository it
        belongs to, so every worktree of one base serializes its ``fetch`` /
        ``gc`` / ``pull`` against the shared object store.  Returns ``None``
        when the path has no registered mutex (no serialization needed).

        The slot -> base mapping comes from ``_worktree_base_paths``, built
        from slot rows when workspaces are prepared.  This must stay sync
        (``GitManager._arun`` calls it inline), which is why it reads a cache
        rather than asking git.
        """
        key = self._worktree_base_paths.get(cwd, cwd)
        return self._git_mutexes.get(key)

    def set_command_handler(self, handler: Any) -> None:
        """Store a reference to the command handler for interactive views."""
        self._command_handler = handler

    def _get_handler(self) -> Any:
        """Return the command handler or None. Used by interactive views."""
        return self._command_handler

    def set_supervisor(self, supervisor) -> None:
        """Set the Supervisor reference for post-task delegation."""
        self._supervisor = supervisor

        # Expose plugin tools to the supervisor's tool registry so the LLM
        # can discover and call plugin-provided tools (e.g. memory_save).
        if hasattr(self, "plugin_registry") and self.plugin_registry:
            supervisor._registry.set_plugin_registry(self.plugin_registry)

        # Store the registry on the orchestrator so command handlers can access
        # the shared instance (with plugin tools and the tool index).
        self._tool_registry = supervisor._registry

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
                thinking_budget: int | None = None,
            ) -> str:
                if model or provider or thinking_budget is not None:
                    import dataclasses
                    from src.chat_providers import create_chat_provider

                    sys_cfg = self.config.chat_provider
                    cfg = dataclasses.replace(
                        sys_cfg,
                        provider=provider or sys_cfg.provider,
                        model=model or sys_cfg.model,
                        thinking_budget=(
                            thinking_budget
                            if thinking_budget is not None
                            else sys_cfg.thinking_budget
                        ),
                    )
                    one_shot = create_chat_provider(cfg)
                    if one_shot is None:
                        raise RuntimeError(
                            f"Plugin {plugin_name}: chat provider '{cfg.provider}' "
                            "is not configured (missing credentials)"
                        )
                    resp = await one_shot.create_message(
                        messages=[{"role": "user", "content": prompt}],
                        system=f"You are a helper for plugin:{plugin_name}.",
                    )
                    return "".join(resp.text_parts)
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

        ``mcp_servers`` from YAML may be either a ``list[str]`` (new shape)
        or a legacy ``dict[str, dict]`` of inline configs.  We normalise to
        a list of registry names; the inline-config migration extracts the
        actual server defs separately.

        Runs at startup during ``initialize()``.  For each profile defined in
        ``config.agent_profiles``, either creates a new DB row or updates the
        existing one with the latest YAML values.  This ensures the DB always
        reflects the config file as the source of truth for profile definitions.

        Profiles are referenced by ID from tasks and projects (see
        ``_resolve_profile``), so they must exist in the DB before the first
        scheduling cycle.  The upsert pattern means operators can freely edit
        profile settings in YAML and restart the daemon to apply changes.
        """
        from src.profiles.mcp_inline_migration import (
            coerce_mcp_server_names as _coerce_mcp_server_names,
        )

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
                    mcp_servers=_coerce_mcp_server_names(pc.mcp_servers),
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
                        mcp_servers=_coerce_mcp_server_names(pc.mcp_servers),
                        system_prompt_suffix=pc.system_prompt_suffix,
                        install=pc.install,
                    )
                )

    async def _resolve_profile(self, task: Task) -> AgentProfile | None:
        """Resolve the agent profile for a task.

        Resolution order (first non-None wins):
        1. **Task-level** — ``task.profile_id`` (explicit override per task)
        2. **Project default** — ``project.default_profile_id``
        3. **None** (adapter uses built-in defaults)

        Project-scoped overrides are checked first at each level: if a
        profile with id ``project:{project_id}:{profile_id}`` exists, it
        takes precedence over the global ``{profile_id}`` profile. This
        is the row synced from
        ``vault/projects/{project}/agent-types/{profile_id}/profile.md``.

        Profiles control: model selection, permission mode (e.g. plan-only),
        allowed tools allowlist, MCP server configuration, and a system prompt
        suffix that sets the agent's "role" for the task.
        """
        project = await self.db.get_project(task.project_id)
        profile_id = task.profile_id or (project.default_profile_id if project else None)
        if not profile_id:
            return None

        if project:
            scoped = await self.db.get_profile(f"project:{project.id}:{profile_id}")
            if scoped:
                return scoped
        return await self.db.get_profile(profile_id)

    async def _on_playbook_trigger(self, playbook: Any, event_data: dict) -> None:
        """PlaybookManager trigger dispatch — launch a run for a matched event.

        Registered as ``playbook_manager.on_trigger`` at startup.  The
        manager has already applied cooldown + concurrency checks before
        calling us, so we just build the runtime context (Supervisor,
        event bus, plugin registry) and fire a PlaybookRunner.  The run
        is intentionally fire-and-forget — the event dispatch path must
        not block waiting for an LLM-driven graph walk.
        """
        try:
            from src.runtimes.supervisor import Supervisor
            from src.playbooks.runner import PlaybookRunner

            graph = playbook.to_dict()
            supervisor = Supervisor(self, self.config, llm_logger=self.llm_logger)
            if not supervisor.initialize():
                logger.error(
                    "Playbook trigger for '%s': failed to initialise LLM — skipping",
                    playbook.id,
                )
                return

            project_id = event_data.get("project_id")
            if project_id:
                supervisor.set_active_project(project_id)

            plugin_registry = getattr(self, "plugin_registry", None)
            if plugin_registry:
                supervisor._registry.set_plugin_registry(plugin_registry)

            runner = PlaybookRunner(
                graph=graph,
                event=event_data,
                supervisor=supervisor,
                db=self.db,
                event_bus=self.bus,
                runtimes=getattr(self, "_runtimes", None),
            )

            async def _run() -> None:
                with CorrelationContext(run_id=runner.run_id):
                    try:
                        await runner.run()
                    except Exception:
                        logger.exception(
                            "Playbook '%s' run failed (trigger event=%s)",
                            playbook.id,
                            event_data.get("type") or event_data.get("_event_type"),
                        )

            # Detach so the EventBus dispatch loop isn't blocked on the run.
            asyncio.create_task(
                _run(), name=f"playbook:{playbook.id}:{event_data.get('type', 'trigger')}"
            )
            logger.info(
                "Dispatched playbook '%s' for trigger event (project=%s)",
                playbook.id,
                project_id or "(none)",
            )
        except Exception:
            logger.exception(
                "Failed to dispatch playbook '%s' trigger",
                getattr(playbook, "id", "?"),
            )

    def pause(self) -> None:
        """Pause scheduling — no new tasks are assigned, but monitoring continues.

        When paused, ``run_one_cycle`` still runs approvals, dependency
        promotion, stuck-task detection, playbooks, and archival — only the
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
    # that still call them during startup.  All
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

        # Check if this skip-to-completed finishes a workflow stage
        await self._check_workflow_stage_completion(task)

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

        # Clean up sentinel and release workspace lock (worktree-aware)
        ws = await self.db.get_workspace_for_task(task_id)
        if ws:
            self._remove_sentinel(ws.workspace_path)
        await self._release_workspaces_for_task(task_id)
        await self.db.transition_task(
            task_id, TaskStatus.BLOCKED, context="stop_task", assigned_agent_id=None
        )
        if agent_id:
            await self.db.update_agent(agent_id, state=AgentState.IDLE, current_task_id=None)
            self._adapters.pop(agent_id, None)

        from src.profiles.sync import underlying_agent_type

        profile = await self._resolve_profile(task)
        await self._emit_task_failure(
            task,
            "stop_task",
            error="Manually stopped by user",
            agent_id=agent_id,
            agent_type=underlying_agent_type(profile.id) if profile else None,
        )
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
           directory structure exists.  Must run before plugins, memory,
           or any file watchers that depend on vault paths.
        4b. **Vault watcher** — create the ``VaultWatcher`` (playbooks spec
           §17) after the vault structure exists but before subsystems that
           register handlers.  The watcher is NOT started here — it uses
           tick-driven polling via ``check()`` in ``run_one_cycle()``.
           An initial snapshot is taken at the end of ``initialize()``
           after all handlers have been registered.
        5. **Plugins** — discover and load plugin modules.
        6. **Config watcher** — hot-reload support for config.yaml.
        7. **Vault watcher snapshot** — take the initial filesystem snapshot
           so ``check()`` in the tick loop only detects changes after init.

        This method must be called (and awaited) before ``run_one_cycle``.
        Called by ``main.py`` during startup, after ``load_config()`` but
        before the Discord bot connects.
        """
        await self.db.initialize()
        await self._sync_profiles_from_config()
        # Adoption runs *before* recovery: a session that survived the
        # restart must be re-bound before the blanket reset would otherwise
        # yank its task out from under it.  Harnesses are loaded first
        # because adoption reads process_names off them.
        adopted_task_ids: set[str] = set()
        if self.config.sessions.enabled:
            try:
                from src.sessions.harness_registry import (
                    load_from_vault as _load_harnesses,
                )

                _load_harnesses(self.harness_registry, self.config.vault_root)
                report = await self.session_reconciler.adopt_on_start()
                adopted_task_ids = await self.session_reconciler.adopted_task_ids(report)
            except Exception:
                logger.error("Session adoption pass failed", exc_info=True)
        await self._recover_stale_state(skip_task_ids=adopted_task_ids)

        # Initialize VaultManager — central path resolution and directory
        # management for the vault.  Must be available before any subsystem
        # that reads vault paths (memory, plugins, playbooks).
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
        # via check() in run_one_cycle().  This ensures all handlers are
        # registered before detection begins.
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
        from src.profiles.sync import register_profile_handlers

        register_profile_handlers(
            self.vault_watcher,
            db=self.db,
            event_bus=self.bus,
            data_dir=self.config.data_dir,
        )

        # Register MCP server registry watcher handlers.  Each scope has its
        # own glob pattern; both share one in-memory store.  The on_reload
        # hook is left None for now — B3 (tool catalog) will set it so a
        # changed server triggers a re-probe of its tools.
        from src.profiles.mcp_registry import (
            builtin_from_config,
            register_mcp_server_handlers,
        )

        register_mcp_server_handlers(
            self.vault_watcher,
            self.mcp_registry,
            vault_root=self.config.vault_root,
        )
        # Harness files ride the same watcher — editing a harness changes
        # how the next session launches, with no restart and no release.
        from src.sessions.harness_registry import register_harness_handlers

        register_harness_handlers(self.vault_watcher, self.harness_registry)
        # Seed the synthetic agent-queue entry so the dashboard can show
        # "Built-in" plus its plugin tools, and profiles can reference it
        # by name.
        builtin = builtin_from_config(self.config)
        if builtin is not None:
            self.mcp_registry.set_builtin(builtin)

        # Workspace kinds — markdown ↔ DB reconciliation (workspaces-v2 §4).
        # Bootstrap first (writes markdown for migration-seeded DB rows),
        # then scan (parses any operator-authored files).  Both are idempotent
        # and safe to run on every daemon start.
        from src.profiles.workspace_kind_registry import WorkspaceKindStore

        self.workspace_kind_store = WorkspaceKindStore(self.db, vault_root=self.config.vault_root)
        await self.workspace_kind_store.bootstrap()
        await self.workspace_kind_store.scan()

        # Register facts.md watcher handlers (memory-plugin spec §7).
        # Detects changes to facts files across all vault scopes so they
        # can be synced to the KV backend.  Initially registered with no
        # service (log-only fallback); re-registered with the live
        # MemoryService after plugins load — see post-plugin wiring below.
        from src.facts_handler import register_facts_handlers

        register_facts_handlers(self.vault_watcher)

        # Register vault index hub generator watcher.  When vault files
        # change, regenerate the affected hub file so the Obsidian graph
        # tree stays current.
        from src.vault_index import VaultIndexGenerator, _is_hub_file

        _vault_root = os.path.join(self.config.data_dir, "vault")
        _index_gen = VaultIndexGenerator(_vault_root)

        async def _on_vault_index_changed(changes: list) -> None:
            affected = set()
            for ch in changes:
                # Skip hub files (named after their parent dir)
                fp = Path(os.path.join(_vault_root, ch.rel_path))
                if _is_hub_file(fp):
                    continue
                affected.add(os.path.dirname(ch.rel_path))
            for d in affected:
                _index_gen.update_directory(d)

        self.vault_watcher.register_handler(
            "**/*.md",
            _on_vault_index_changed,
            handler_id="vault-index-updater",
        )

        # V1 memory/*.md watcher handlers removed (roadmap 8.6).
        # Memory file watching is now handled by MemoryPlugin.

        # ------------------------------------------------------------------
        # Playbook subsystem (paused by ``playbooks.enabled=false``).
        #
        # Everything in this block is *not constructed* while paused --
        # PlaybookManager, the timer service, both resume handlers and the
        # orphan-workflow recovery pass.  Compiled JSON under
        # ``{data_dir}/compiled/`` and every ``playbook_runs`` row are left
        # exactly as they are: paused means frozen, never deleted.
        # See docs/specs/implementation/feature-pauses.md §4 (P1-P8, P11).
        # ------------------------------------------------------------------
        if self.config.playbooks.enabled:
            # Register playbook .md watcher handlers (playbooks spec §17).
            # Detects changes to playbook files across all vault scopes so they
            # can be recompiled into executable graphs.  The PlaybookManager
            # handles compilation, versioning, and error recovery (keeping the
            # previous compiled version active on failure — roadmap 5.1.7).
            from src.playbooks.handler import register_playbook_handlers
            from src.playbooks.manager import PlaybookManager

            # Ensure a chat provider is available for playbook compilation.
            # self._chat_provider is only set when use_llm_parser is enabled,
            # so create one from the config if needed.
            playbook_provider = self._chat_provider
            if playbook_provider is None:
                try:
                    from src.chat_providers import create_chat_provider

                    playbook_provider = create_chat_provider(self.config.chat_provider)
                except Exception:
                    logger.warning("No chat provider for playbook compilation")

            self.playbook_manager = PlaybookManager(
                chat_provider=playbook_provider,
                config=self.config,
                event_bus=self.bus,
                data_dir=self.config.data_dir,
                playbook_max_tokens=self.config.chat_provider.playbook_max_tokens,
            )
            # Restore previously compiled playbooks from disk so version numbers
            # continue from where they left off and source-hash change detection
            # can skip recompilation of unchanged files (roadmap 5.1.5).
            await self.playbook_manager.load_from_disk()

            vault_root = os.path.join(self.config.data_dir, "vault")

            # Prune orphans synchronously — this is a fast file-scan with no LLM
            # calls, and we want orphan compiled JSONs out of ``_active`` before
            # trigger dispatch wires up.
            try:
                await self.playbook_manager.prune_orphan_compilations(vault_root)
            except Exception:
                logger.warning("Orphan compiled-playbook prune failed", exc_info=True)

            # Reconcile vault playbooks with the compiled registry: compile any
            # ``.md`` that's present on disk but not yet in the active registry.
            # Runs as a background task because each uncompiled playbook costs
            # ~30s of LLM latency; blocking startup on a fresh vault full of
            # defaults would delay profile sync, Discord bot init, and the HTTP
            # server by many minutes.  Tasks that need a specific playbook
            # should wait for its compile event or rely on the vault-watcher
            # create-event path.
            async def _reconcile_in_background() -> None:
                try:
                    await self.playbook_manager.reconcile_compilations(vault_root)
                except Exception:
                    logger.warning("Playbook compilation reconcile failed", exc_info=True)

            asyncio.create_task(_reconcile_in_background())

            # Wire trigger dispatch: when a playbook's trigger event fires on
            # the bus, create a PlaybookRunner and execute the graph.  Without
            # this, events fire but playbooks never auto-run.
            self.playbook_manager.on_trigger = self._on_playbook_trigger
            subscribed = self.playbook_manager.subscribe_to_events()
            logger.info("Subscribed to %d playbook trigger event(s)", subscribed)

            register_playbook_handlers(
                self.vault_watcher,
                playbook_manager=self.playbook_manager,
            )

            # Timer service (playbooks spec §7, roadmap 5.3.7) — emits synthetic
            # ``timer.*`` events for playbooks with periodic triggers.  Scans the
            # playbook manager for ``timer.{interval}`` triggers and only tracks
            # intervals that have at least one active subscriber.
            from src.timer_service import TimerService

            self.timer_service = TimerService(
                event_bus=self.bus,
                playbook_manager=self.playbook_manager,
                state_path=os.path.join(self.config.data_dir, "timer_state.json"),
            )
            self.timer_service.start()

            # Playbook resume handler (roadmap 5.4.3) — subscribes to
            # ``human.review.completed`` events and resumes paused playbook
            # runs from their saved conversation state.  External systems
            # (Discord buttons, API endpoints) fire the event; this handler
            # validates, creates a Supervisor, and delegates to
            # PlaybookRunner.resume().
            from src.playbooks.resume_handler import PlaybookResumeHandler

            self.playbook_resume_handler = PlaybookResumeHandler(
                db=self.db,
                event_bus=self.bus,
                orchestrator=self,
                playbook_manager=self.playbook_manager,
                config=self.config,
            )
            self.playbook_resume_handler.subscribe()

            # Workflow stage resume handler (Roadmap 7.5.5) — subscribes to
            # ``workflow.stage.completed`` events and automatically resumes
            # coordination playbook runs that are paused waiting for stage
            # completion.  This enables long-running playbooks that span
            # multiple workflow stages with event-triggered resumption.
            from src.workflow_stage_resume_handler import WorkflowStageResumeHandler

            self.workflow_stage_resume_handler = WorkflowStageResumeHandler(
                db=self.db,
                event_bus=self.bus,
                orchestrator=self,
                playbook_manager=self.playbook_manager,
                config=self.config,
            )
            self.workflow_stage_resume_handler.subscribe()

            # Orphan workflow recovery (Roadmap 7.5.6) — detect and recover
            # workflows whose coordination playbook died (crashed, failed,
            # timed out).  Tasks continue independently; this module handles
            # re-emitting missed events and alerting operators.
            from src.orphan_workflow_recovery import OrphanWorkflowRecovery

            self.orphan_workflow_recovery = OrphanWorkflowRecovery(
                db=self.db,
                event_bus=self.bus,
            )
            recovery_summary = await self.orphan_workflow_recovery.recover_on_startup()
            if recovery_summary.get("checked"):
                logger.info(
                    "Orphan workflow recovery: %s",
                    {k: v for k, v in recovery_summary.items() if k != "details"},
                )

        else:
            # Paused: leave every playbook attribute as ``None`` so the
            # ``getattr``/``if x:`` guards at the call sites (shutdown(),
            # run_one_cycle(), src/commands/playbook_commands.py) see a
            # falsy value rather than a missing attribute.
            #
            # NOTE for future maintainers: TimerService is the *sole*
            # producer of ``timer.*``/``cron.*`` bus events and its timer
            # map comes exclusively from ``PlaybookManager.get_all_triggers()``.
            # While playbooks are paused nothing emits those events.  A new
            # periodic consumer must use plugin ``@cron`` (which stays ON via
            # ``PluginRegistry.tick_cron()``) or a hardcoded cascade step --
            # not a ``timer.*`` subscription.
            self.playbook_manager = None
            self.timer_service = None
            self.playbook_resume_handler = None
            self.workflow_stage_resume_handler = None
            self.orphan_workflow_recovery = None
            logger.info(
                "Playbooks subsystem PAUSED (playbooks.enabled=false) — "
                "PlaybookManager/TimerService/resume handlers/workflow recovery "
                "not started; playbook_runs and compiled JSON preserved. "
                "See docs/specs/design/feature-pauses.md"
            )

        # Register override file watcher handlers (memory-scoping spec §5).
        # Detects changes to per-project agent-type override files so they
        # can be re-indexed into agent context.  The handler callback is
        # wired to the OverrideIndexer below (after memory collections init).
        from src.override_handler import register_override_handlers

        register_override_handlers(self.vault_watcher)

        # Register project README.md watcher handler (self-improvement spec §5).
        # Detects changes to project README files so the orchestrator
        # generates/updates per-project summaries in orchestrator memory.
        # Passing vault_root lets the handler resolve summary output paths
        # without guessing from change events.  The event_bus enables
        # notify.readme_summary_updated / notify.readme_summary_failed
        # events for observability and downstream subscribers.
        from src.readme_handler import register_readme_handlers

        register_readme_handlers(
            self.vault_watcher,
            vault_root=self.config.vault_root,
            event_bus=self.bus,
        )

        # Workspace spec/doc change detector (vault.md §4).
        # Monitors project workspace directories for changes to spec and
        # documentation files and generates reference stubs in the vault
        # at vault/projects/{id}/references/spec-{name}.md.  Runs via
        # tick-driven polling (rate-limited internally to once per
        # spec_watcher_poll_interval seconds) called in run_one_cycle().
        from src.workspace_spec_watcher import WorkspaceSpecWatcher

        self.workspace_spec_watcher: WorkspaceSpecWatcher | None = None
        # ``memory.enabled`` is the section master flag — the spec watcher is
        # memory-accumulation plumbing, so it stays down while memory is
        # paused (feature-pauses.md M8).
        if self.config.memory.enabled and self.config.memory.spec_watcher_enabled:
            self.workspace_spec_watcher = WorkspaceSpecWatcher(
                db=self.db,
                bus=self.bus,
                git=self.git,
                vault_projects_dir=self.config.vault_projects,
                file_patterns=self.config.memory.spec_watcher_patterns,
                poll_interval_seconds=self.config.memory.spec_watcher_poll_interval,
                max_excerpt_lines=self.config.memory.spec_watcher_max_excerpt_lines,
                enabled=True,
            )
            logger.info(
                "WorkspaceSpecWatcher initialized (poll=%ds, patterns=%s)",
                self.config.memory.spec_watcher_poll_interval,
                self.config.memory.spec_watcher_patterns,
            )

        # Reference stub LLM enricher (roadmap 6.3.2 — vault.md §4).
        # Subscribes to workspace.spec.changed events emitted by the
        # WorkspaceSpecWatcher and enriches stubs with LLM-generated
        # summaries (Summary, Key Decisions, Key Interfaces).
        from src.reference_stub_enricher import ReferenceStubEnricher

        self.reference_stub_enricher: ReferenceStubEnricher | None = None
        if (
            self.config.memory.enabled
            and self.config.memory.spec_watcher_enabled
            and self.config.memory.stub_enrichment_enabled
        ):
            self.reference_stub_enricher = ReferenceStubEnricher(
                bus=self.bus,
                vault_projects_dir=self.config.vault_projects,
                config=self.config.memory,
                enabled=True,
                max_source_chars=self.config.memory.stub_enrichment_max_source_chars,
                max_tokens=self.config.chat_provider.max_tokens,
            )
            self.reference_stub_enricher.subscribe()
            logger.info("ReferenceStubEnricher initialized and subscribed")

        # MemoryExtractor is now managed by the MemoryPlugin internally.

        # Initialize plugin registry (after DB)
        from src.plugins import PluginRegistry
        from src.plugins.services import build_internal_services

        self.plugin_registry = PluginRegistry(
            db=self.db,
            bus=self.bus,
            config=self.config,
            doctor_registry=self.doctor_registry,
        )

        # Build and inject services for internal plugins
        internal_services = build_internal_services(
            db=self.db,
            git=self.git,
            config=self.config,
        )
        # Expose the vault_watcher so external plugins (e.g. memory) can
        # register their own facts.md / vault-path handlers.  Spec §6.3.
        if self.vault_watcher is not None:
            internal_services["vault_watcher"] = self.vault_watcher
        self.plugin_registry.set_internal_services(internal_services)

        try:
            discovered = await self.plugin_registry.discover_plugins()
            if discovered:
                logger.info("Discovered %d plugins: %s", len(discovered), discovered)
            # Feature pause (feature-pauses.md M1): when the memory
            # subsystem is paused the aq-memory plugin is simply not loaded.
            # The registry is told *what* to skip; it never mutates the
            # plugin's DB status, so re-enabling is a config flip + restart.
            skip_plugins: frozenset[str] = (
                frozenset() if self.config.memory.enabled else frozenset({"aq-memory", "memory"})
            )
            if skip_plugins:
                logger.info(
                    "Memory subsystem PAUSED (memory.enabled=false) — aq-memory plugin "
                    "not loaded; L1/L2 prompt tiers empty; reflection off; data "
                    "preserved. See docs/specs/design/feature-pauses.md"
                )
            loaded = await self.plugin_registry.load_all(skip=skip_plugins)
            if loaded:
                logger.info("Loaded %d plugins", loaded)
        except Exception as e:
            logger.error("Plugin initialization failed: %s", e, exc_info=True)

        # Build the in-memory semantic tool index for find_applicable_tool.
        # Runs after plugins are loaded so plugin-contributed tools are included.
        if hasattr(self, "_tool_registry") and self._tool_registry:
            try:
                await self._tool_registry.build_tool_index(self.config.memory)
            except Exception as e:
                logger.warning("Tool index build failed (semantic search disabled): %s", e)

        # Wire MemoryService to facts.md watcher handlers (spec §7).
        #
        # The facts handlers were registered earlier (before plugins loaded)
        # with service=None — the handler falls back to logging only.  Now
        # that a memory plugin has been loaded and registered itself via
        # ``ctx.register_service("memory", ...)``, re-register the handlers
        # with the actual service so KV sync works.  register_facts_handlers()
        # is idempotent (same handler IDs are reused), so this is safe.
        mem_svc = self.plugin_registry.get_service("memory")
        if mem_svc and getattr(mem_svc, "available", False):
            from src.facts_handler import register_facts_handlers

            register_facts_handlers(self.vault_watcher, service=mem_svc)
            logger.info("Wired memory service to facts.md watcher handlers")

        # Now that plugins are loaded we know the full set of tools the
        # embedded agent-queue MCP server exposes.  Re-register the
        # mcp-server vault handlers with the on_reload hook (so a vault
        # change triggers a tool-catalog refresh) and run the initial
        # parallel probe pass.
        from src.mcp_registration import get_effective_exclusions
        from src.profiles.mcp_catalog import (
            build_agent_queue_tools,
            make_reload_hook,
        )
        from src.profiles.mcp_registry import register_mcp_server_handlers

        _mcp_exclusions = get_effective_exclusions(config=self.config)
        _plugin_tool_defs: list[dict] = []
        if hasattr(self, "plugin_registry") and self.plugin_registry:
            try:
                _plugin_tool_defs = self.plugin_registry.get_all_tool_definitions()
            except Exception:
                logger.debug("Could not enumerate plugin tool definitions", exc_info=True)

        def _resolve_agent_queue_tools():
            return build_agent_queue_tools(
                excluded=_mcp_exclusions,
                plugin_tools=_plugin_tool_defs,
            )

        register_mcp_server_handlers(
            self.vault_watcher,
            self.mcp_registry,
            vault_root=self.config.vault_root,
            on_reload=make_reload_hook(
                self.mcp_registry,
                self.mcp_tool_catalog,
                builtin_resolver=_resolve_agent_queue_tools,
            ),
        )
        # Stash the resolver so the explicit probe command (B5) can reuse it.
        self._mcp_builtin_resolver = _resolve_agent_queue_tools

        # HookEngine + RuleManager removed (playbooks spec §13 Phase 3).
        # All automation is now handled by playbooks — see PlaybookExecutor
        # and TimerService.

        # Start config file watcher for hot-reloading
        if self.config._config_path:
            self._config_watcher = ConfigWatcher(
                config_path=self.config._config_path,
                event_bus=self.bus,
                current_config=self.config,
            )
            self.bus.subscribe("config.reloaded", self._on_config_reloaded)
            self._config_watcher.start()

        # Live event-gate resolution (WG-3).  Sweep is the restart-safe
        # backstop; this subscription handles the fast path.
        if self.config.work_graph.gate_sweep_interval_seconds > 0:
            await self._subscribe_event_gates()

        # MemoryExtractor is now managed by the MemoryPlugin.

        # Take the vault watcher's initial snapshot now that all subsystems
        # have had a chance to register their path handlers.  The first
        # check() call in run_one_cycle() will detect only changes that
        # occur *after* this point.
        if self.vault_watcher:
            await self.vault_watcher.check()

        # One-shot inline-mcp-servers migration.  Runs BEFORE the profile
        # scan so the scan sees the new ``list[str]`` shape and writes
        # the right thing to the DB.  Idempotent — already-migrated
        # profiles are no-ops.  Updates the in-memory registry so the
        # subsequent catalog probe sees the extracted entries on the
        # same startup.
        from src.profiles.mcp_inline_migration import (
            migrate_vault_profiles as migrate_inline_vault,
            migrate_yaml_config_profiles as migrate_inline_yaml,
        )

        try:
            migrate_inline_vault(self.config.vault_root, registry=self.mcp_registry)
        except Exception:
            logger.warning("Vault inline mcp_servers migration failed", exc_info=True)
        try:
            migrate_inline_yaml(
                self.config.agent_profiles,
                self.config.vault_root,
                registry=self.mcp_registry,
            )
        except Exception:
            logger.warning("YAML inline mcp_servers migration failed", exc_info=True)

        # Startup scan: sync any existing profile.md files from the vault
        # to the database.  The VaultWatcher's initial check() only takes
        # a snapshot (no dispatch), so pre-existing profile files would
        # not be synced without this step.  This ensures profile DB rows
        # are always consistent with vault files after a daemon restart.
        from src.profiles.sync import scan_and_sync_existing_profiles

        await scan_and_sync_existing_profiles(
            self.config.vault_root,
            self.db,
            event_bus=self.bus,
        )

        # Startup scan: load every mcp-server file under the vault into the
        # in-memory registry.  Same rationale as the profile scan above —
        # the watcher's initial snapshot does not dispatch, so pre-existing
        # files need an explicit pass.  Errors are logged per-file; one
        # malformed entry never blocks the rest.
        from src.profiles.mcp_catalog import populate_catalog
        from src.profiles.mcp_registry import load_from_vault as load_mcp_registry

        load_mcp_registry(self.mcp_registry, self.config.vault_root)

        # Same for harness files (vault/harnesses/*.md).  A harness is one
        # CLI coding agent as markdown — the file is the source of truth,
        # so this is a projection, not a cache of a DB table.
        from src.sessions.harness_registry import load_from_vault as load_harness_registry

        try:
            load_harness_registry(self.harness_registry, self.config.vault_root)
        except Exception:
            logger.warning("Harness registry load failed", exc_info=True)

        # Probe every registry entry (parallel) and populate the in-memory
        # tool catalog.  Builtin entries are resolved in-process via the
        # resolver set up above; external entries hit the network.  A
        # hung server cannot stall this pass beyond the 10s per-probe
        # timeout enforced inside ``probe_server``.
        try:
            await populate_catalog(
                self.mcp_registry,
                self.mcp_tool_catalog,
                builtin_resolver=getattr(self, "_mcp_builtin_resolver", None),
            )
        except Exception:
            logger.warning("MCP tool catalog populate failed", exc_info=True)

        # Startup scan: generate orchestrator summaries for all existing
        # project READMEs (self-improvement spec §5).  The VaultWatcher
        # handles changes going forward, but pre-existing files need this
        # one-time scan.  Summaries are written to
        # vault/orchestrator/memory/project-{id}.md and are skipped when
        # already up-to-date (mtime comparison), keeping startup fast.
        from src.readme_handler import scan_and_generate_readme_summaries

        try:
            await scan_and_generate_readme_summaries(self.config.vault_root)
        except Exception as e:
            logger.warning("Startup README scan failed: %s", e)

    async def _recover_stale_state(self, skip_task_ids: set[str] | None = None) -> None:
        """Reset any in-flight work from a previous daemon run.

        ``skip_task_ids`` are tasks owned by a session that was adopted on
        boot (session-runtime §4.4).  Their agents are genuinely still
        working, so resetting them would abort live work — the one case
        where the blanket reset below is wrong.  ``aq daemon start --reset``
        forces the old behavior by skipping adoption entirely.

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
        skip_task_ids = skip_task_ids or set()
        # Agents holding an adopted task keep their BUSY state and their
        # workspace lock — the session behind them never stopped.
        protected_agents: set[str] = set()
        if skip_task_ids:
            for tid in skip_task_ids:
                t = await self.db.get_task(tid)
                if t is not None and t.assigned_agent_id:
                    protected_agents.add(t.assigned_agent_id)
            logger.info(
                "Recovery: %d task(s) owned by adopted sessions are exempt from reset",
                len(skip_task_ids),
            )

        # Reset BUSY agents to IDLE
        agents = await self.db.list_agents()
        for a in agents:
            if a.id in protected_agents:
                continue
            if a.state == AgentState.BUSY:
                logger.info("Recovery: resetting agent '%s' from %s to IDLE", a.name, a.state.value)
                await self.db.update_agent(a.id, state=AgentState.IDLE, current_task_id=None)

        # Reap idle agents whose profile_id no longer exists in agent_profiles
        # (e.g. profile was deleted while the daemon was down).  Per spec §6:
        # the reconciler does not reap mid-run; reaping happens at startup.
        profile_ids = {p.id for p in await self.db.list_profiles()}
        for a in agents:
            if a.state == AgentState.IDLE and a.profile_id not in profile_ids:
                logger.info(
                    "Recovery: deleting idle agent '%s' (profile '%s' no longer exists)",
                    a.name,
                    a.profile_id,
                )
                await self.db.delete_agent(a.id)

        # Reap excess idle agents over project.max_concurrent_agents.
        # Attribute idle agents to projects via their workspace lock, then
        # delete oldest-first beyond the cap.  Important for handling
        # max_concurrent_agents being lowered while the daemon was down.
        all_workspaces = await self.db.list_workspaces()
        ws_owner = {
            w.locked_by_agent_id: w.project_id for w in all_workspaces if w.locked_by_agent_id
        }
        projects = await self.db.list_projects()
        max_by_project = {p.id: p.max_concurrent_agents for p in projects}
        idle_by_project: dict[str, list] = {}
        for a in agents:
            if a.state != AgentState.IDLE:
                continue
            pid = ws_owner.get(a.id)
            if pid is None:
                continue
            idle_by_project.setdefault(pid, []).append(a)
        for pid, idle_agents in idle_by_project.items():
            cap = max_by_project.get(pid)
            if cap is None or len(idle_agents) <= cap:
                continue
            # Sort oldest-first by created_at.
            idle_agents.sort(key=lambda a: getattr(a, "created_at", 0) or 0)
            excess = idle_agents[: len(idle_agents) - cap]
            for a in excess:
                logger.info(
                    "Recovery: deleting excess idle agent '%s' (project=%s over cap=%d)",
                    a.name,
                    pid,
                    cap,
                )
                await self.db.delete_agent(a.id)

        # Release all workspace locks and clean orphaned sentinels.
        # After a restart no agents are running, so all DB locks are stale.
        # Also remove sentinel files from ALL workspaces — they may exist
        # even when the DB lock was already released (e.g. _prepare_workspace
        # acquired + detected sentinel + released lock, but never deleted file).
        #
        # *Legacy* branch-isolated worktree rows (source_type=WORKTREE with
        # no slot_index) are cleaned up: the git worktree is removed and the
        # record deleted.  Those were created dynamically per task and should
        # not persist across restarts.
        #
        # **Slot rows are not.**  A slot is durable inventory (design §3.4,
        # §6 "adopt, don't delete"): its directory holds warm caches and
        # possibly uncommitted work, and `git worktree remove` here — with
        # its `--force` fallback — would destroy both on every daemon start.
        # They are unlocked and left alone; `_cleanup_worktree_workspace`
        # short-circuits for them, and the sweep below re-registers their
        # slot->base mapping so the git mutex keeps serializing.
        # Full adoption (git worktree list cross-check, prune, repair) is
        # phase 4.
        all_workspaces = await self.db.list_workspaces()
        self._recover_worktree_base_paths(all_workspaces)
        for ws in all_workspaces:
            if ws.locked_by_agent_id in protected_agents:
                # An adopted session is still working in here; removing the
                # worktree or the sentinel would pull the floor out from
                # under a live agent.
                continue
            if ws.is_slot:
                # Slot directories keep their own `.aq-worktree.json`; the
                # legacy `.agent-queue-lock` sentinel is not used there.
                if ws.locked_by_agent_id or ws.locked_by_task_id:
                    logger.info(
                        "Recovery: releasing worktree slot '%s' at %s "
                        "(directory and branch preserved)",
                        ws.id,
                        ws.workspace_path,
                    )
                    await self.db.release_workspace(ws.id)
                continue
            self._remove_sentinel(ws.workspace_path)
            if ws.source_type == RepoSourceType.WORKTREE:
                logger.info(
                    "Recovery: cleaning up legacy worktree workspace '%s' at %s",
                    ws.id,
                    ws.workspace_path,
                )
                await self._cleanup_worktree_workspace(ws)
            elif ws.locked_by_agent_id and ws.locked_by_agent_id not in protected_agents:
                logger.info(
                    "Recovery: releasing workspace lock '%s' (was locked by %s)",
                    ws.id,
                    ws.locked_by_agent_id,
                )
                await self.db.release_workspace(ws.id)

        # Reset IN_PROGRESS tasks back to READY so they get re-scheduled.
        #
        # Container tasks are excluded. A plan parent or graph parent is
        # IN_PROGRESS *because* its children are running — not because an
        # agent died holding it. Resetting one to READY makes the scheduler
        # pick it up, take the project's exclusive project-repo lock, and
        # launch an agent on a prompt whose whole content is the parent
        # title, blocking its own children while _phase_verify judges the
        # resulting git state. A graph parent stays IN_PROGRESS for the
        # entire graph lifetime, so the window is days, not minutes.
        #
        # They still auto-complete correctly: _check_plan_parent_completion
        # keys off *having subtasks*, not off is_plan_subtask.
        tasks = await self.db.list_tasks(status=TaskStatus.IN_PROGRESS)
        for t in tasks:
            if t.id in skip_task_ids:
                logger.info(
                    "Recovery: task '%s' kept IN_PROGRESS — its session was adopted", t.id
                )
                continue
            if await self.db.get_subtasks(t.id):
                logger.info(
                    "Recovery: leaving container task '%s' (%s) IN_PROGRESS — it has subtasks",
                    t.id,
                    t.title,
                )
                continue
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
        # The migration is idempotent at the per-profile level — profiles
        # that already have vault files are skipped inside the migration
        # function.  Run unconditionally whenever DB profiles exist so a
        # YAML-defined profile that does not yet have a vault file gets
        # one written, even when other profiles (e.g. the bundled
        # supervisor / claude-* profiles created by ensure_vault_structure)
        # already exist.
        if all_profiles:
            from src.profiles.migration import migrate_db_profiles_to_vault

            logger.info(
                "Profile auto-migration triggered: %d DB profile(s) found "
                "(per-profile idempotent skip applies)",
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

        # Phase 2 migration: passive rules → vault memory guidance files.
        # This runs unconditionally (idempotent) because passive rules may
        # already be in the vault playbooks/ dirs and need moving to memory/.
        from src.vault import migrate_passive_rules_to_memory

        passive_report = migrate_passive_rules_to_memory(data_dir)
        if passive_report["moved"]:
            logger.info(
                "Passive rule migration: %d moved to vault memory guidance",
                passive_report["moved"],
            )

        # Per-profile directories (vault/agent-types/{profile_id}/).
        # Skip project-scoped profiles — their vault home is
        # projects/{project}/agent-types/{type}/, managed elsewhere.
        for profile in all_profiles:
            if not profile.id.startswith("project:"):
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

    async def shutdown(self) -> None:
        """Gracefully shut down all subsystems in dependency order.

        Shutdown order:
        1. Wait for running agent tasks (with a 10s timeout so we don't
           hang indefinitely if an adapter is stuck).
        2. Stop watchers, timers, and resume handlers.
        3. Close the database connection.

        The order matters: tasks use the DB, so we must wait for them
        to finish before closing it.
        """
        await self.wait_for_running_tasks(timeout=10)
        if self.vault_watcher:
            try:
                await self.vault_watcher.stop()
            except Exception as e:
                logger.warning("Vault watcher shutdown error: %s", e)
        if self._config_watcher:
            await self._config_watcher.stop()
        if self.timer_service:
            self.timer_service.stop()
        if hasattr(self, "playbook_resume_handler") and self.playbook_resume_handler:
            self.playbook_resume_handler.shutdown()
        if hasattr(self, "workflow_stage_resume_handler") and self.workflow_stage_resume_handler:
            self.workflow_stage_resume_handler.shutdown()
        # MemoryExtractor shutdown is handled by the MemoryPlugin.
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

        7a. **Timer service tick** — emit synthetic timer.* events for
            playbooks with periodic triggers (playbooks spec §7).
        7b. **Plugin cron tick** — run plugin cron jobs.
        7c. **Vault watcher tick** — poll the vault directory tree for file
            changes and dispatch to registered handlers (playbooks spec §17).
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

            # 2b. Sweep gates: resolve satisfied timer/pr-merged/ci-run/event/
            #     task gates and expire overdue ones.  Runs before promotion
            #     so a freshly resolved gate unblocks its waiters in the same
            #     cycle (work-graph spec §6.1).  No-op until Wave 2 lane WG-3.
            #     Own try/except: the sweep polls `gh`, and a failure there
            #     must not skip promotion (step 3) for the whole cycle.
            try:
                await self._sweep_gates()
            except Exception:
                logger.error("Gate sweep error", exc_info=True)

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

            # 7a. Run timer service tick — emit synthetic timer.* events for
            # playbooks with periodic triggers (playbooks spec §7).
            if self.timer_service:
                try:
                    await self.timer_service.tick()
                except Exception as e:
                    logger.warning("Timer service tick failed: %s", e)

            # 7b. Run plugin cron jobs (plain async functions, not LLM prompts).
            if hasattr(self, "plugin_registry") and self.plugin_registry:
                await self.plugin_registry.tick_cron()

            # 7c. Poll vault watcher for filesystem changes (playbooks spec §17).
            # Uses tick-driven polling rather than a background loop — check()
            # has built-in rate limiting via poll_interval and debounce, so
            # calling it each cycle is cheap.
            if self.vault_watcher:
                try:
                    await self.vault_watcher.check()
                except Exception as e:
                    logger.warning("VaultWatcher check failed: %s", e)

            # 7d. Workspace spec/doc change detector (vault.md §4).
            # Polls project workspace directories for spec/doc file changes
            # and writes reference stubs to vault/projects/{id}/references/.
            # Rate-limited internally to once per spec_watcher_poll_interval.
            if self.workspace_spec_watcher:
                try:
                    await self.workspace_spec_watcher.check()
                except Exception as e:
                    logger.warning("WorkspaceSpecWatcher check failed: %s", e)

            # 7e. Periodic orphan workflow check (Roadmap 7.5.6).
            # Detects workflows whose coordination playbook died and emits
            # workflow.orphaned events.  Rate-limited internally (~60s).
            if hasattr(self, "orphan_workflow_recovery") and self.orphan_workflow_recovery:
                try:
                    await self.orphan_workflow_recovery.check_periodic()
                except Exception as e:
                    logger.warning("Orphan workflow check failed: %s", e)

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

            # 11. V1 memory compaction removed (roadmap 8.6).
            # Memory lifecycle is now managed by MemoryPlugin.

            # 12. Check paused playbook runs for timeout (roadmap 5.4.4).
            #     Sweeps paused runs and handles expired timeouts — either
            #     transitioning to a timeout node or marking as timed_out.
            await self._check_paused_playbook_timeouts()

            # ── Phase 4: Framework-overhaul substrate steps ─────────────────
            # Wave 0 landed these as flag-gated no-ops so the Wave 2 lanes
            # each fill in one body without touching this file again.
            # (``_sweep_gates`` is the exception — work-graph spec §6.1
            # requires it at step 2b, before promotion, so a freshly
            # resolved gate unblocks its waiters in the same cycle.  It is
            # called up there, not here.)
            # See docs/analysis/execution-plan.md §1.1.
            await self._reap_worktree_slots()
            await self._reconcile_sessions()
            await self._deliver_messages()
            await self._revoke_expired_tokens()
        except Exception:
            logger.error("Scheduler cycle error", exc_info=True)

    # -----------------------------------------------------------------------
    # Framework-overhaul cascade stubs (Wave 0 substrate).
    #
    # Each of these is called from ``run_one_cycle`` and does nothing until
    # its owning lane fills the body in.  They exist now so five parallel
    # agents never have to edit this file's cascade — see
    # docs/analysis/execution-plan.md §1.1.
    #
    # Every stub is a flag check followed by an immediate return.  All the
    # flags default off, so today every one of them is inert.
    # -----------------------------------------------------------------------

    async def _sweep_gates(self) -> None:
        """Resolve satisfied gates and expire overdue ones.

        Wave 2 lane 2C fills this in — see
        docs/specs/implementation/work-graph.md §6.2 (``_sweep_gates``).
        Expected body: ``expire_open_gates(now)``; resolve ``timer`` and
        ``task`` gates; poll ``pr-merged``/``ci-run`` on the 60 s approval
        throttle; re-check ``event`` gates against persisted events; emit
        ``gate.resolved`` / ``gate.expired`` / ``task.unblocked``.

        Gating note (WG-1): this used to check
        ``work_graph.blocked_state_authoritative``, but §9's rollout puts
        gates at stage (3) — *after* the stage-(2) flip of that flag.  It is
        gated on ``gate_sweep_interval_seconds`` (``<= 0`` disables
        sweeping) so the two rollout stages are independent.

        Order matters: the sweep runs at cascade step 2b (before
        ``_check_defined_tasks``) so a freshly resolved gate unblocks its
        waiter in the same cycle.
        """
        interval = self.config.work_graph.gate_sweep_interval_seconds
        if interval <= 0:
            return
        now = time.time()
        if now - self._last_gate_sweep < interval:
            return
        self._last_gate_sweep = now

        # 1. Resolve satisfied gates by type.  Timers first so a "past
        #    timeout_at" timer resolves rather than being expired below.
        await self._sweep_resolve_timer_gates(now)
        await self._sweep_resolve_task_gates()
        await self._sweep_resolve_pr_ci_gates()
        await self._sweep_resolve_event_gates()

        # 2. Expire overdue open gates — expired keeps blocking (design
        #    §5.4), so waiters need no recompute, but the event stream must
        #    show it.
        try:
            expired = await self.db.expire_open_gates(now)
        except Exception:
            logger.exception("_sweep_gates: expire_open_gates failed")
            expired = []
        for gate_id in expired:
            try:
                gate = await self.db.get_gate(gate_id)
                if gate is None:
                    continue
                payload = {
                    "gate_id": gate_id,
                    "project_id": gate["project_id"],
                    "gate_type": gate["gate_type"],
                    "timeout_at": gate["timeout_at"],
                }
                await self.bus.emit("gate.expired", payload)
                await self.db.log_event(
                    "gate.expired",
                    project_id=gate["project_id"],
                    payload=gate_id,
                )
            except Exception:
                logger.debug("_sweep_gates: expiry emit failed", exc_info=True)

    async def _resolve_gate_and_emit(
        self, gate_id: str, *, resolved_by: str, resolution: str = ""
    ) -> None:
        """Resolve *gate_id* through the DB and emit the audit + bus events.

        Central helper for the sweep — any resolution path that flows
        through here gets the same ``gate.resolved`` + ``task.unblocked``
        events for free.  Idempotent: an already-resolved gate is a no-op.
        """
        gate = await self.db.get_gate(gate_id)
        if gate is None or gate["status"] == "resolved":
            return
        flipped = await self.db.resolve_gate(
            gate_id, resolved_by=resolved_by, resolution=resolution
        )
        payload = {
            "gate_id": gate_id,
            "project_id": gate["project_id"],
            "resolved_by": resolved_by,
            "resolution": resolution,
            "unblocked_task_ids": sorted(flipped),
            "gate_type": gate["gate_type"],
        }
        try:
            await self.bus.emit("gate.resolved", payload)
        except Exception:
            logger.debug("_sweep_gates: bus emit failed", exc_info=True)
        try:
            await self.db.log_event(
                "gate.resolved",
                project_id=gate["project_id"],
                payload=gate_id,
            )
        except Exception:
            logger.debug("_sweep_gates: log_event failed", exc_info=True)
        # ``resolve_gate`` already wrote the audit rows via
        # ``log_blocked_flips``; also emit on the bus so playbooks that
        # subscribe to ``task.blocked``/``task.unblocked`` can fire
        # (WG-4, work-graph §9.1 note #263).
        await self._emit_blocked_flips(flipped, reason="gate:resolved")

    async def _sweep_resolve_timer_gates(self, now: float) -> None:
        """Resolve ``timer`` gates whose ``timeout_at`` has passed.

        ``timer`` uses ``timeout_at`` as the *fire* time (not a hard
        deadline).  A separate expiry pass runs first and would have
        marked past-timeout ``timer`` gates as ``expired`` — but timers
        are the one gate kind where "past timeout" means "satisfied", not
        "failed", so we override that by picking any remaining open
        ``timer`` gates whose fire time has arrived and resolving them.
        Practically, we look at *open* timer gates only: expiry already
        removed the ones with a passed ``timeout_at`` — so timers must be
        resolved before the expiry pass to preserve the "fire" semantics.

        To keep the ordering simple we scan open timers here and resolve
        those with ``timeout_at <= now`` regardless of what expiry did in
        step 1 (idempotent).  This is the "expire → resolve" order the
        spec §6.2 documents.
        """
        try:
            timers = await self.db.list_open_gates_by_type("timer")
        except Exception:
            logger.exception("_sweep_gates: list_open_gates_by_type(timer) failed")
            return
        for gate in timers:
            if gate["timeout_at"] is not None and gate["timeout_at"] <= now:
                await self._resolve_gate_and_emit(
                    gate["id"], resolved_by="sweep:timer", resolution="fired"
                )

    async def _sweep_resolve_task_gates(self) -> None:
        """Resolve ``task`` gates whose awaited task is COMPLETED."""
        try:
            gates = await self.db.list_open_gates_by_type("task")
        except Exception:
            logger.exception("_sweep_gates: list_open_gates_by_type(task) failed")
            return
        for gate in gates:
            await_id = gate.get("await_id")
            if not await_id:
                continue
            dep = await self.db.get_task(await_id)
            if dep is None:
                continue
            if getattr(dep.status, "value", dep.status) == "COMPLETED":
                await self._resolve_gate_and_emit(
                    gate["id"], resolved_by="sweep:task", resolution=await_id
                )

    async def _sweep_resolve_pr_ci_gates(self) -> None:
        """Resolve ``pr-merged``/``ci-run`` gates via ``_poll_pr_merged``."""
        for gate_type in ("pr-merged", "ci-run"):
            try:
                gates = await self.db.list_open_gates_by_type(gate_type)
            except Exception:
                logger.exception(
                    "_sweep_gates: list_open_gates_by_type(%s) failed", gate_type
                )
                continue
            for gate in gates:
                pr_url = gate.get("await_id")
                if not pr_url:
                    continue
                merged = await self._poll_pr_merged(
                    pr_url, project_id=gate["project_id"]
                )
                if merged is True:
                    await self._resolve_gate_and_emit(
                        gate["id"],
                        resolved_by=f"sweep:{gate_type}",
                        resolution=pr_url,
                    )

    async def _sweep_resolve_event_gates(self) -> None:
        """Backstop for ``event`` gates: match persisted events after the
        gate's creation watermark.

        The EventBus subscription (``_subscribe_event_gates``) is the fast
        path — this is the restart-safe backstop, so an event emitted while
        the daemon was down still resolves its gate.
        """
        try:
            gates = await self.db.list_open_gates_by_type("event")
        except Exception:
            logger.exception("_sweep_gates: list_open_gates_by_type(event) failed")
            return
        for gate in gates:
            event_type = gate.get("await_id")
            if not event_type:
                continue
            try:
                rows = await self.db.get_recent_events(
                    limit=100, event_type=event_type, since=gate["created_at"]
                )
            except Exception:
                logger.debug(
                    "_sweep_gates: get_recent_events failed", exc_info=True
                )
                continue
            if rows:
                await self._resolve_gate_and_emit(
                    gate["id"], resolved_by="sweep:event", resolution=event_type
                )

    async def _subscribe_event_gates(self) -> None:
        """Register the EventBus handler that resolves open ``event`` gates.

        Called once from :meth:`initialize`.  Live path: an event arrives,
        we scan open ``event`` gates whose ``await_id`` matches and resolve
        them.  The sweep remains the backstop.
        """
        if self._gate_event_unsub is not None:
            return

        async def _on_event(data: dict) -> None:
            event_type = data.get("_event_type")
            if not event_type or event_type.startswith("gate."):
                return  # avoid a resolve → gate.resolved → resolve loop
            try:
                gates = await self.db.list_open_gates_by_type("event")
            except Exception:
                logger.debug("event-gate handler: list failed", exc_info=True)
                return
            for gate in gates:
                if gate.get("await_id") == event_type:
                    await self._resolve_gate_and_emit(
                        gate["id"],
                        resolved_by="bus:event",
                        resolution=event_type,
                    )

        self._gate_event_unsub = self.bus.subscribe("*", _on_event)

    async def _reap_worktree_slots(self) -> None:
        """Break expired merge-slot leases and reap retired worktree slots.

        Wave 2 lane 2B fills this in — see
        docs/specs/implementation/worktree-execution.md §6.2 (cascade steps
        7d/7e).  Expected body: ``break_expired_merge_slots`` plus a
        rate-limited retired-slot sweep and ``prune_branches``.
        """
        if not self.config.worktrees.enabled:
            return

    async def _reconcile_sessions(self) -> None:
        """Reconcile desired vs. actual agent sessions — one reconciler tick.

        Deterministic and LLM-free, like every other cascade step.  The
        reconciler swallows its own per-step failures (see
        ``SessionReconciler.tick``), so a wedged provider degrades one step
        rather than stopping the cascade.

        See docs/specs/design/session-runtime.md §4.
        """
        if not self.config.sessions.enabled:
            return
        await self.session_reconciler.tick()

    async def _deliver_messages(self) -> None:
        """Deliver queued inter-agent messages to their recipients.

        Wave 2 lane 2D owns the queue; the delivery engine itself is
        explicitly out of Wave 2 scope (it needs named sessions).  See
        docs/specs/implementation/supervisor-agent.md §10 (``MessagesConfig``).
        """
        if not self.config.messages.enabled:
            return

    async def _revoke_expired_tokens(self) -> None:
        """Sweep expired API session tokens out of ``api_session_tokens``.

        Wave 3 (aq-surface S2) fills this in — see
        docs/specs/implementation/aq-surface.md §4.1
        (``SessionTokenStore.revoke_expired``).  Gated on either flag:
        tokens are minted by sessions, and enforcement can be on
        independently.
        """
        if not (self.config.sessions.enabled or self.config.api_auth.require_session_token):
            return

    async def _on_config_reloaded(self, data: dict) -> None:
        """Handle config.reloaded events from the ConfigWatcher.

        Updates the orchestrator's config reference and propagates changes
        to subsystems that cache config values (budget manager, etc.).
        """
        config = data.get("config")
        if config is None:
            return
        self.config = config
        # Update budget manager if global budget changed
        if self.budget and config.global_token_budget_daily is not None:
            self.budget._global_budget = config.global_token_budget_daily
        logger.info(
            "Config reloaded: updated sections: %s",
            ", ".join(data.get("changed_sections", [])),
        )

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
        # Reconciler runs first: ensures the agents table has idle rows
        # for any project with dispatchable READY tasks, subject to
        # project.max_concurrent_agents.  See spec §4.1.
        rep = await self._agent_reconciler.reconcile()
        if rep.created or rep.reassigned:
            logger.info(
                "reconciler: created=%d reassigned=%d skipped=%d",
                len(rep.created),
                len(rep.reassigned),
                len(rep.skipped),
            )

        # Build a consistent point-in-time snapshot of all system state the
        # scheduler needs.  Each query reads from the DB independently (no
        # transaction), but the ~5s cycle frequency means the snapshot is
        # close enough to consistent for scheduling purposes.
        projects = await self.db.list_projects()
        # Non-terminal tasks only: the scheduler reads READY rows plus the
        # ASSIGNED/IN_PROGRESS rows behind busy-agent counts and sync
        # blocking — never completed ones.  The unfiltered table scan was
        # 1.7 s/cycle at 100k tasks (performance-assessment.md §1.1).
        tasks = await self.db.list_active_tasks()
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
        #
        # Pass the slot cap under worktree mode for the same reason the
        # reconciler does (worktree-execution §6.7): the 1-arg form counts
        # *inventory*, which under worktree mode means counting the base —
        # a row no agent can ever acquire — as available, while ignoring the
        # slots that can be created on demand.
        worktrees_enabled = self._worktrees_enabled()
        workspace_counts: dict[str, int] = {}
        for p in projects:
            workspace_counts[p.id] = await self.db.count_available_workspaces(
                p.id,
                worktree_slot_cap=(
                    self._project_slot_cap(p) if worktrees_enabled else None
                ),
            )

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

        # Load active project constraints (exclusive, pause_scheduling,
        # max_agents_by_type) so the scheduler can enforce them.
        constraint_list = await self.db.list_project_constraints()
        constraint_map = {c.project_id: c for c in constraint_list}

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
            provider_cooldowns=self._provider_cooldowns,
            project_constraints=constraint_map,
            now=time.time(),
            affinity_wait_seconds=self.config.scheduling.affinity_wait_seconds,
        )

        actions = Scheduler.schedule(state)
        # Cache the state snapshot + supporting maps so ``_cmd_explain_task``
        # can answer between ticks with the same "why not scheduled" reasons
        # the scheduler-blocker log uses (WG-4, work-graph §6.3).
        self._last_scheduler_state = state
        self._last_scheduler_workspace_counts = dict(workspace_counts)
        self._last_scheduler_idle_by_project = _idle_by_project(state)
        # Log *why* READY tasks didn't get assigned, but only when the set of
        # unassignable reasons changes. Silent scheduler no-ops previously
        # left tasks stuck in READY forever with zero log signal.
        self._log_scheduler_blockers(state, actions, workspace_counts)
        return actions

    # Per-task reason cache to dedupe scheduler-blocker logs across ticks.
    # Maps task_id → last-emitted blocker string; logs only when the reason
    # changes (including clears via removal).
    _scheduler_blocker_reasons: dict[str, str] = {}

    def _log_scheduler_blockers(
        self,
        state: "SchedulerState",
        actions: list,
        workspace_counts: dict[str, int],
    ) -> None:
        """Log changes in which READY tasks can't be scheduled and why.

        Called after every scheduler tick. Dedupes via
        ``_scheduler_blocker_reasons`` so an unassignable task logs once, not
        every 5s. Logs again if the *reason* changes, and logs a "cleared"
        line when the task finally assigns or otherwise leaves READY.
        """
        assigned_task_ids = {a.task_id for a in actions}
        ready_tasks = [t for t in state.tasks if t.status == TaskStatus.READY]

        # Build a per-project view of idle agents so each task reason is
        # concrete ("0 idle agents on project foo" vs generic).
        idle_by_project = _idle_by_project(state)

        current_reasons: dict[str, str] = {}
        for task in ready_tasks:
            if task.id in assigned_task_ids:
                continue  # assigned this tick, not stuck
            reason = self._describe_task_blocker(task, state, workspace_counts, idle_by_project)
            if reason:
                current_reasons[task.id] = reason

        # Emit diffs: newly blocked, reason-changed, or newly unblocked.
        for task_id, reason in current_reasons.items():
            if self._scheduler_blocker_reasons.get(task_id) != reason:
                logger.info("scheduler blocked task=%s reason=%s", task_id, reason)
                self._scheduler_blocker_reasons[task_id] = reason

        # Clear any tasks that used to be blocked but aren't anymore.
        cleared = set(self._scheduler_blocker_reasons) - set(current_reasons)
        for task_id in cleared:
            logger.info(
                "scheduler unblocked task=%s (prev=%s)",
                task_id,
                self._scheduler_blocker_reasons[task_id],
            )
            del self._scheduler_blocker_reasons[task_id]

    def _describe_task_blocker(
        self,
        task,
        state: "SchedulerState",
        workspace_counts: dict[str, int],
        idle_by_project: dict[str, int],
    ) -> str | None:
        """Best-effort reason string for why *task* wasn't scheduled this tick.

        Thin wrapper over :func:`src.explain.build_capacity_reasons` so this
        method, ``_cmd_explain_task`` and the dashboard share one source of
        truth (WG-4, work-graph §6.3).  Returns the first capacity reason's
        ``detail`` string, or the "ready but not picked" fallback.
        """
        from src.explain import build_capacity_reasons

        reasons = build_capacity_reasons(task, state, workspace_counts, idle_by_project)
        if reasons:
            return reasons[0]["detail"]
        return "ready but not picked this tick (capacity/priority ordering)"

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
