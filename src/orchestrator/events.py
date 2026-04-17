"""Events mixin — event emission and notification helpers."""

from __future__ import annotations

import logging
import time
from typing import Any

from src.notifications.builder import build_agent_summary, build_task_detail
from src.notifications.events import (
    AgentQuestionEvent,
    BudgetWarningEvent,
    ChainStuckEvent,
    TextNotifyEvent,
)
from src.models import Task, TaskStatus

logger = logging.getLogger(__name__)


class EventsMixin:
    """Event emission and notification methods mixed into Orchestrator."""

    async def _emit_task_event(self, event_type: str, task, **extra) -> None:
        """Emit a task lifecycle event for playbooks and subscribers."""
        payload = {
            "task_id": task.id,
            "project_id": task.project_id,
            "title": getattr(task, "title", ""),
        }
        payload.update(extra)
        await self.bus.emit(event_type, payload)

    async def _emit_task_failure(
        self,
        task,
        context: str,
        error: str = "",
        *,
        agent_id: str | None = None,
        agent_type: str | None = None,
    ) -> None:
        """Emit ``task.failed`` event so playbooks and subscribers can react.

        Parameters
        ----------
        agent_id:
            The agent that was executing the task (if known).
        agent_type:
            The vault agent-type identifier (resolved profile ID) for
            agent-type-scoped playbook matching.  See roadmap 6.1.3.
        """
        extras: dict[str, Any] = {
            "status": task.status.value if hasattr(task.status, "value") else str(task.status),
            "context": context,
            "error": error,
        }
        if agent_id is not None:
            extras["agent_id"] = agent_id
        if agent_type is not None:
            extras["agent_type"] = agent_type
        await self._emit_task_event("task.failed", task, **extras)

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

    async def _check_workflow_stage_completion(self, task: Task) -> None:
        """Check if a completed task finishes a workflow stage and emit an event.

        Called after a task transitions to COMPLETED.  If the task belongs
        to a workflow (``task.workflow_id`` is set), this method loads the
        workflow and checks whether **all** tasks tracked by the workflow
        have reached COMPLETED status.  When they have, a
        ``workflow.stage.completed`` event is emitted on the bus so that
        coordination playbooks can advance to the next stage.

        The check is intentionally conservative — only COMPLETED tasks count.
        Failed or blocked tasks do NOT satisfy stage completion; the playbook
        is expected to handle those via ``task.failed`` listeners.

        Args:
            task: The task that just completed.
        """
        if not task.workflow_id:
            return

        try:
            workflow = await self.db.get_workflow(task.workflow_id)
        except Exception as e:
            logger.warning(
                "Failed to fetch workflow %s for stage completion check: %s",
                task.workflow_id,
                e,
            )
            return

        if not workflow:
            logger.debug(
                "Workflow %s not found for task %s — skipping stage check",
                task.workflow_id,
                task.id,
            )
            return

        if workflow.status != "running":
            logger.debug(
                "Workflow %s is '%s', not 'running' — skipping stage check",
                workflow.workflow_id,
                workflow.status,
            )
            return

        if not workflow.task_ids:
            return

        # Check whether every task in the workflow has reached COMPLETED.
        for tid in workflow.task_ids:
            try:
                t = await self.db.get_task(tid)
            except Exception:
                logger.debug("Could not fetch task %s during stage check", tid)
                return
            if not t or t.status != TaskStatus.COMPLETED:
                return  # At least one task is still outstanding

        # All tasks completed — emit stage completion event.
        stage = workflow.current_stage or ""
        logger.info(
            "Workflow %s stage '%s' completed (all %d tasks done)",
            workflow.workflow_id,
            stage,
            len(workflow.task_ids),
        )
        await self.bus.emit(
            "workflow.stage.completed",
            {
                "workflow_id": workflow.workflow_id,
                "stage": stage,
                "task_ids": list(workflow.task_ids),
            },
        )

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
