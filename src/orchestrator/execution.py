"""Execution mixin — the agent execution pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

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
from src.models import (
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
                    agent_type=profile.id if profile else None,
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
            if agent and agent.agent_type in constraint.max_agents_by_type:
                limit = constraint.max_agents_by_type[agent.agent_type]
                agents = await self.db.list_agents(state=AgentState.BUSY)
                type_count = 0
                for a in agents:
                    if a.agent_type == agent.agent_type and a.current_task_id:
                        t = await self.db.get_task(a.current_task_id)
                        if t and t.project_id == action.project_id:
                            type_count += 1
                if type_count >= limit:
                    return (
                        f"max_agents_by_type limit reached for type "
                        f"'{agent.agent_type}' (limit={limit}, active={type_count})"
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

        if not self._adapter_factory:
            logger.error("Cannot execute task %s: no adapter factory configured", action.task_id)
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
        # ------------------------------------------------------------------ #
        full_description = await self._build_task_context_with_prompt_builder(
            task, workspace, project, profile
        )

        # ------------------------------------------------------------------ #
        # L0 Identity tier and L1 Critical Facts tier.
        # ------------------------------------------------------------------ #
        l0_role = ""
        if profile and profile.system_prompt_suffix:
            l0_role = profile.system_prompt_suffix.strip()

        l1_facts = ""
        l1_guidance = ""
        l2_context = ""
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

            try:
                l1_guid = await self._memory_v2_service.load_l1_guidance(
                    project_id=task.project_id,
                    agent_type=profile.id if profile else None,
                )
                if l1_guid:
                    l1_guidance = l1_guid
            except Exception as e:
                logger.warning("L1 guidance injection failed for task %s: %s", task.id, e)

            # L2 Topic Context — semantic search using task description.
            if task.project_id and task.description:
                try:
                    l2_text = await self._memory_v2_service.load_l2_context(
                        task.description,
                        project_id=task.project_id,
                    )
                    if l2_text:
                        l2_context = l2_text
                except Exception as e:
                    logger.warning("L2 context injection failed for task %s: %s", task.id, e)

        # Merge MCP servers: start with the daemon's own MCP server (if
        # inject_into_tasks is enabled), then layer profile-specific servers
        # on top.  Profile servers win on name collisions.
        injected_mcp: dict[str, dict] = dict(self.config.mcp_server.task_mcp_entry())
        profile_mcp: dict[str, dict] = (
            dict(profile.mcp_servers) if profile and profile.mcp_servers else {}
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

        ctx = TaskContext(
            task_id=task.id,
            description=full_description,
            l0_role=l0_role,
            l1_facts=l1_facts,
            l1_guidance=l1_guidance,
            l2_context=l2_context,
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

            if pipeline_ctx.plan_needs_approval and completed_ok:
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
                await self.db.transition_task(
                    action.task_id, TaskStatus.COMPLETED, context="completed_no_approval"
                )
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
                    brief = f"✅ Task completed: {task.title} (`{task.id}`)"
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
                    agent_type=profile.id if profile else None,
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
                    agent_type=profile.id if profile else None,
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
            if agent and agent.agent_type:
                self._provider_cooldowns[agent.agent_type] = resume_at
                logger.info(
                    "Provider cooldown set: %s until %.0f (%ds from now)",
                    agent.agent_type,
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
