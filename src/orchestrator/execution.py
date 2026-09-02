"""Execution mixin — the agent execution pipeline."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import time

from src.database.queries.hierarchy_queries import HierarchyError
from src.orchestrator.base_workspace import base_checkout_refusal
from src.logging_config import CorrelationContext
from src.discord.notifications import format_task_started
from src.notifications.builder import build_agent_summary, build_task_detail
from src.api.models.agent import AgentSettings, AgentSummary
from src.notifications.events import (
    TaskBlockedEvent,
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskStartedEvent,
    TaskThreadOpenEvent,
)
from src.profiles.sync import underlying_agent_type
from src.review_keys import is_pipeline_review_task
from src.models import (
    AgentOutput,
    AgentResult,
    AgentState,
    PipelineContext,
    RepoConfig,
    TaskStatus,
    TaskType,
)
from src.scheduler import AssignAction

logger = logging.getLogger(__name__)


class ExecutionMixin:
    """Agent execution pipeline methods mixed into Orchestrator."""

    @asynccontextmanager
    async def _task_control_lock(self, task_id: str):
        locks = getattr(self, "_task_control_locks", None)
        if locks is None:
            locks = self._task_control_locks = {}
            self._task_control_owners = {}
        owners = self._task_control_owners
        caller = asyncio.current_task()
        if owners.get(task_id) is caller:
            yield  # Completion/preparation may call guarded cleanup.
            return
        async with locks.setdefault(task_id, asyncio.Lock()):
            owners[task_id] = caller
            try:
                yield
            finally:
                owners.pop(task_id, None)

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
            async with self._task_control_lock(action.task_id):
                current = await self.db.get_task(action.task_id)
                if current and current.status == TaskStatus.PAUSED and current.resume_after is None:
                    return
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
            async with self._task_control_lock(action.task_id):
                current = await self.db.get_task(action.task_id)
                if current and current.status == TaskStatus.PAUSED and current.resume_after is None:
                    return
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

    async def _effective_assignment_task(self, task):
        """Return a class-hydrated task and its fresh route, or ``(None, None)``."""
        from dataclasses import replace

        route = (await self.assignment_routing.routes_for([task])).get(task.id)
        if route is None:
            return None, None
        return replace(task, intelligence_class=route.intelligence_class), route

    async def _check_agent_routing(self, task, agent, *, effective_route=None) -> str | None:
        """Validate current routing independently of an earlier scheduler snapshot."""
        from src.agents.routing import (
            resolve_agent_profile, resolve_task_profile, task_agent_mismatch,
        )

        if agent is None or not agent.enabled or agent.role != "worker":
            return "worker is unavailable"
        if effective_route is None:
            task, effective_route = await self._effective_assignment_task(task)
            if task is None:
                return "awaiting intelligence route"
        else:
            from dataclasses import replace

            task = replace(task, intelligence_class=effective_route.intelligence_class)
        profiles = {profile.id: profile for profile in await self.db.list_profiles()}
        project = await self.db.get_project(task.project_id)
        task_profile = resolve_task_profile(task, project, profiles)
        if task.profile_id and task_profile is None:
            return f"required profile '{task.profile_id}' is not configured"
        return task_agent_mismatch(
            task, agent, task_profile=task_profile,
            agent_profile=resolve_agent_profile(agent, task.project_id, profiles),
            harness_registry=getattr(self, "harness_registry", None),
            intelligence_classes=getattr(
                getattr(self, "session_spec_builder", None), "_intelligence_classes", None,
            ),
            required_provider=effective_route.provider,
        )

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
        task = await self.db.get_task(action.task_id)
        if task is not None:
            if task.is_blocked:
                return "task has unresolved gates or dependencies"
            agent = await self.db.get_agent(action.agent_id)
            mismatch = await self._check_agent_routing(task, agent)
            if mismatch:
                return mismatch
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

        This launches a session-routed task after assignment and workspace
        preparation. Completion is handled separately by ``aq task close``.

        Steps:
        1. **Assign** — mark task IN_PROGRESS and agent BUSY in the DB.
        2. **Workspace setup** — clone/link/init the repo, create or switch
           to the task branch (see ``_prepare_workspace``).  If no workspace
           is available, the task is returned to READY for retry next cycle.
        3. **Session launch** — construct the session and return immediately.
        """
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
        if await self.db.assign_task_to_agent(action.task_id, action.agent_id) is False:
            logger.info("Assignment lost availability: task=%s agent=%s", action.task_id, action.agent_id)
            return

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

        # Resolve the agent profile before preparing its session workspace.
        profile = await self._resolve_profile(task)
        from src.agents.configuration import apply_agent_overrides
        if profile:
            worker_profile = (
                await self.db.get_profile(f"project:{task.project_id}:{agent.profile_id}")
                or await self.db.get_profile(agent.profile_id)
            )
            profile = apply_agent_overrides(profile, agent, agent_profile=worker_profile)
        if profile:
            # Report the selected session CLI rather than the removed runtime.
            _routed = self._is_session_routed(profile)
            logger.info(
                "Task %s: profile='%s' via=%s tools=%s mcp=%s",
                task.id,
                profile.id,
                (
                    f"session/{getattr(profile, 'harness', '') or '?'}"
                    if _routed
                    else "session/unconfigured"
                ),
                profile.allowed_tools or "(default)",
                list(profile.mcp_servers) if profile.mcp_servers else "(none)",
            )
        else:
            logger.info("Task %s: no profile (using system defaults)", task.id)

        # ── Session-runtime routing ──────────────────────────────────────
        # A task worker must select a session harness. There is no runtime
        # fallback after the runtime subsystem removal.
        session_routed = self._is_session_routed(profile)

        if not session_routed:
            raise RuntimeError(f"Task {task.id} {self._why_not_session_routed(profile)}")

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
                assigned_agent_id=None,
            )
            await self._emit_task_event(
                "task.paused",
                task,
                reason="no_workspace",
                resume_after=time.time() + no_ws_backoff,
            )
            await self.db.update_agent(
                action.agent_id, state=AgentState.IDLE, current_task_id=None
            )
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
            elif wait_reason == "branch_held":
                # The task's own branch sits in a slot nobody will move:
                # unlike the two waits above this never clears itself, so
                # it has to reach the operator rather than loop quietly.
                await self._emit_text_notify(
                    f"**Branch In Use:** Task `{task.id}` paused for "
                    f"{no_ws_backoff}s — its branch is checked out in "
                    f"another worktree. Run `aq doctor --check "
                    f"worktrees.orphans` to find the slot holding it.",
                    project_id=action.project_id,
                )
            else:
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

        # Fetch the workspace object for display in notifications.  None for
        # workspace-less platforms (no workspace was provisioned).
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

        # ── Session launch and return ─────────────────────────────────────
        # The session receives its prompt from ``aq prime`` and reports
        # completion through ``aq task close``.
        #
        # Note the wrapper above (``_execute_task_safe_inner``) still wraps
        # this coroutine in ``asyncio.wait_for(stuck_timeout_seconds)``.
        # That is harmless rather than overlooked: the coroutine now returns
        # in milliseconds, so the timeout can never fire on it.  The real
        # stuck-timeout backstop moved to ``SessionReconciler.tick()``
        # step 6, where it can act on a session that outlives the daemon.
        await self._launch_session_for_task(action, task, profile, workspace)
        return

    # ======================================================================
    # Session runtime -- launch and return (session-runtime spec §4, §6)
    # ======================================================================

    def _is_session_routed(self, profile) -> bool:
        """True when this profile's tasks run as sessions.

        A task worker requires both the session service and a session
        harness. With sessions disabled, execution fails explicitly instead
        of falling through to the removed runtime adapter pipeline.

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

    def _why_not_session_routed(self, profile) -> str:
        """The reason ``_is_session_routed`` said no, phrased for an operator.

        Both conditions used to collapse into "legacy runtime dispatch was
        removed", which named a subsystem that no longer exists instead of
        the knob the reader has to turn.  The distinction matters: the flag
        is box-wide and the harness is per profile.
        """
        if not self.config.sessions.enabled:
            return (
                "cannot be dispatched: sessions.enabled is false, and a session is "
                "the only execution path"
            )
        if profile is None:
            return "has no agent profile, so no session harness could be selected"
        if getattr(profile, "lifecycle", "task") == "pool":
            return (
                f"resolved to pool profile '{profile.id}', which is never push-launched; "
                "pool sessions claim their own work"
            )
        return (
            f"has no session harness: profile '{profile.id}' sets no `harness:` "
            "(claude | codex | gemini)"
        )

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

        Harnesses without a transcript reader are left alone:
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
            path = await asyncio.to_thread(reader.resolve_path, work_dir, resume_key)
        except Exception:
            return resume_key

        # Codex stores the identity in the rollout suffix, Claude in the
        # whole basename. Both must prove the exact requested conversation.
        if path is not None:
            discover = getattr(reader, "discover_session_key", None)
            key = discover(path) if discover else None
            if (key or path.stem) == resume_key:
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

    async def _launch_session_for_task(self, action, task, profile, workspace) -> None:
        # Pause must not release a workspace while a provider is still starting.
        async with self._task_control_lock(task.id):
            await self._launch_session_for_task_locked(action, task, profile, workspace)

    async def _launch_session_for_task_locked(
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
        from src.agents.configuration import apply_agent_overrides, resolve_launch_settings
        current = await self.db.get_task(action.task_id)
        if (
            current is None
            or current.status not in (TaskStatus.ASSIGNED, TaskStatus.IN_PROGRESS)
            or current.assigned_agent_id != action.agent_id
            or current.claim_epoch != task.claim_epoch
        ):
            logger.info("Task %s changed ownership or stopped before session launch", action.task_id)
            return
        task = current
        agent = await self.db.get_agent(action.agent_id)
        routed_task, effective_route = await self._effective_assignment_task(task)
        mismatch = "task has unresolved gates or dependencies" if task.is_blocked else None
        if mismatch is None and routed_task is None:
            mismatch = "awaiting intelligence route"
        if mismatch is None:
            mismatch = await self._check_agent_routing(
                task, agent, effective_route=effective_route
            )
        if mismatch:
            await self._fail_session_launch(action, task, f"worker routing changed: {mismatch}")
            return
        task = routed_task
        # Re-resolve unmodified definitions: workspace preparation may await
        # long enough for either routing or worker settings to change.
        profile = await self._resolve_profile(task)
        worker_profile = (
            await self.db.get_profile(f"project:{task.project_id}:{agent.profile_id}")
            or await self.db.get_profile(agent.profile_id)
        )
        profile = apply_agent_overrides(profile, agent, agent_profile=worker_profile)
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

        # The base checkout is never a place to run an agent — see
        # :mod:`src.orchestrator.base_workspace`.  Refusing here rather than
        # at acquisition keeps the guard in front of *every* way a work_dir
        # can be chosen, including a preferred_workspace_id pointing at the
        # base and any future path that bypasses ``acquire_for_task``.
        refusal = await base_checkout_refusal(
            self.db, work_dir, profile, project_id=task.project_id
        )
        if refusal:
            await self._fail_session_launch(action, task, refusal)
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
        from src.claim_file import write_claim_file

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

        launched_at = time.time()
        from dataclasses import replace
        launch_record = SessionRecord(
            id=session_id, task_id=task.id, agent_id=action.agent_id,
            last_claim_epoch=claim_epoch,
            **resolve_launch_settings(profile, harness, self.session_spec_builder,
                                      task_class=task.intelligence_class),
            project_id=task.project_id, profile_id=getattr(profile, "id", "") or "",
            harness=harness.id, provider=provider.name, name=spec.session_name,
            lifecycle="task", state="starting",
            # Only harnesses with a session-id flag accept AQ's UUID.
            session_key=resume_key or (session_id if harness.session_id_flag else None),
            work_dir=work_dir, epoch=self.daemon_epoch, instance_token=instance_token,
            started_at=launched_at, last_activity=launched_at,
            hooks_provisioned=spec.hooks_provisioned,
        )

        async def record_failed_launch(reason):
            try:
                await self.db.create_session(replace(
                    launch_record, state="stopped", desired_state="stopped",
                    ended_at=time.time(), end_reason=reason,
                ))
            except Exception:
                # Recording failure must not prevent the existing resource
                # cleanup and retry backoff from running.
                logger.exception("Could not record failed launch for task %s", task.id)

        try:
            await provider.start(spec)
        except SessionDiedDuringStartup as exc:
            await record_failed_launch("startup_exit")
            await self._fail_session_launch(
                action,
                task,
                f"session died during startup: {exc}",
                stderr_path=exc.start_stderr_path,
            )
            return
        except Exception as exc:
            await record_failed_launch("launch_failed")
            await self._fail_session_launch(action, task, f"session launch failed: {exc}")
            return

        # The row is written *after* the session exists, so a crash between
        # the two leaves an orphan session rather than a phantom row -- and
        # adoption reconciles an orphan session by its env markers, which is
        # the recoverable direction.
        now = time.time()
        try:
            await self.db.create_session(
                replace(launch_record, state="running", last_activity=now)
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
        await self.db.delete_task_meta(task.id, "manual_pause_checkpoint")
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

    async def complete_session_task(self, task, **kwargs) -> dict:
        # Completion owns its workspace until its pipeline and final write finish.
        # A pause queued behind it observes the committed outcome and never
        # releases files still being verified or committed.
        from src.database.queries.task_queries import ManualPauseActive, StaleClaim

        async with self._task_control_lock(task.id):
            current = await self.db.get_task(task.id)
            if current and current.status == TaskStatus.PAUSED and current.resume_after is None:
                raise ManualPauseActive(f"Task {task.id} is manually paused; use resume_task.")
            expected = kwargs.get("expect_claim_epoch")
            if expected is not None and (current is None or current.claim_epoch != expected):
                raise StaleClaim(f"{task.id}: claim epoch {expected} is not current")
            return await self._complete_session_task_locked(task, **kwargs)

    async def _complete_session_task_locked(
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
        session_live: bool = False,
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
            close_session_live=session_live,
            work_outcome=work_outcome,
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

        # Git verification found only fixable issues and the closing session
        # is still live: the close is refused rather than the task reopened.
        # Nothing is transitioned and nothing is released — the task keeps
        # its IN_PROGRESS status, its agent, its workspace and its claim, so
        # the reconciler's orphan rule (live session, task not IN_PROGRESS)
        # never sees a reason to drain the worker that has to fix this.
        if outcome == "pass" and ctx.verification_retry_in_session:
            current = await self.db.get_task(task.id)
            logger.info(
                "Task %s: close refused, %d fixable verification issue(s) returned "
                "to the live session",
                task.id,
                len(ctx.verification_issues),
            )
            return {
                "status": (current or task).status.value,
                "pr_url": None,
                "pipeline_ok": False,
                "retry_count": None,
                "verification_retry": True,
                "issues": list(ctx.verification_issues),
                "feedback": ctx.verification_feedback,
            }

        # ``retry_count`` is only carried on the transient leg; the other
        # branches are terminal and must not bump the counter.
        new_retry: int | None = None
        verification_reopened = outcome == "pass" and ctx.verification_reopened
        if outcome == "pass" and completed_ok:
            new_status = TaskStatus.COMPLETED
            context = "session_close"
        elif verification_reopened:
            # Git verification already transitioned this exact claim back to
            # READY with actionable feedback.  Preserve that retry state;
            # forcing a second READY -> BLOCKED transition races the session
            # reconciler and can strand the old agent/workspace reservation.
            new_status = TaskStatus.READY
            context = "verification_reopen"
        elif outcome == "pass":
            # The pipeline stopped without arranging a retry.  BLOCKED, not
            # COMPLETED: the agent's word triggers verification but does not
            # replace its verdict.
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

        # Persist a pipeline-discovered PR on the row so the review policy
        # (final-reviewer trigger, downstream ``pr-merged`` gates) can see
        # it even when the agent never called ``aq task set --pr-url``.
        pr_kwargs = {"pr_url": pr_url} if pr_url else {}
        try:
            if verification_reopened:
                # _reopen_with_verification_feedback performed the state
                # transition and recorded its context inside this lock.
                pass
            elif new_retry is not None:
                await self.db.transition_task(
                    task.id,
                    new_status,
                    context=context,
                    retry_count=new_retry,
                    assigned_agent_id=None,
                    expect_claim_epoch=expect_claim_epoch,
                    **pr_kwargs,
                )
            else:
                await self.db.transition_task(
                    task.id,
                    new_status,
                    context=context,
                    assigned_agent_id=None,
                    expect_claim_epoch=expect_claim_epoch,
                    **pr_kwargs,
                )
        except HierarchyError as exc:
            # Invariant 6 (spec §7): the task has open children, so it stays
            # where it was.  The rest of the close (event, resource release)
            # still runs — it reports the status the task actually has.
            logger.warning("session-close transition refused for %s: %s", task.id, exc)
            refreshed = await self.db.get_task(task.id)
            if refreshed:
                new_status = refreshed.status

        # ``task.failed`` is the trigger for the reflection playbook
        # (``vault/templates/reflection-playbook.md`` -> deep tier) and for
        # the failure-notification path.  The legacy execution tail raised it
        # when a failure went terminal; the session path replaced that tail
        # and only ever emitted ``task.closed``, so a worker that burned its
        # retry budget or closed ``--failure-class hard`` got no reflection
        # and no notification at all.
        #
        # Every terminal BLOCKED leg qualifies — retry budget spent
        # (``max_retries``), hard failure (``session_close_hard_failure``)
        # and a pass whose pipeline stopped short
        # (``session_close_pipeline_stop``).  The retry leg (READY) is
        # deliberately excluded: the task is not finished failing yet.
        # Emitted before ``task.closed`` so a subscriber sees the failure
        # ahead of the close, and best-effort so a blowing-up subscriber
        # cannot undo a committed transition.
        if new_status == TaskStatus.BLOCKED:
            try:
                await self._emit_task_failure(
                    task,
                    context,
                    error=notes or "",
                    agent_id=task.assigned_agent_id,
                    agent_type=task.profile_id,
                    status=new_status.value,
                )
            except Exception:
                logger.warning(
                    "Task %s: task.failed emit failed (state is BLOCKED)",
                    task.id,
                    exc_info=True,
                )

        # ``task.completed`` is the event the review pipeline triggers on
        # (``per-task-review`` / ``per-branch-final-review`` in
        # default-pipeline.md).  The legacy blocking tail of ``_execute_task``
        # used to raise it, but every agent is session-routed now — that tail
        # is dead code below the "Session-runtime fork", so this close path is
        # the only place an ordinary task can still announce that it finished.
        # Without it workers opened PRs, closed clean, and nothing ever
        # reviewed or merged them.
        #
        # It is emitted *before* ``task.closed`` and only for a task that
        # actually reached COMPLETED: the retry/blocked legs are not finished
        # work, and spawning a reviewer for a task about to be retried would
        # be worse than the silence.  Ordering matters for the guards, not the
        # payload — ``_dispatch_playbook`` hydrates ``event.task`` from a fresh
        # ``db.get_task``, so the transition above must already have committed
        # ``pr_url`` for ``event.task.pr_url`` to read as truthy.
        if new_status == TaskStatus.COMPLETED:
            try:
                # ``no_code`` is the same verdict git verification used a
                # moment ago (``_task_produces_no_code``: ``read_only``
                # profile or ``--work-outcome no-op``).  The review rules in
                # default-pipeline.md guard on it: a reviewer's own task
                # carries a ``branch_name`` like any other session task (the
                # slot is checked out on ``aq/<id>``), so without this flag
                # every finished review spawned a review *of the review*, and
                # so on — reviewer tasks nested three deep on the live queue.
                # ``truthy: false`` in the guard means an emitter that does
                # not set the key (container settlement, hand-written events)
                # still fires the review, so this only ever narrows.
                #
                # ``review_task`` is the structural half of the same guard.
                # ``no_code`` is only as good as the reviewer profile's
                # ``read_only`` flag: an operator who hands the reviewer
                # Write/Edit tools (``read_only: false``) turns it off and the
                # recursion is back (task sound-horizon-77.18.2).  The pipeline
                # stamps every review it creates with a ``review:task:`` /
                # ``branch-review:`` dedup key, so a finishing task carrying
                # one *is* a review whatever its profile says, and the rules
                # guard on this flag as well.
                no_code = await self._task_produces_no_code(ctx)
                await self._emit_task_event(
                    "task.completed",
                    task,
                    agent_id=task.assigned_agent_id,
                    agent_type=task.profile_id,
                    no_code=no_code,
                    review_task=is_pipeline_review_task(task.dedup_key),
                )
            except Exception:
                # Best-effort, exactly like the notification below it: a
                # subscriber blowing up must not undo a committed COMPLETED.
                logger.warning(
                    "Task %s: task.completed emit failed (state is COMPLETED)",
                    task.id,
                    exc_info=True,
                )
        # The ``notify.*`` transports (Discord today) lost their task-outcome
        # feed when the legacy execution tail was deleted: that tail was the
        # only emitter of ``notify.task_completed`` / ``notify.task_failed`` /
        # ``notify.task_blocked``, so ``DiscordNotificationHandler`` kept the
        # subscriptions but never heard from any of them again.  Session close
        # is now the only place an ordinary task reaches a terminal state, so
        # the pairing is restored here with the tail's own mapping: retryable
        # failure -> task_failed, retries spent -> task_blocked.  Best-effort,
        # like the emits above — a transport error must not undo a committed
        # transition.
        notify_error = output.error_message or ""
        if verification_reopened and ctx.verification_feedback:
            notify_error = ctx.verification_feedback
        elif not notify_error and outcome == "pass" and not completed_ok:
            notify_error = "completion pipeline did not finish"
        try:
            await self._emit_close_notify(
                task,
                agent,
                outcome=outcome,
                new_status=new_status,
                context=context,
                error_detail=notify_error,
                retry_count=new_retry if new_retry is not None else (task.retry_count or 0),
                summary=output.summary or "",
                files_changed=list(output.files_changed or []),
                tokens_used=output.tokens_used or 0,
            )
        except Exception:
            logger.warning(
                "Task %s: outcome notification failed (state is %s)",
                task.id,
                new_status.value,
                exc_info=True,
            )
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
                task.id, agent_id=task.assigned_agent_id, workspace_path=workspace_path,
                expect_claim_epoch=task.claim_epoch,
            )

        return {
            "status": new_status.value,
            "pr_url": pr_url,
            "pipeline_ok": completed_ok,
            "retry_count": new_retry,
        }

    async def _emit_close_notify(
        self,
        task,
        agent,
        *,
        outcome: str,
        new_status: TaskStatus,
        context: str,
        error_detail: str = "",
        retry_count: int = 0,
        summary: str = "",
        files_changed: list[str] | None = None,
        tokens_used: int = 0,
    ) -> None:
        """Announce a session-closed task's outcome on the ``notify.*`` bus.

        One event per close, chosen from the status the task actually landed
        in rather than from what the agent claimed:

        * ``COMPLETED``               -> ``notify.task_completed``
        * ``BLOCKED`` via max retries -> ``notify.task_blocked``
        * any other failing leg       -> ``notify.task_failed``

        A close that did not settle the task — Invariant 6 held a container
        open, say — announces nothing: there is no outcome to report yet.
        """
        # ``task`` is the pre-transition row the close started from, so its
        # status and retry count are one step behind what was just committed.
        detail = build_task_detail(task)
        detail.status = new_status.value
        detail.retry_count = retry_count
        agent_summary = (
            build_agent_summary(agent)
            if agent is not None
            else AgentSummary(
                id=task.assigned_agent_id or "",
                name=task.assigned_agent_id or "unknown",
                profile_id=task.profile_id or "",
                settings=AgentSettings(
                    name=task.assigned_agent_id or "unknown",
                    profile_id=task.profile_id or "",
                ),
            )
        )

        if new_status == TaskStatus.COMPLETED:
            await self._emit_notify(
                "notify.task_completed",
                TaskCompletedEvent(
                    task=detail,
                    agent=agent_summary,
                    summary=summary,
                    files_changed=files_changed or [],
                    tokens_used=tokens_used,
                    project_id=task.project_id,
                ),
            )
            return

        if new_status == TaskStatus.BLOCKED and context == "max_retries":
            await self._emit_notify(
                "notify.task_blocked",
                TaskBlockedEvent(
                    task=detail,
                    last_error=error_detail,
                    project_id=task.project_id,
                ),
            )
            return

        if new_status not in (TaskStatus.BLOCKED, TaskStatus.READY):
            # The transition was refused (open children) — the task is still
            # where it was, so there is no outcome to announce.
            return
        if outcome == "pass" and new_status == TaskStatus.READY and not error_detail:
            # A plain verification reopen with nothing to say.
            return

        await self._emit_notify(
            "notify.task_failed",
            TaskFailedEvent(
                task=detail,
                agent=agent_summary,
                error_detail=error_detail,
                retry_count=retry_count,
                max_retries=task.max_retries or 0,
                project_id=task.project_id,
            ),
        )

    async def release_session_task_resources(
        self,
        task_id: str,
        *,
        agent_id: str | None = None,
        workspace_path: str | None = None,
        expect_claim_epoch: int | None = None,
    ) -> None:
        async with self._task_control_lock(task_id):
            current = await self.db.get_task(task_id)
            if current is not None:
                if current.status == TaskStatus.PAUSED and current.resume_after is None:
                    return
                if expect_claim_epoch is not None and current.claim_epoch != expect_claim_epoch:
                    return
            await self._release_session_task_resources_locked(
                task_id, agent_id=agent_id, workspace_path=workspace_path,
            )

    async def _release_session_task_resources_locked(
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

        Repeated cleanup only releases the old task's locks and assignment;
        it must not disturb a durable worker already reused for another task.
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
            adapter = self._adapters.get(agent_id)
            try:
                released = await self.db.release_agent_for_task(agent_id, task_id)
                if released and self._adapters.get(agent_id) is adapter:
                    self._adapters.pop(agent_id, None)
            except Exception:
                logger.error("Task %s: could not idle agent %s", task_id, agent_id, exc_info=True)
        self._task_exec_start.pop(task_id, None)
        self._task_pre_exec_sha.pop(task_id, None)
