"""Discord consumer of notification events from the EventBus.

Subscribes to ``notify.*`` events and formats them for Discord — building
embeds, attaching interactive views, managing threads, and routing messages
to the correct project channel.

This handler replaces the direct callback wiring between the orchestrator
and Discord bot.  The orchestrator emits transport-agnostic events; this
handler translates them into Discord-specific presentation.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from src.event_bus import EventBus

logger = logging.getLogger(__name__)


def _task_proxy(td: Any) -> SimpleNamespace:
    """Wrap a TaskDetail Pydantic model to look like a domain Task for formatters.

    The existing Discord formatters access ``task.status.value``,
    ``task.assigned_agent_id``, ``task.branch_name``, etc.  This proxy
    provides attribute-compatible access so we can reuse the formatters
    without modifying them.
    """
    status_str = td.status if isinstance(td.status, str) else str(td.status)
    return SimpleNamespace(
        id=td.id,
        project_id=td.project_id,
        title=td.title,
        description=getattr(td, "description", ""),
        priority=getattr(td, "priority", 0),
        status=SimpleNamespace(value=status_str),
        assigned_agent_id=getattr(td, "assigned_agent", None),
        retry_count=getattr(td, "retry_count", 0),
        max_retries=getattr(td, "max_retries", 3),
        requires_approval=getattr(td, "requires_approval", False),
        is_plan_subtask=getattr(td, "is_plan_subtask", False),
        task_type=SimpleNamespace(value=td.task_type) if getattr(td, "task_type", None) else None,
        parent_task_id=getattr(td, "parent_task_id", None),
        branch_name=None,  # Not in TaskDetail; set by caller if needed
        pr_url=getattr(td, "pr_url", None),
        profile_id=getattr(td, "profile_id", None),
        auto_approve_plan=getattr(td, "auto_approve_plan", False),
    )


def _agent_proxy(ag: Any) -> SimpleNamespace:
    """Wrap an AgentSummary Pydantic model to look like domain Agent for formatters."""
    return SimpleNamespace(
        id=ag.workspace_id,
        workspace_id=ag.workspace_id,
        name=ag.name or ag.workspace_id,
        state=ag.state,
        current_task_id=ag.current_task_id,
        current_task_title=ag.current_task_title,
    )


def _output_proxy(
    *,
    summary: str = "",
    files_changed: list[str] | None = None,
    tokens_used: int = 0,
    error_message: str | None = None,
) -> SimpleNamespace:
    """Build an AgentOutput-like proxy from event data."""
    return SimpleNamespace(
        summary=summary,
        files_changed=files_changed or [],
        tokens_used=tokens_used,
        error_message=error_message,
    )


class DiscordNotificationHandler:
    """Subscribes to notification events and renders them for Discord.

    Holds a reference to the bot for message sending, thread management,
    and interactive view registration.  Subscriptions are registered in
    ``__init__`` and can be torn down via ``shutdown()``.
    """

    def __init__(self, bot: Any, bus: EventBus):
        self.bot = bot
        self.bus = bus
        self._unsubscribes: list[Any] = []

        # Thread management — maps task_id → (send_to_thread, notify_main)
        self._task_threads: dict[str, tuple[Any, Any]] = {}

        # Subscribe to all notification events
        events = [
            ("notify.task_started", self._on_task_started),
            ("notify.task_completed", self._on_task_completed),
            ("notify.task_failed", self._on_task_failed),
            ("notify.task_blocked", self._on_task_blocked),
            ("notify.task_stopped", self._on_task_stopped),
            ("notify.agent_question", self._on_agent_question),
            ("notify.plan_awaiting_approval", self._on_plan_awaiting_approval),
            ("notify.pr_created", self._on_pr_created),
            ("notify.merge_conflict", self._on_merge_conflict),
            ("notify.push_failed", self._on_push_failed),
            ("notify.budget_warning", self._on_budget_warning),
            ("notify.chain_stuck", self._on_chain_stuck),
            ("notify.stuck_defined_task", self._on_stuck_defined_task),
            ("notify.system_online", self._on_system_online),
            ("notify.task_thread_open", self._on_task_thread_open),
            ("notify.task_message", self._on_task_message),
            ("notify.task_thread_close", self._on_task_thread_close),
            ("notify.text", self._on_text),
        ]
        for event_type, handler in events:
            unsub = bus.subscribe(event_type, handler)
            self._unsubscribes.append(unsub)

    def shutdown(self) -> None:
        """Remove all event subscriptions."""
        for unsub in self._unsubscribes:
            unsub()
        self._unsubscribes.clear()
        self._task_threads.clear()

    def _get_handler(self) -> Any:
        """Get the command handler from the bot for interactive views."""
        try:
            return self.bot.agent.handler
        except AttributeError:
            return None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_task_started(self, data: dict) -> None:
        event = TaskStartedEvent(**{k: v for k, v in data.items() if k != "_event_type"})
        if event.is_reopened:
            return  # suppress noisy notifications for reopened tasks

        from src.discord.notifications import (
            TaskStartedView,
            format_task_started,
            format_task_started_embed,
        )

        task_p = _task_proxy(event.task)
        agent_p = _agent_proxy(event.agent)
        ws_p = None
        if event.workspace_path:
            ws_p = SimpleNamespace(
                name=event.workspace_name or None,
                workspace_path=event.workspace_path,
            )

        embed = format_task_started_embed(task_p, agent_p, workspace=ws_p)
        handler_ref = self._get_handler()
        view = TaskStartedView(
            event.task.id,
            handler=handler_ref,
            task_description=event.task_description,
            task_contexts=event.task_contexts,
        )
        msg = await self.bot._send_message(
            format_task_started(task_p, agent_p, workspace=ws_p),
            project_id=event.project_id,
            embed=embed,
            view=view,
        )
        # Store the sent message for later deletion (task-started → superseded)
        if msg is not None:
            orch = self.bot.orchestrator
            if hasattr(orch, "_task_started_messages"):
                orch._task_started_messages[event.task.id] = msg

    async def _on_task_completed(self, data: dict) -> None:
        event = TaskCompletedEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import format_task_completed_embed

        task_p = _task_proxy(event.task)
        agent_p = _agent_proxy(event.agent)
        output_p = _output_proxy(
            summary=event.summary,
            files_changed=event.files_changed,
            tokens_used=event.tokens_used,
        )
        embed = format_task_completed_embed(task_p, agent_p, output_p)

        brief = f"✅ Task completed: {event.task.title} (`{event.task.id}`)"

        # Post to thread if available, otherwise to channel
        thread_cbs = self._task_threads.get(event.task.id)
        if thread_cbs:
            send_thread, notify_main = thread_cbs
            if send_thread:
                await send_thread(brief)
            if notify_main:
                await notify_main(brief, embed=embed)
        else:
            await self.bot._send_message(
                brief,
                project_id=event.project_id,
                embed=embed,
            )

    async def _on_task_failed(self, data: dict) -> None:
        event = TaskFailedEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import (
            TaskFailedView,
            classify_error,
            format_task_failed,
            format_task_failed_embed,
        )

        task_p = _task_proxy(event.task)
        agent_p = _agent_proxy(event.agent)
        output_p = _output_proxy(error_message=event.error_detail or None)
        # Set retry_count from event
        task_p.retry_count = event.retry_count

        embed = format_task_failed_embed(task_p, agent_p, output_p)
        handler_ref = self._get_handler()
        view = TaskFailedView(event.task.id, handler=handler_ref)

        brief = (
            f"⚠️ Task failed: {event.task.title} (`{event.task.id}`) — "
            f"retry {event.retry_count}/{event.max_retries}"
        )

        # Post detailed failure to thread if available
        thread_cbs = self._task_threads.get(event.task.id)
        if thread_cbs:
            send_thread, notify_main = thread_cbs
            error_type, suggestion = classify_error(event.error_detail or None)
            fail_lines = [
                f"**Task Failed:** `{event.task.id}` — {event.task.title}",
                f"Agent: {event.agent.name} | Retry: {event.retry_count}/{event.max_retries}",
                f"Error type: **{error_type}**",
            ]
            if event.error_detail:
                snippet = event.error_detail[:400]
                if len(event.error_detail) > 400:
                    snippet += "…"
                fail_lines.append(f"```\n{snippet}\n```")
            fail_lines.append(f"💡 {suggestion}")
            fail_lines.append(f"_Use `/agent-error {event.task.id}` for full details._")
            if send_thread:
                await send_thread("\n".join(fail_lines))
            if notify_main:
                await notify_main(brief)
        else:
            await self.bot._send_message(
                format_task_failed(task_p, agent_p, output_p),
                project_id=event.project_id,
                embed=embed,
                view=view,
            )

    async def _on_task_blocked(self, data: dict) -> None:
        event = TaskBlockedEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import (
            TaskBlockedView,
            format_task_blocked,
            format_task_blocked_embed,
        )

        task_p = _task_proxy(event.task)
        embed = format_task_blocked_embed(task_p, last_error=event.last_error or None)
        handler_ref = self._get_handler()
        view = TaskBlockedView(event.task.id, handler=handler_ref)

        brief = (
            f"🚫 Task blocked: {event.task.title} (`{event.task.id}`) — "
            f"max retries ({event.task.max_retries}) exhausted"
        )

        thread_cbs = self._task_threads.get(event.task.id)
        if thread_cbs:
            send_thread, notify_main = thread_cbs
            if send_thread:
                await send_thread(format_task_blocked(task_p, last_error=event.last_error or None))
            if notify_main:
                await notify_main(brief)
        else:
            await self.bot._send_message(
                format_task_blocked(task_p, last_error=event.last_error or None),
                project_id=event.project_id,
                embed=embed,
                view=view,
            )

    async def _on_task_stopped(self, data: dict) -> None:
        event = TaskStoppedEvent(**{k: v for k, v in data.items() if k != "_event_type"})
        await self.bot._send_message(
            f"**Task Stopped:** `{event.task.id}` — {event.task.title}",
            project_id=event.project_id,
        )

    async def _on_agent_question(self, data: dict) -> None:
        event = AgentQuestionEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import (
            AgentQuestionView,
            format_agent_question,
            format_agent_question_embed,
        )

        task_p = _task_proxy(event.task)
        agent_p = _agent_proxy(event.agent)
        embed = format_agent_question_embed(task_p, agent_p, event.question)
        handler_ref = self._get_handler()
        view = AgentQuestionView(event.task.id, handler=handler_ref)

        thread_cbs = self._task_threads.get(event.task.id)
        if thread_cbs:
            send_thread, notify_main = thread_cbs
            if send_thread:
                await send_thread(format_agent_question(task_p, agent_p, event.question))
            if notify_main:
                await notify_main(
                    f"❓ Agent question on: {event.task.title} (`{event.task.id}`)",
                    embed=embed,
                )
        else:
            await self.bot._send_message(
                format_agent_question(task_p, agent_p, event.question),
                project_id=event.project_id,
                embed=embed,
                view=view,
            )

    async def _on_plan_awaiting_approval(self, data: dict) -> None:
        event = PlanAwaitingApprovalEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import (
            PlanApprovalView,
            format_plan_approval_embed,
        )

        # Resolve thread URL at delivery time — the orchestrator no longer
        # queries transport-specific URLs; this is the handler's responsibility.
        thread_url = event.thread_url or ""
        if not thread_url:
            try:
                thread_url = await self.bot.get_thread_last_message_url(event.task.id) or ""
            except Exception:
                logger.debug(
                    "Could not resolve thread URL for task %s", event.task.id, exc_info=True
                )

        task_p = _task_proxy(event.task)
        handler_ref = self._get_handler()
        plan_view = PlanApprovalView(event.task.id, handler=handler_ref)

        embed = format_plan_approval_embed(
            task_p,
            raw_content=event.raw_content,
            plan_url=event.plan_url,
            parsed_steps=event.subtasks if event.subtasks else None,
            thread_url=thread_url,
        )
        await self.bot._send_message(
            f"📋 **Plan ready for review:** `{event.task.id}` — {event.task.title}",
            project_id=event.project_id,
            embed=embed,
            view=plan_view,
        )

        # Also post brief to thread
        thread_cbs = self._task_threads.get(event.task.id)
        if thread_cbs:
            _, notify_main = thread_cbs
            if notify_main:
                await notify_main(
                    f"📋 Plan awaiting approval: {event.task.title} (`{event.task.id}`)"
                )

    async def _on_pr_created(self, data: dict) -> None:
        event = PRCreatedEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import (
            TaskApprovalView,
            format_pr_created,
            format_pr_created_embed,
        )

        task_p = _task_proxy(event.task)
        handler_ref = self._get_handler()
        view = TaskApprovalView(event.task.id, handler=handler_ref)

        thread_cbs = self._task_threads.get(event.task.id)
        if thread_cbs:
            send_thread, notify_main = thread_cbs
            if send_thread:
                await send_thread(format_pr_created(task_p, event.pr_url))
            if notify_main:
                brief = (
                    f"🔍 PR created for review: {event.task.title} "
                    f"(`{event.task.id}`)\n{event.pr_url}"
                )
                await notify_main(brief)
        else:
            await self.bot._send_message(
                format_pr_created(task_p, event.pr_url),
                project_id=event.project_id,
                embed=format_pr_created_embed(task_p, event.pr_url),
                view=view,
            )

    async def _on_merge_conflict(self, data: dict) -> None:
        event = MergeConflictEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import format_merge_conflict_embed

        task_p = _task_proxy(event.task)
        embed = format_merge_conflict_embed(task_p, event.branch, event.target_branch)
        await self.bot._send_message(
            f"**Merge Conflict:** Task `{event.task.id}` branch "
            f"`{event.branch}` has conflicts with "
            f"`{event.target_branch}`. Manual resolution needed.",
            project_id=event.project_id,
            embed=embed,
        )

    async def _on_push_failed(self, data: dict) -> None:
        event = PushFailedEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import format_push_failed_embed

        task_p = _task_proxy(event.task)
        embed = format_push_failed_embed(
            task_p,
            event.branch or "unknown",
            event.error_detail or "",
        )
        await self.bot._send_message(
            f"**Push Failed:** Could not push `{event.branch}` for task "
            f"`{event.task.id}`. Details: {event.error_detail}",
            project_id=event.project_id,
            embed=embed,
        )

    async def _on_budget_warning(self, data: dict) -> None:
        event = BudgetWarningEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import format_budget_warning, format_budget_warning_embed

        embed = format_budget_warning_embed(event.project_name, event.usage, event.limit)
        await self.bot._send_message(
            format_budget_warning(event.project_name, event.usage, event.limit),
            project_id=event.project_id,
            embed=embed,
        )

    async def _on_chain_stuck(self, data: dict) -> None:
        event = ChainStuckEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import format_chain_stuck_embed

        # Build task proxies for the formatter
        blocked_p = _task_proxy(event.blocked_task)
        stuck_proxies = [
            SimpleNamespace(id=tid, title=title)
            for tid, title in zip(event.stuck_task_ids, event.stuck_task_titles)
        ]

        embed = format_chain_stuck_embed(blocked_p, stuck_proxies)
        task_list = ", ".join(f"`{tid}`" for tid in event.stuck_task_ids[:5])
        if len(event.stuck_task_ids) > 5:
            task_list += f" +{len(event.stuck_task_ids) - 5} more"

        await self.bot._send_message(
            f"⛓️ **Chain Stuck:** `{event.blocked_task.id}` BLOCKED → "
            f"{len(event.stuck_task_ids)} stuck: {task_list}\n"
            f"`/skip-task {event.blocked_task.id}` or "
            f"`/restart-task {event.blocked_task.id}`",
            project_id=event.project_id,
            embed=embed,
        )

    async def _on_stuck_defined_task(self, data: dict) -> None:
        event = StuckDefinedTaskEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import (
            format_stuck_defined_task,
            format_stuck_defined_task_embed,
        )

        task_p = _task_proxy(event.task)
        # Convert blocking_deps from list[dict] to list[tuple] for formatter
        blocking_tuples = [
            (d.get("id", ""), d.get("title", ""), d.get("status", ""))
            for d in event.blocking_deps
        ]

        embed = format_stuck_defined_task_embed(task_p, blocking_tuples, event.stuck_hours)
        await self.bot._send_message(
            format_stuck_defined_task(task_p, blocking_tuples, event.stuck_hours),
            project_id=event.project_id,
            embed=embed,
        )

    async def _on_system_online(self, data: dict) -> None:
        from src.discord.notifications import format_server_started, format_server_started_embed

        await self.bot._send_message(
            format_server_started(),
            embed=format_server_started_embed(),
        )

    # ------------------------------------------------------------------
    # Thread management
    # ------------------------------------------------------------------

    async def _on_task_thread_open(self, data: dict) -> None:
        event = TaskThreadOpenEvent(**{k: v for k, v in data.items() if k != "_event_type"})
        try:
            result = await self.bot._create_task_thread(
                event.thread_name,
                event.initial_message,
                project_id=event.project_id,
                task_id=event.task_id,
            )
            if result:
                self._task_threads[event.task_id] = result
                logger.debug("Thread opened for task %s", event.task_id)
            else:
                logger.warning("Thread creation returned None for task %s", event.task_id)
        except Exception:
            logger.error("Failed to create thread for task %s", event.task_id, exc_info=True)

    async def _on_task_message(self, data: dict) -> None:
        event = TaskMessageEvent(**{k: v for k, v in data.items() if k != "_event_type"})
        thread_cbs = self._task_threads.get(event.task_id)

        if event.message_type == "brief":
            # Brief notification → main channel (reply to thread root)
            if thread_cbs:
                _, notify_main = thread_cbs
                if notify_main:
                    # Check if embed_data was passed via the event's extra fields
                    await notify_main(event.message)
            else:
                await self.bot._send_message(event.message, project_id=event.project_id)
        else:
            # Agent output or status → thread
            if thread_cbs:
                send_thread, _ = thread_cbs
                if send_thread:
                    await send_thread(event.message)
            else:
                await self.bot._send_message(event.message, project_id=event.project_id)

    async def _on_task_thread_close(self, data: dict) -> None:
        event = TaskThreadCloseEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        # Update thread root message
        if event.final_message:
            try:
                await self.bot.edit_thread_root_message(
                    event.task_id,
                    event.final_message,
                    None,  # no embed change
                )
            except Exception:
                logger.debug(
                    "Could not update thread root for task %s",
                    event.task_id,
                    exc_info=True,
                )

        # Clean up thread references
        self._task_threads.pop(event.task_id, None)

    # ------------------------------------------------------------------
    # Generic text
    # ------------------------------------------------------------------

    async def _on_text(self, data: dict) -> None:
        event = TextNotifyEvent(**{k: v for k, v in data.items() if k != "_event_type"})
        await self.bot._send_message(
            event.message,
            project_id=event.project_id,
        )
