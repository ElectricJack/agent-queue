"""Execution mixin — the agent execution pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from src.database.queries.hierarchy_queries import HierarchyError
from src.logging_config import CorrelationContext
from src.task_summary import write_task_summary
from src.discord.notifications import format_task_started
from src.notifications.builder import build_agent_summary, build_task_detail
from src.notifications.events import (
    AgentQuestionEvent,
    PlanAwaitingApprovalEvent,
    PRCreatedEvent,
    TaskBlockedEvent,
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskMessageEvent,
    TaskStartedEvent,
    TaskThreadCloseEvent,
    TaskThreadOpenEvent,
)
from src.profiles.sync import underlying_agent_type
from src.models import (
    AgentOutput,
    AgentResult,
    AgentState,
    PipelineContext,
    RepoConfig,
    RepoSourceType,
    TaskContext,
    TaskStatus,
    TaskType,
)
from src.scheduler import AssignAction

logger = logging.getLogger(__name__)


def _render_workspaces_block(attachments: list) -> str:
    """Render the per-task Workspaces block for the agent prompt.

    See workspaces-v2 spec §8.3.  Each attachment is shown with its kind,
    capability flags, and absolute path so the agent can reason about
    where it can read, write, and lock.
    """
    lines = ["## Workspaces"]
    for a in attachments:
        flags: list[str] = []
        if a.writable:
            flags.append("writable")
        else:
            flags.append("read-only")
        if a.lockable and a.workspace.locked_by_task_id is not None:
            flags.append("locked")
        flag_str = ", ".join(flags)
        alias_str = f" ({a.alias})" if a.alias else ""
        lines.append(f"- **{a.kind_id}**{alias_str} ({flag_str}) → {a.workspace_path}")
    return "\n".join(lines)


class ExecutionMixin:
    """Agent execution pipeline methods mixed into Orchestrator."""

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
            # Clean up sentinel before releasing workspace lock (worktree-aware)
            ws = await self.db.get_workspace_for_task(action.task_id)
            if ws:
                self._remove_sentinel(ws.workspace_path)
            await self._release_workspaces_for_task(action.task_id)
            await self.db.transition_task(
                action.task_id, TaskStatus.BLOCKED, context="timeout", assigned_agent_id=None
            )
            await self.db.update_agent(action.agent_id, state=AgentState.IDLE, current_task_id=None)
            self._adapters.pop(action.agent_id, None)
            task = await self.db.get_task(action.task_id)
            if task:
                profile = await self._resolve_profile(task)
                await self._emit_task_failure(
                    task,
                    "timeout",
                    error=f"Task execution timed out after {timeout}s",
                    agent_id=action.agent_id,
                    agent_type=underlying_agent_type(profile.id) if profile else None,
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
                # Clean up sentinel before releasing workspace lock (worktree-aware)
                ws = await self.db.get_workspace_for_task(action.task_id)
                if ws:
                    self._remove_sentinel(ws.workspace_path)
                await self._release_workspaces_for_task(action.task_id)
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

    async def _check_constraints_before_assignment(self, action: AssignAction) -> str | None:
        """Re-check project constraints right before committing an assignment.

        The scheduler runs as a pure function on a point-in-time snapshot, and
        the resulting ``AssignAction`` objects are executed asynchronously as
        background tasks.  Between the scheduler's decision and the actual
        ``assign_task_to_agent()`` call, constraints may have changed (e.g. a
        coordination playbook called ``set_project_constraint`` to pause the
        project or request exclusive access).

        This method queries the *current* constraint state from the database
        and validates the assignment against it.  Returns ``None`` if the
        assignment is allowed, or a human-readable reason string if it should
        be aborted.

        Checked constraints:
        - ``pause_scheduling`` — project is paused, no new assignments allowed.
        - ``exclusive`` — only one agent may work on the project at a time;
          if another agent is already active, this assignment is rejected.
        - ``max_agents_by_type`` — per-agent-type concurrency limits; if the
          agent's type has reached its cap, the assignment is rejected.
        """
        constraint = await self.db.get_project_constraint(action.project_id)
        if not constraint:
            return None  # no constraint → always allowed

        # 1. pause_scheduling — block all new assignments
        if constraint.pause_scheduling:
            return "pause_scheduling is active"

        # 2. exclusive — only one agent on the project at a time
        #    Count agents currently BUSY on tasks belonging to this project.
        if constraint.exclusive:
            agents = await self.db.list_agents(state=AgentState.BUSY)
            active_on_project = 0
            for a in agents:
                if a.current_task_id:
                    t = await self.db.get_task(a.current_task_id)
                    if t and t.project_id == action.project_id:
                        active_on_project += 1
            if active_on_project >= 1:
                return "exclusive constraint active and project already has an active agent"

        # 3. max_agents_by_type — per-type concurrency limits
        if constraint.max_agents_by_type:
            agent = await self.db.get_agent(action.agent_id)
            if agent and agent.profile_id in constraint.max_agents_by_type:
                limit = constraint.max_agents_by_type[agent.profile_id]
                agents = await self.db.list_agents(state=AgentState.BUSY)
                type_count = 0
                for a in agents:
                    if a.profile_id == agent.profile_id and a.current_task_id:
                        t = await self.db.get_task(a.current_task_id)
                        if t and t.project_id == action.project_id:
                            type_count += 1
                if type_count >= limit:
                    return (
                        f"max_agents_by_type limit reached for type "
                        f"'{agent.profile_id}' (limit={limit}, active={type_count})"
                    )

        return None

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
        3. **Agent context assembly** — build a structured markdown prompt.
        4. **Memory recall** — inject semantically relevant historical context.
        5. **Agent launch** — create an adapter and start the agent process.
        6. **Stream + wait** — forward agent output messages to Discord thread.
        7. **Token accounting** — record tokens used, check budget warnings.
        8. **Result handling** — branch on the ``AgentResult`` enum.
        9. **Cleanup** — release workspace lock, free agent, remove adapter.
        """
        from src.orchestrator.core import _parse_reset_time

        if not self._runtimes and not self.config.sessions.enabled:
            logger.error("Cannot execute task %s: no platforms registry configured", action.task_id)
            await self._emit_text_notify(
                f"**Error:** Cannot execute task `{action.task_id}` — no agent adapter configured.",
                project_id=action.project_id,
            )
            return

        # ── Pre-assignment constraint check ─────────────────────────────
        # The scheduler checked constraints with a point-in-time snapshot,
        # but this background task may execute seconds later.  Re-check
        # constraints right before committing the assignment to catch
        # changes that occurred since the scheduler ran (e.g. a playbook
        # set pause_scheduling or exclusive after the scheduling tick).
        violation = await self._check_constraints_before_assignment(action)
        if violation:
            logger.info(
                "Constraint violation for task %s on project %s: %s — returning to READY",
                action.task_id,
                action.project_id,
                violation,
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

        # Resolve the agent profile up front so we know which platform to
        # dispatch through and whether a workspace is needed.  Tool-call-only
        # platforms (e.g. supervisor) skip workspace prep entirely.
        profile = await self._resolve_profile(task)
        if profile:
            # Report the route actually taken.  This used to print
            # ``platform=<profile.runtime>`` unconditionally, which was
            # actively misleading: a session-routed task logged
            # ``platform=claude_sdk`` while launching tmux, and after runtime
            # stripping it prints an empty field for every task. The routing
            # decision is what a reader needs.
            _routed = self._is_session_routed(profile)
            logger.info(
                "Task %s: profile='%s' via=%s tools=%s mcp=%s",
                task.id,
                profile.id,
                (
                    f"session/{getattr(profile, 'harness', '') or '?'}"
                    if _routed
                    else f"runtime/{profile.runtime or 'none'}"
                ),
                profile.allowed_tools or "(default)",
                list(profile.mcp_servers) if profile.mcp_servers else "(none)",
            )
        else:
            logger.info("Task %s: no profile (using system defaults)", task.id)

        # ── Session-runtime routing (session-runtime spec §6.2) ──────────
        # Session path iff ``sessions.enabled`` AND the resolved profile
        # declares a ``harness``.  Per-profile and per-project opt-in falls
        # out of that for free, because profiles are project-scoped.  A
        # profile without ``harness`` keeps its ``runtime:`` verbatim.
        session_routed = self._is_session_routed(profile)

        platform_name = profile.runtime if profile else self.config.default_runtime
        platform = None
        if not session_routed:
            platform = self._runtimes.create(
                platform_name, profile=profile, llm_logger=self.llm_logger
            )
            # Store platform reference so admin commands (stop_task, timeout
            # handler) can call platform.stop() to terminate the agent process.
            self._adapters[action.agent_id] = platform

        project = await self.db.get_project(action.project_id)
        if getattr(platform, "requires_workspace", True):
            # Prepare workspace (repo checkout/worktree/init)
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
                # Some waits are expected and self-clearing.  Telling the
                # operator to "/add-workspace" while the slot pool is simply
                # ramping — one slot per dispatch, so a cold cap-N project
                # needs N-1 rounds — is both wrong and, at one notice per
                # round, noisy.  Same for two plan subtasks queueing on their
                # shared parent branch (worktree-execution §4.4).
                wait_reason = self._workspace_wait_reasons.pop(action.task_id, None)
                if wait_reason == "slot_warming":
                    logger.info(
                        "Task %s paused %ds — worktree slot pool for project %s is "
                        "still warming up (one slot is provisioned per dispatch)",
                        task.id,
                        no_ws_backoff,
                        action.project_id,
                    )
                elif wait_reason == "branch_busy":
                    logger.info(
                        "Task %s paused %ds — a sibling plan subtask holds the "
                        "shared plan branch in another slot",
                        task.id,
                        no_ws_backoff,
                    )
                else:
                    await self._emit_text_notify(
                        f"**No Workspace:** Task `{task.id}` paused for "
                        f"{no_ws_backoff}s — project `{action.project_id}` has no "
                        f"available workspaces. Use `/add-workspace` to create one.",
                        project_id=action.project_id,
                    )
                # Drop the platform we registered above; the task is paused.
                self._adapters.pop(action.agent_id, None)
                return
        else:
            # Tool-call-only platform (e.g. Supervisor): no workspace.
            workspace = None

        # Re-fetch task/agent in case _prepare_workspace updated them
        task = await self.db.get_task(action.task_id)
        agent = await self.db.get_agent(action.agent_id)

        # Fetch the workspace object for display in notifications.  None for
        # workspace-less platforms (no workspace was provisioned).
        ws_obj = (
            await self.db.get_workspace_for_task(task.id)
            if getattr(platform, "requires_workspace", True)
            else None
        )

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
                    workspace_path=(ws_obj.workspace_path if ws_obj else (workspace or "")),
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

        # ── Session-runtime fork: launch and return ──────────────────────
        # Everything below this point is the legacy blocking pipeline:
        # build a prompt, start an adapter, await its stream, branch on the
        # result.  A session-routed task does none of it.  Its full prompt
        # comes from ``aq prime``, its progress arrives as events, and its
        # completion arrives as ``aq task close`` — so this method's job
        # ends the moment the session exists.
        #
        # Note the wrapper above (``_execute_task_safe_inner``) still wraps
        # this coroutine in ``asyncio.wait_for(stuck_timeout_seconds)``.
        # That is harmless rather than overlooked: the coroutine now returns
        # in milliseconds, so the timeout can never fire on it.  The real
        # stuck-timeout backstop moved to ``SessionReconciler.tick()``
        # step 6, where it can act on a session that outlives the daemon.
        if session_routed:
            await self._launch_session_for_task(action, task, profile, workspace)
            return

        # Profile + platform were resolved earlier (before workspace prep) so
        # the workspace decision could honor platform.requires_workspace.
        # Alias kept because later code in this method reads ``adapter``.
        adapter = platform

        # ------------------------------------------------------------------ #
        # Build the agent's system context prompt.
        # ------------------------------------------------------------------ #
        full_description = await self._build_task_context_with_prompt_builder(
            task, workspace, project, profile
        )

        # ------------------------------------------------------------------ #
        # L0 Identity tier and L1 Critical Facts tier.
        # ------------------------------------------------------------------ #
        # Profile chain: the global agent-type profile (e.g. claude-opus)
        # provides the base Role; the project-scoped profile (e.g. Meredith
        # Oxalis) supplies a project specialisation on top.  We expose them
        # as two separate tiers so the prompt builder keeps them ordered:
        # l0_role → project_override_role → L1 facts → ...  Before this
        # chain existed, the scoped profile fully replaced the base, so any
        # role guidance from the agent-type was silently dropped.
        l0_role = ""
        project_override_role = ""
        if profile and profile.system_prompt_suffix:
            scoped_suffix = profile.system_prompt_suffix.strip()
            base_agent_type = underlying_agent_type(profile.id)
            is_scoped = profile.id.startswith("project:")
            if is_scoped and base_agent_type:
                global_profile = await self.db.get_profile(base_agent_type)
                if global_profile and global_profile.system_prompt_suffix:
                    l0_role = global_profile.system_prompt_suffix.strip()
                    project_override_role = scoped_suffix
                else:
                    # No global parent — fall back to scoped suffix as the
                    # only role source.  Keeps single-profile projects working.
                    l0_role = scoped_suffix
            else:
                l0_role = scoped_suffix

        l1_facts = ""
        l1_guidance = ""
        l2_context = ""
        mem_svc = (
            self.plugin_registry.get_service("memory")
            if getattr(self, "plugin_registry", None) is not None
            else None
        )
        if mem_svc:
            try:
                l1_text = await mem_svc.load_l1_facts(
                    project_id=task.project_id,
                    agent_type=underlying_agent_type(profile.id) if profile else None,
                )
                if l1_text:
                    l1_facts = l1_text
            except Exception as e:
                logger.warning("L1 facts injection failed for task %s: %s", task.id, e)

            try:
                l1_guid = await mem_svc.load_l1_guidance(
                    project_id=task.project_id,
                    agent_type=underlying_agent_type(profile.id) if profile else None,
                )
                if l1_guid:
                    l1_guidance = l1_guid
            except Exception as e:
                logger.warning("L1 guidance injection failed for task %s: %s", task.id, e)

            # L2 Topic Context — semantic search using task description.
            if task.project_id and task.description:
                try:
                    l2_text = await mem_svc.load_l2_context(
                        task.description,
                        project_id=task.project_id,
                    )
                    if l2_text:
                        l2_context = l2_text
                except Exception as e:
                    logger.warning("L2 context injection failed for task %s: %s", task.id, e)

        # Resolve MCP servers from the in-memory registry.  Each profile
        # entry is a *name*; we look it up against the project-scoped or
        # system-scoped registry to recover the dict the agent adapter
        # expects.  The daemon's own embedded MCP server is auto-injected
        # if ``inject_into_tasks`` is enabled, separately from profile
        # entries (so a profile that doesn't list ``agent-queue`` still
        # gets it).
        injected_mcp: dict[str, dict] = dict(self.config.mcp_server.task_mcp_entry())
        if not injected_mcp:
            ms = self.config.mcp_server
            logger.info(
                "Task %s: agent-queue MCP auto-injection skipped (enabled=%s inject_into_tasks=%s)",
                task.id,
                ms.enabled,
                ms.inject_into_tasks,
            )

        profile_mcp: dict[str, dict] = {}
        registry = getattr(self, "mcp_registry", None)
        missing_servers: list[str] = []
        if profile and profile.mcp_servers and registry is not None:
            for name in profile.mcp_servers:
                # Auto-injected agent-queue takes precedence over any
                # registry entry of the same name.
                if name in injected_mcp:
                    continue
                resolved = registry.get(name, project_id=task.project_id)
                if resolved is None:
                    missing_servers.append(name)
                    continue
                profile_mcp[name] = resolved.to_adapter_dict()

        if missing_servers:
            logger.error(
                "Task %s: profile='%s' references unknown MCP server(s): %s",
                task.id,
                profile.id if profile else "(none)",
                missing_servers,
            )

        task_mcp: dict[str, dict] = dict(injected_mcp)
        task_mcp.update(profile_mcp)

        # Log the merged MCP server view so operators can see which servers
        # came from auto-injection vs the profile, and what the agent will
        # actually try to connect to.
        def _mcp_summary(servers: dict[str, dict]) -> str:
            parts: list[str] = []
            for sname, sconf in servers.items():
                if isinstance(sconf, dict):
                    if sconf.get("type") == "http":
                        parts.append(f"{sname}=http({sconf.get('url', '?')})")
                    elif sconf.get("type") == "sdk":
                        parts.append(f"{sname}=sdk-instance")
                    elif "command" in sconf:
                        cmd = sconf.get("command", "?")
                        parts.append(f"{sname}=subprocess[{cmd}]")
                    else:
                        parts.append(f"{sname}=?")
                else:
                    parts.append(f"{sname}=<non-dict>")
            return ", ".join(parts) if parts else "(none)"

        logger.info(
            "Task %s MCP servers resolved: injected=[%s] profile=[%s] final=[%s]",
            task.id,
            _mcp_summary(injected_mcp),
            _mcp_summary(profile_mcp),
            _mcp_summary(task_mcp),
        )

        # Validation: warn if the profile's allowed_tools references
        # ``mcp__{server}__*`` patterns for servers that aren't in the final
        # task_mcp dict. This catches hyphen/underscore mismatches and typos
        # that otherwise surface only as "agent says it has no tools".
        if profile and profile.allowed_tools:
            import re

            pat = re.compile(r"^mcp__([^_]+(?:[-_][^_]+)*)__")
            mcp_server_names = set(task_mcp.keys())
            missing: list[tuple[str, str]] = []
            for tool in profile.allowed_tools:
                m = pat.match(tool)
                if not m:
                    continue
                declared_server = m.group(1)
                if declared_server not in mcp_server_names:
                    missing.append((tool, declared_server))
            if missing:
                candidates = ", ".join(sorted(mcp_server_names)) or "(none)"
                details = "; ".join(
                    f"{tool} → server '{srv}' not in task_mcp" for tool, srv in missing[:4]
                )
                logger.warning(
                    "Task %s profile='%s' allowed_tools references %d MCP server(s) not "
                    "in task_mcp: %s. Available servers: %s",
                    task.id,
                    profile.id,
                    len(missing),
                    details,
                    candidates,
                )

        # Give the agent read/write access to its own memory in the vault.
        # The workspace (cwd) is the project's git repo; the vault lives
        # under ~/.agent-queue/vault/projects/<id>/ which is outside cwd, so
        # without this the Claude CLI's filesystem sandbox rejects Edit/Write
        # calls on insight files — breaking any task that edits memory
        # directly (consolidation, note-taking, self-improvement).
        extra_dirs: list[str] = []
        if task.project_id:
            vault_project_dir = os.path.join(self.config.vault_root, "projects", task.project_id)
            extra_dirs.append(vault_project_dir)

        # Workspaces-v2: surface the per-task attachment set captured at
        # acquisition time.  When present, it includes every workspace the
        # task locked or auto-attached (including the project vault as a
        # first-class attachment).  Empty for back-compat call sites that
        # haven't been wired yet (e.g. Supervisor singleton).
        attachment_set = getattr(self, "_task_attachments", {}).get(task.id)
        workspace_attachments = (
            list(attachment_set.attachments) if attachment_set is not None else []
        )

        # Append a Workspaces block to the description so the agent knows
        # what each attached path represents.  Spec §8.3.
        if workspace_attachments:
            full_description = (
                full_description.rstrip() + "\n\n" + _render_workspaces_block(workspace_attachments)
            )

        # Build the runtime's allowed-paths set: extra_dirs (vault back-compat)
        # ∪ attachment paths, minus the cwd.  Spec §7.1: dedup is explicit.
        cwd_path = workspace
        attachment_extra = {
            a.workspace_path
            for a in workspace_attachments
            if a.workspace_path and a.workspace_path != cwd_path
        }
        deduped_extra_dirs = list(
            dict.fromkeys(  # preserve order, dedup
                [d for d in extra_dirs if d != cwd_path]
                + sorted(attachment_extra - set(extra_dirs))
            )
        )

        ctx = TaskContext(
            task_id=task.id,
            description=full_description,
            l0_role=l0_role,
            project_override_role=project_override_role,
            l1_facts=l1_facts,
            l1_guidance=l1_guidance,
            l2_context=l2_context,
            checkout_path=workspace,
            branch_name=task.branch_name or "",
            image_paths=task.attachments if task.attachments else [],
            mcp_servers=task_mcp,
            add_dirs=deduped_extra_dirs,
            workspace_attachments=workspace_attachments,
            # Singleton platforms (e.g. Supervisor) read profile here at
            # ``start(task)`` time since they can't carry it in __init__.
            profile=profile,
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

        # Record execution start time so _discover_and_store_plan() can
        # detect stale plan files that predate this task's agent execution.
        self._task_exec_start[action.task_id] = time.time()

        # Snapshot the current HEAD so the task summary can show only the
        # commits the agent made (git log pre_sha..HEAD).
        if workspace and await self.git.avalidate_checkout(workspace):
            try:
                pre_sha = (
                    await self.git._arun(
                        ["rev-parse", "HEAD"],
                        cwd=workspace,
                    )
                ).strip()
                if pre_sha:
                    self._task_pre_exec_sha[action.task_id] = pre_sha
            except Exception:
                pass

        await adapter.start(ctx)

        # ------------------------------------------------------------------ #
        # Agent message streaming and question detection.
        # ------------------------------------------------------------------ #
        _question_notified = False

        async def forward_agent_message(
            text: str,
            *,
            stream_id: str | None = None,
            stream_done: bool = False,
        ) -> None:
            nonlocal _question_notified
            # Stream agent output via event — the notification handler
            # routes to the task's thread if one exists, otherwise to the
            # main channel.  ``stream_id``/``stream_done`` are passthrough
            # kwargs from streaming sources — when set, the
            # Discord receiver edits a single message in place instead of
            # posting a new one per call.
            await self._emit_notify(
                "notify.task_message",
                TaskMessageEvent(
                    task_id=task.id,
                    message=text,
                    message_type="agent_output",
                    project_id=action.project_id,
                    stream_id=stream_id,
                    stream_done=stream_done,
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
        # ------------------------------------------------------------------ #
        _rl_base = self.config.pause_retry.rate_limit_backoff_seconds
        _rl_max_backoff = self.config.pause_retry.rate_limit_max_backoff_seconds
        _rl_max_retries = self.config.pause_retry.rate_limit_max_retries
        _rl_attempt = 0

        while True:
            output = await adapter.wait(on_message=forward_agent_message)

            if output.result != AgentResult.PAUSED_RATE_LIMIT:
                break  # Completed, failed, or token-exhausted — leave the loop.

            # Session limits (with a known reset time) should NOT be retried
            # in-process — go straight to the PAUSED handler which sets the
            # provider cooldown and waits until the actual reset time.
            _err = output.error_message or ""
            if "hit your limit" in _err.lower():
                logger.info(
                    "Task %s: session limit detected, skipping in-process retries.",
                    task.id,
                )
                break

            _rl_attempt += 1
            if _rl_attempt > _rl_max_retries:
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
        # ------------------------------------------------------------------ #

        # Track the final root text for updating the thread root message
        _final_root_content: str | None = None

        # Log the agent's final output so "what did the agent actually say"
        # is visible in daemon.log for every task, not buried in Discord.
        try:
            _final_text = getattr(output, "final_text", None) or getattr(output, "text", None)
            _err_text = getattr(output, "error_message", None)
            _preview = _final_text or _err_text or "(no text)"
            if isinstance(_preview, str) and len(_preview) > 600:
                _preview = _preview[:600] + "…"
            logger.info(
                "Task %s agent output: result=%s preview=%s",
                task.id,
                getattr(output.result, "name", str(output.result)),
                _preview,
            )
        except Exception:
            pass

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

            pipeline_ctx = PipelineContext(
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
            pr_url, completed_ok = await self._run_completion_pipeline(pipeline_ctx)

            if (
                pipeline_ctx.plan_needs_approval
                and completed_ok
                and self._should_run_legacy_plan_region(task)
            ):
                # Gated by ``config.planner.legacy_plan_discovery``
                # (supervisor-agent §9 row 4). ``_phase_plan_discover``
                # only sets ``plan_needs_approval`` when the legacy
                # discovery path ran, so this extra guard is defensive:
                # if the flag was flipped between discover and here, the
                # legacy region (AWAITING_PLAN_APPROVAL transition +
                # ``break_plan_into_tasks``) is skipped. Drain semantics
                # (spec §11 P5) let already-AWAITING_PLAN_APPROVAL tasks
                # continue on legacy — see
                # ``_should_run_legacy_plan_region``. When the region is
                # skipped, no planner task-graph is auto-created here:
                # spec §9 doesn't define a concrete replacement and the
                # Task 8 brief mandates "skip + log at info" over
                # invented behaviour.
                logger.info(
                    "Task %s: plan needs approval — sending notification",
                    task.id,
                )
                # Plan was discovered — present it to the user for approval
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
                # path (plan_draft_subtasks context exists).
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
                                # break_plan_into_tasks() already gave every
                                # subtask a `parent-child` edge to this task
                                # (+ `discovered-from` provenance).  An
                                # AWAITING_PLAN_APPROVAL container withholds
                                # its children, so the chain stays blocked
                                # until approval without a separate `blocks`
                                # edge (work-graph design §3.1).

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
                        pipeline_ctx.plan_needs_approval = False

                if pipeline_ctx.plan_needs_approval:
                    # Populate parsed_steps from auto-created subtasks (if any).
                    parsed_steps: list[dict] = [
                        {"title": t["title"], "description": ""} for t in created_info
                    ]

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
                try:
                    await self.db.transition_task(
                        action.task_id, TaskStatus.COMPLETED, context="completed_no_approval"
                    )
                except HierarchyError as exc:
                    # Invariant 6 (spec §7): open children hold the container
                    # open.  Leave the task as it was rather than crash the
                    # cascade; it settles when the children finish.
                    logger.warning(
                        "completion refused for %s: %s", action.task_id, exc
                    )
                    return
                await self.db.log_event(
                    "task_completed",
                    project_id=action.project_id,
                    task_id=action.task_id,
                    agent_id=action.agent_id,
                )
                # Notifications after state transition are best-effort.
                # A Discord error (e.g. session closed during restart) must
                # NOT propagate — the outer except would revert the task to
                # READY, undoing the COMPLETED transition.
                try:
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
                except Exception:
                    logger.warning(
                        "Task %s: completion notification failed (state is COMPLETED)",
                        task.id,
                        exc_info=True,
                    )
                await self.bus.emit(
                    "task.completed",
                    {
                        "task_id": task.id,
                        "project_id": task.project_id,
                        "title": task.title,
                        "agent_id": action.agent_id,
                        "agent_type": profile.id if profile else None,
                    },
                )

                # Write task summary to vault
                try:
                    result_dict = {
                        "summary": output.summary or "",
                        "files_changed": output.files_changed or [],
                        "tokens_used": output.tokens_used or 0,
                        "error_message": output.error_message,
                    }
                    # Collect commits the agent made during this task.
                    # Uses the pre-execution HEAD snapshot so we only get
                    # commits introduced by the agent, not history.
                    commits: list[tuple[str, str]] | None = None
                    ws_path = pipeline_ctx.workspace_path
                    pre_sha = self._task_pre_exec_sha.pop(action.task_id, None)
                    if ws_path and pre_sha and await self.git.avalidate_checkout(ws_path):
                        try:
                            log_output = await self.git._arun(
                                ["log", "--format=%H|%s", f"{pre_sha}..HEAD"],
                                cwd=ws_path,
                            )
                            if log_output.strip():
                                commits = []
                                for line in log_output.strip().splitlines():
                                    if "|" in line:
                                        sha, subject = line.split("|", 1)
                                        commits.append((sha.strip(), subject.strip()))
                        except Exception:
                            pass  # git log failure is non-critical
                    write_task_summary(
                        self.config.vault_root,
                        task,
                        result_dict,
                        commits=commits,
                    )
                except Exception as e:
                    logger.warning("Failed to write task summary for %s: %s", task.id, e)

                # Check if this completion finishes a workflow stage
                await self._check_workflow_stage_completion(task)

                # Auto-reload plugin if the task modified a plugin workspace
                await self._check_plugin_workspace_update(task, ws)
                # Mark for thread root update
                _final_root_content = f"✅ **Work completed:** {task.title}"
            elif pipeline_ctx.verification_reopened:
                # Task was already reopened to READY by _phase_verify —
                # don't transition to BLOCKED.
                brief = f"🔄 Task reopened for git verification: {task.title} (`{task.id}`)"
                await _post(brief)
                await _notify_brief(brief)
                # Clean up workspace so the next attempt starts with a
                # clean working tree.
                await self._cleanup_workspace_for_next_task(
                    pipeline_ctx.workspace_path,
                    pipeline_ctx.default_branch,
                    task.id,
                    project_id=task.project_id,
                    agent_id=pipeline_ctx.agent.id,
                )
            else:
                # Pipeline stopped and could not reopen — last-ditch attempt
                # to clean the workspace before blocking.
                if pipeline_ctx.workspace_path:
                    try:
                        has_dirty = await self.git.ahas_uncommitted_changes(
                            pipeline_ctx.workspace_path
                        )
                        if has_dirty:
                            cur = await self.git.aget_current_branch(pipeline_ctx.workspace_path)
                            still_dirty = await self._auto_remediate_uncommitted(
                                pipeline_ctx.workspace_path,
                                task.id,
                                cur,
                                project_id=task.project_id,
                                agent_id=pipeline_ctx.agent.id,
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
                    agent_id=action.agent_id,
                    agent_type=underlying_agent_type(profile.id) if profile else None,
                )
                await _post(
                    f"**Verification failed** for `{task.id}` — "
                    f"max retries exhausted, manual resolution needed."
                )
                # Clean up workspace so it's ready for the next task
                await self._cleanup_workspace_for_next_task(
                    pipeline_ctx.workspace_path,
                    pipeline_ctx.default_branch,
                    task.id,
                    project_id=task.project_id,
                    agent_id=pipeline_ctx.agent.id,
                )

            # Ensure workspace is clean for the next task.
            if (
                not pipeline_ctx.verification_reopened
                and completed_ok
                and pipeline_ctx.workspace_path
            ):
                await self._cleanup_workspace_for_next_task(
                    pipeline_ctx.workspace_path,
                    pipeline_ctx.default_branch,
                    task.id,
                    project_id=task.project_id,
                    agent_id=pipeline_ctx.agent.id,
                )

            # Re-check DEFINED tasks so newly created subtasks get promoted
            await self._check_defined_tasks()

        elif output.result == AgentResult.FAILED:
            # WG-5: honour the ``failure_class`` outcome-metadata key.  A
            # ``"hard"`` failure skips retry entirely and goes straight to
            # BLOCKED — retries can't heal a structural problem.
            try:
                failure_class = await self.db.get_task_meta(task.id, "failure_class")
            except Exception:
                failure_class = None
            if failure_class == "hard":
                await self.db.transition_task(
                    action.task_id,
                    TaskStatus.BLOCKED,
                    context="hard_failure",
                    retry_count=task.retry_count,
                )
                await self._emit_task_failure(
                    task,
                    "hard_failure",
                    error=output.error_message or "hard failure_class — no retry",
                    agent_id=action.agent_id,
                    agent_type=underlying_agent_type(profile.id) if profile else None,
                )
                await _notify_brief(
                    f"🚫 Hard failure: {task.title} (`{task.id}`) — retry skipped"
                )
                # No workspace reset — leave the state for post-mortem.
                await self._check_defined_tasks()
                return
            new_retry = task.retry_count + 1
            if new_retry >= task.max_retries:
                await self.db.transition_task(
                    action.task_id, TaskStatus.BLOCKED, context="max_retries", retry_count=new_retry
                )
                await self._emit_task_failure(
                    task,
                    "max_retries",
                    error=f"Max retries ({task.max_retries}) exhausted",
                    agent_id=action.agent_id,
                    agent_type=underlying_agent_type(profile.id) if profile else None,
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
            # Emit typed failure/blocked event
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
            await _notify_brief(brief)

            # Mark for thread root update
            if new_retry >= task.max_retries:
                _final_root_content = f"🚫 **Work blocked:** {task.title}"
            else:
                _final_root_content = f"⚠️ **Work failed (retrying):** {task.title}"

            # Check if this blocked task breaks a dependency chain
            if new_retry >= task.max_retries:
                await self._notify_stuck_chain(task)

            # Clean up workspace git state so it's ready for the next task.
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
            # PAUSED path
            retry_secs = (
                self.config.pause_retry.rate_limit_backoff_seconds
                if output.result == AgentResult.PAUSED_RATE_LIMIT
                else self.config.pause_retry.token_exhaustion_retry_seconds
            )
            reason = (
                "rate limit"
                if output.result == AgentResult.PAUSED_RATE_LIMIT
                else "token exhaustion"
            )

            # Session limits include a reset time
            error_msg = output.error_message or ""
            parsed_resume = _parse_reset_time(error_msg)
            if parsed_resume and parsed_resume > time.time():
                retry_secs = int(parsed_resume - time.time()) + 60  # +60s buffer
                reason = "session limit"
                logger.info(
                    "Task %s: session limit resets in %ds, will resume then.",
                    task.id,
                    retry_secs,
                )

            resume_at = time.time() + retry_secs

            # Set provider-level cooldown
            if agent and agent.profile_id:
                self._provider_cooldowns[agent.profile_id] = resume_at
                logger.info(
                    "Provider cooldown set: %s until %.0f (%ds from now)",
                    agent.profile_id,
                    resume_at,
                    retry_secs,
                )

            await self.db.transition_task(
                action.task_id,
                TaskStatus.PAUSED,
                context="tokens_exhausted",
                resume_after=resume_at,
            )
            await self._emit_task_event(
                "task.paused",
                task,
                reason=reason,
                resume_after=resume_at,
            )
            friendly_wait = (
                f"{retry_secs // 3600}h {(retry_secs % 3600) // 60}m"
                if retry_secs >= 3600
                else f"{retry_secs // 60}m"
            )
            await _post(
                f"**Task Paused:** `{task.id}` — {task.title}\n"
                f"Reason: {reason}. Will resume in {friendly_wait}."
            )

            # Clean up workspace
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
            # Agent is blocked on a question
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
        # ------------------------------------------------------------------ #

        # Close the task thread
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
        if workspace:
            self._remove_sentinel(workspace)

        # Release the workspace lock
        await self._release_workspaces_for_task(action.task_id)

        # Free the agent for new work
        post_agent = await self.db.get_agent(action.agent_id)
        next_state = (
            AgentState.PAUSED
            if post_agent and post_agent.state == AgentState.PAUSED
            else AgentState.IDLE
        )
        await self.db.update_agent(action.agent_id, state=next_state, current_task_id=None)

        # Remove adapter reference
        self._adapters.pop(action.agent_id, None)
        self._task_exec_start.pop(action.task_id, None)
        self._task_pre_exec_sha.pop(action.task_id, None)

        # Delete the task-added notification
        added_msg = self._task_added_messages.pop(action.task_id, None)
        if added_msg is not None:
            try:
                await added_msg.delete()
            except Exception as e:
                logger.debug("Could not delete task-added message for %s: %s", action.task_id, e)

        # Delete the Task Started message
        started_msg = self._task_started_messages.pop(action.task_id, None)
        if started_msg is not None:
            try:
                await started_msg.delete()
            except Exception as e:
                logger.debug("Could not delete task-started message for %s: %s", action.task_id, e)

    # ======================================================================
    # Session runtime -- launch and return (session-runtime spec §4, §6)
    # ======================================================================

    def _is_session_routed(self, profile) -> bool:
        """True when this profile's tasks run as sessions.

        Two conditions, both required: the daemon-wide ``sessions.enabled``
        flag, and a ``harness`` on the resolved profile.  Rollback is
        therefore either flipping the flag or removing ``harness:`` from one
        profile -- no code change, and live sessions drain naturally.

        A ``lifecycle: pool`` profile is never push-routed (swarm-work-model
        §11): its work is claimed by long-lived pool sessions, not launched
        per task.  ``lifecycle`` lives on the profile row itself, so this
        check needs no project context even for a project-scoped override.
        """
        if not self.config.sessions.enabled:
            return False
        if getattr(profile, "lifecycle", "task") == "pool":
            return False
        return bool(getattr(profile, "harness", "") or "")

    async def _validated_resume_key(
        self, harness_name: str, work_dir: str, task_id: str, resume_key: str
    ) -> str | None:
        """Return *resume_key* if the harness can actually resume it, else None.

        Resuming a session the CLI has no record of is fatal: ``claude
        --resume <unknown>`` exits 1 immediately, the launch is reported as
        "process died while waiting for the ready prompt", and the task pauses
        for 60s. The failed launch then records a *new* session id that also
        never got a transcript, so the next attempt has a fresh dead key to
        resume — a loop that survives daemon restarts because the key lives in
        task metadata.

        Dropping the key starts a fresh session instead, which loses the prior
        conversation but runs. That is strictly better than not running.

        Harnesses without a transcript reader (codex, gemini) are left alone:
        no reader means no way to check, and refusing to resume on that basis
        would break resume for them entirely.
        """
        try:
            from src.sessions.transcripts import resolve_reader

            reader = resolve_reader(harness_name)
        except Exception:
            return resume_key
        if reader is None:
            return resume_key

        try:
            path = reader.resolve_path(work_dir, resume_key)
        except Exception:
            return resume_key

        # resolve_path falls back to the newest transcript when the key's own
        # file is absent, so an exact match is the only proof it exists.
        if path is not None and path.stem == resume_key:
            return resume_key

        logger.warning(
            "Task %s: resume key %s has no transcript under %s — starting a "
            "fresh session instead of resuming a session the CLI cannot find",
            task_id,
            resume_key,
            work_dir,
        )
        try:
            await self.db.set_task_meta(task_id, "session_resume_key", "")
        except Exception:
            logger.debug("Task %s: could not clear stale resume key", task_id)
        return None

    async def _launch_session_for_task(
        self, action: AssignAction, task, profile, workspace: str | None
    ) -> None:
        """Start a session for *task* and return.  No wait, no result branch.

        Steps 6-9 of the legacy pipeline (stream, token accounting, result
        branch, cleanup) do not happen here.  They moved to
        ``_cmd_task_close`` (the agent declaring completion) and to
        ``SessionReconciler`` (everything the agent failed to declare).

        A launch failure is *not* a task failure with a fabricated result:
        the task is paused with a backoff so the normal scheduler retries,
        because "the CLI could not start" is almost always an install or
        config problem that a retry after 60 s either fixes or repeats
        visibly.
        """
        import uuid as _uuid

        from src.models import SessionRecord
        from src.sessions.provider import SessionDiedDuringStartup, SessionHandle

        harness_name = getattr(profile, "harness", "") or ""
        harness = self.harness_registry.get(harness_name, task.project_id)
        if harness is None:
            await self._fail_session_launch(
                action,
                task,
                f"profile '{getattr(profile, 'id', '?')}' declares harness "
                f"'{harness_name}' but no such harness file exists in the vault "
                f"(vault/harnesses/{harness_name}.md)",
            )
            return

        provider_name = self.config.sessions.provider
        try:
            provider = self.session_providers.create(provider_name, self.config)
        except ValueError as exc:
            await self._fail_session_launch(action, task, str(exc))
            return

        work_dir = workspace or ""
        if not work_dir:
            await self._fail_session_launch(
                action, task, "session launch needs a work_dir but none was prepared"
            )
            return

        # Canonical dashed form: this id rides the harness's session-id flag
        # (``claude --session-id``) and Claude Code rejects a dashless hex
        # string with "Invalid session ID. Must be a valid UUID."
        session_id = str(_uuid.uuid4())
        instance_token = _uuid.uuid4().hex
        # A per-session bearer token.  aq-surface Phase S2 wired real
        # session-scoped mint via :class:`SessionTokenStore`; the sha256
        # hash is persisted and the plaintext is handed to the harness as
        # ``AQ_API_TOKEN``.  The legacy uuid4 fallback survives for unit
        # tests that construct execution paths without a store.
        token_store = getattr(self, "token_store", None)
        if token_store is not None:
            api_token = await token_store.mint(
                session_id=session_id,
                task_id=task.id,
                project_id=task.project_id,
            )
        else:
            api_token = _uuid.uuid4().hex
        resume_key = None
        try:
            resume_key = await self.db.get_task_meta(task.id, "session_resume_key")
        except Exception:
            pass
        if resume_key:
            resume_key = await self._validated_resume_key(
                harness_name, work_dir, task.id, str(resume_key)
            )

        # The workspace's source_type decides whether this launch qualifies
        # for the harness's skip-permissions flag (trust-and-ops §4).  An
        # unreadable workspace row is the *restrictive* default -- the flag
        # is withheld, not granted on a guess.
        source_type = None
        try:
            ws_row = await self.db.get_workspace_for_task(task.id)
            source_type = ws_row.source_type if ws_row else None
        except Exception:
            logger.debug("Task %s: could not read workspace source_type", task.id)

        # A push launch joins the same claim fence a pool session's pull
        # claim does (swarm-work-model §10): bump the task's epoch, write
        # the claim file the agent's ``.aq`` tooling reads, and hand the
        # epoch to the harness as ``AQ_CLAIM_EPOCH`` so its writes are
        # fenced identically.
        from src.commands.claim_commands import write_claim_file

        claim_epoch = await self.db.bump_claim_epoch(task.id)
        write_claim_file(
            work_dir,
            {
                "task_id": task.id,
                "claim_epoch": claim_epoch,
                "session_id": session_id,
                "claimed_at": time.time(),
            },
        )

        spec = self.session_spec_builder.build_task_spec(
            task=task,
            profile=profile,
            harness=harness,
            work_dir=work_dir,
            session_id=session_id,
            instance_token=instance_token,
            epoch=self.daemon_epoch,
            api_token=api_token,
            resume_key=resume_key,
            workspace_source_type=source_type,
            extra_env={"AQ_CLAIM_EPOCH": str(claim_epoch)},
        )

        try:
            await provider.start(spec)
        except SessionDiedDuringStartup as exc:
            await self._fail_session_launch(
                action,
                task,
                f"session died during startup: {exc}",
                stderr_path=exc.start_stderr_path,
            )
            return
        except Exception as exc:
            await self._fail_session_launch(action, task, f"session launch failed: {exc}")
            return

        # The row is written *after* the session exists, so a crash between
        # the two leaves an orphan session rather than a phantom row -- and
        # adoption reconciles an orphan session by its env markers, which is
        # the recoverable direction.
        now = time.time()
        try:
            await self.db.create_session(
                SessionRecord(
                    id=session_id,
                    task_id=task.id,
                    project_id=task.project_id,
                    profile_id=getattr(profile, "id", "") or "",
                    harness=harness.id,
                    provider=provider.name,
                    name=spec.session_name,
                    lifecycle="task",
                    state="running",
                    # ``--session-id`` already pinned the harness's own
                    # conversation id to ours, so the resume key *is* the
                    # session id.  Persisting it here is what makes
                    # restart-with-resume real rather than a checklist tick:
                    # the reconciler copies it into ``session_resume_key``
                    # when it kills, and the next launch passes ``--resume``.
                    session_key=session_id,
                    work_dir=work_dir,
                    epoch=self.daemon_epoch,
                    instance_token=instance_token,
                    started_at=now,
                    last_activity=now,
                )
            )
        except Exception as exc:
            # The process is already running and now has no row, so nothing
            # would ever reconcile it.  Kill what we just started before
            # handing the task back to the scheduler -- otherwise the generic
            # handler upstairs sets the task READY and releases the
            # workspace while a live agent is still writing to it.
            logger.error("Task %s: session row insert failed", task.id, exc_info=True)
            try:
                await provider.stop(
                    SessionHandle(
                        name=spec.session_name,
                        provider=provider.name,
                        instance_token=instance_token,
                    ),
                    grace=2.0,
                )
            except Exception:
                logger.error(
                    "Task %s: could not stop the orphan session %s",
                    task.id,
                    spec.session_name,
                    exc_info=True,
                )
            await self._fail_session_launch(
                action, task, f"session started but its row could not be written: {exc}"
            )
            return
        logger.info(
            "Task %s: session %s started (%s/%s) in %s",
            task.id,
            spec.session_name,
            provider.name,
            harness.id,
            work_dir,
        )
        await self.bus.emit(
            "session.started",
            {
                "session_id": session_id,
                "name": spec.session_name,
                "task_id": task.id,
                "project_id": task.project_id,
                "provider": provider.name,
                "harness": harness.id,
                "work_dir": work_dir,
            },
        )

    async def _fail_session_launch(
        self, action: AssignAction, task, reason: str, stderr_path: str | None = None
    ) -> None:
        """Pause the task with a backoff after a failed session launch."""
        backoff = 60
        logger.error("Task %s: session launch failed -- %s", task.id, reason)
        await self.db.transition_task(
            action.task_id,
            TaskStatus.PAUSED,
            context="session_launch_failed",
            resume_after=time.time() + backoff,
            assigned_agent_id=None,
        )
        await self.db.update_agent(action.agent_id, state=AgentState.IDLE, current_task_id=None)
        await self._release_workspaces_for_task(action.task_id)
        detail = f"\nStartup output: `{stderr_path}`" if stderr_path else ""
        await self._emit_text_notify(
            f"**Session launch failed:** task `{task.id}` -- {reason}. "
            f"Retrying in {backoff}s.{detail}",
            project_id=action.project_id,
        )

    async def complete_session_task(
        self,
        task,
        *,
        outcome: str,
        work_outcome: str = "",
        failure_class: str = "",
        commit: str = "",
        notes: str = "",
        expect_claim_epoch: int | None = None,
        pool: bool = False,
    ) -> dict:
        """Run the completion pipeline for a session-closed task.

        Called from ``_cmd_task_close``.  Deliberately shares
        ``_run_completion_pipeline`` with the legacy path so the only thing
        that differs between the two runtimes is launch/observe/close --
        commit, push, PR and verify behave identically, which is what makes
        the dual-run comparison meaningful.

        ``expect_claim_epoch`` fences the terminal ``transition_task`` call
        (swarm-work-model §10) — a stale epoch raises ``StaleClaim``, which
        the caller (``_cmd_task_close``) turns into ``{"result":
        "stale_claim"}``.  ``pool=True`` means the calling session is a pool
        session: it keeps its workspace agent-lock and instance token, so
        ``release_session_task_resources`` (a full release) is skipped —
        ``_cmd_task_close`` releases the claim itself via ``db.release_claim``.
        """
        agent = (
            await self.db.get_agent(task.assigned_agent_id)
            if task.assigned_agent_id
            else None
        )
        ws = await self.db.get_workspace_for_task(task.id)
        project = await self.db.get_project(task.project_id)
        workspace_path = ws.workspace_path if ws else None
        default_branch = await self._get_default_branch(project, workspace_path)

        repo = (
            RepoConfig(
                id=f"project-{task.project_id}",
                project_id=task.project_id,
                source_type=ws.source_type,
                url=project.repo_url if project else "",
                default_branch=default_branch,
            )
            if ws
            else None
        )

        # ``PipelineContext.output`` is synthesized from the close arguments:
        # the agent told us what happened, so there is no stream to summarize.
        output = AgentOutput(
            result=AgentResult.COMPLETED if outcome == "pass" else AgentResult.FAILED,
            summary=notes or "",
            error_message=None if outcome == "pass" else (notes or "closed as failed"),
        )
        ctx = PipelineContext(
            task=task,
            agent=agent,
            output=output,
            workspace_path=workspace_path,
            workspace_id=ws.id if ws else None,
            repo=repo,
            default_branch=default_branch,
            project=project,
        )

        pr_url = None
        completed_ok = True
        if outcome == "pass":
            try:
                pr_url, completed_ok = await self._run_completion_pipeline(ctx)
            except Exception:
                logger.error(
                    "Task %s: completion pipeline raised during task close",
                    task.id,
                    exc_info=True,
                )
                completed_ok = False

        # ``retry_count`` is only carried on the transient leg; the other
        # branches are terminal and must not bump the counter.
        new_retry: int | None = None
        if outcome == "pass" and completed_ok:
            new_status = TaskStatus.COMPLETED
            context = "session_close"
        elif outcome == "pass":
            # Pipeline said stop (verification reopened, uncommitted work,
            # ...).  BLOCKED, not COMPLETED: the agent's word is the trigger
            # for the pipeline, not a substitute for it.
            new_status = TaskStatus.BLOCKED
            context = "session_close_pipeline_stop"
        elif failure_class == "hard":
            new_status = TaskStatus.BLOCKED
            context = "session_close_hard_failure"
        else:
            # ``transient`` -- or absent, the legacy default.  work-graph
            # §"outcome metadata" routes this to the existing
            # retry-with-backoff path, so it has to behave exactly like the
            # legacy failure branch: bump ``retry_count`` and re-queue until
            # ``max_retries`` is spent, then BLOCKED.  Sending it straight to
            # FAILED made a session-run flake terminal where a legacy one
            # would have been retried.
            new_retry = (task.retry_count or 0) + 1
            if new_retry >= (task.max_retries or 0):
                new_status = TaskStatus.BLOCKED
                context = "max_retries"
            else:
                new_status = TaskStatus.READY
                context = "retry"

        try:
            if new_retry is not None:
                await self.db.transition_task(
                    task.id,
                    new_status,
                    context=context,
                    retry_count=new_retry,
                    assigned_agent_id=None,
                    expect_claim_epoch=expect_claim_epoch,
                )
            else:
                await self.db.transition_task(
                    task.id,
                    new_status,
                    context=context,
                    assigned_agent_id=None,
                    expect_claim_epoch=expect_claim_epoch,
                )
        except HierarchyError as exc:
            # Invariant 6 (spec §7): the task has open children, so it stays
            # where it was.  The rest of the close (event, resource release)
            # still runs — it reports the status the task actually has.
            logger.warning("session-close transition refused for %s: %s", task.id, exc)
            refreshed = await self.db.get_task(task.id)
            if refreshed:
                new_status = refreshed.status
        await self._emit_task_event(
            "task.closed",
            task,
            outcome=outcome,
            work_outcome=work_outcome,
            status=new_status.value,
            pr_url=pr_url or "",
        )

        # Release the workspace and free the agent -- the session is on its
        # way out, and the next task should not wait for the drain-ack.
        # Pool sessions skip this: they keep their agent-lock and token, and
        # ``_cmd_task_close`` releases the claim itself via ``db.release_claim``.
        if not pool:
            await self.release_session_task_resources(
                task.id, agent_id=task.assigned_agent_id, workspace_path=workspace_path
            )

        return {
            "status": new_status.value,
            "pr_url": pr_url,
            "pipeline_ok": completed_ok,
            "retry_count": new_retry,
        }

    async def release_session_task_resources(
        self,
        task_id: str,
        *,
        agent_id: str | None = None,
        workspace_path: str | None = None,
    ) -> None:
        """Free everything a session-run task was holding.  Idempotent.

        The cleanup tail every terminal path owes: sentinel removed,
        workspace lock released, agent back to IDLE, per-task bookkeeping
        dropped.  ``complete_session_task`` is the happy path; the
        :class:`~src.sessions.reconciler.SessionReconciler` calls this on
        every *non*-happy one (rate-limit, rapid crash, productive death,
        quarantine, backstop, task-closed).

        Before this existed each reconciler verdict transitioned the task
        and stopped -- leaving the agent BUSY and the workspace locked
        forever, because ``AgentReconciler``'s orphan sweep only frees an
        agent whose task *row* is gone, and a PAUSED/BLOCKED task still
        has one.  N crash-looping tasks burned N agents and N workspaces
        until the daemon restarted.

        Safe to call twice: ``_release_workspaces_for_task`` is a no-op on
        an already-released task and ``update_agent`` is a plain write.
        """
        if workspace_path is None:
            try:
                ws = await self.db.get_workspace_for_task(task_id)
                workspace_path = ws.workspace_path if ws else None
            except Exception:
                workspace_path = None
        if workspace_path:
            try:
                self._remove_sentinel(workspace_path)
            except Exception:
                pass
        try:
            await self._release_workspaces_for_task(task_id)
        except Exception:
            logger.error("Task %s: workspace release failed", task_id, exc_info=True)
        if agent_id:
            try:
                await self.db.update_agent(
                    agent_id, state=AgentState.IDLE, current_task_id=None
                )
            except Exception:
                logger.error("Task %s: could not idle agent %s", task_id, agent_id, exc_info=True)
            self._adapters.pop(agent_id, None)
        self._task_exec_start.pop(task_id, None)
        self._task_pre_exec_sha.pop(task_id, None)
