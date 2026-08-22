"""Discord consumer of notification events from the EventBus.

Subscribes to ``notify.*`` events and formats them for Discord — building
embeds, attaching interactive views, managing threads, and routing messages
to the correct project channel.

This handler replaces the direct callback wiring between the orchestrator
and Discord bot.  The orchestrator emits transport-agnostic events; this
handler translates them into Discord-specific presentation.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from src.notifications.events import (
    AgentQuestionEvent,
    BudgetWarningEvent,
    ChainStuckEvent,
    MergeConflictEvent,
    PlanAwaitingApprovalEvent,
    PlaybookRunCompletedEvent,
    PlaybookRunFailedEvent,
    PlaybookRunPausedEvent,
    PlaybookRunStartedEvent,
    PlaybookRunTimedOutEvent,
    PRCreatedEvent,
    PushFailedEvent,
    StuckDefinedTaskEvent,
    TaskAddedEvent,
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
        skip_verification=getattr(td, "skip_verification", False),
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
        # Per-stream state for streaming runtimes (e.g. ACPX).  Each stream
        # has a long-lived worker task that owns Discord I/O for that stream
        # — the event handler just updates ``latest_text`` and pings the
        # worker.  Newer text always overwrites older pending text, so a
        # slow Discord (rate limit, transient 5xx) cannot back up the event
        # bus and "freeze" the live stream from the user's perspective.
        # Schema: stream_id → state dict with keys::
        #   msg          : discord.Message | None  (current, owned by worker)
        #   displayed    : str                       (text shown in `msg`)
        #   prefix_len   : int                       (chars in PRIOR messages)
        #   latest_text  : str | None                (newest pending text)
        #   latest_done  : bool                      (sticky stream_done flag)
        #   ping         : asyncio.Event             (signals new pending data)
        #   worker       : asyncio.Task | None       (the renderer)
        self._stream_states: dict[str, dict[str, Any]] = {}

        # Wave 4: track posted gate messages so gate.resolved can edit them.
        self._gate_messages: dict[str, Any] = {}

        # Subscribe to all notification events
        events = [
            ("notify.task_added", self._on_task_added),
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
            ("notify.playbook_run_started", self._on_playbook_run_started),
            ("notify.playbook_run_completed", self._on_playbook_run_completed),
            ("notify.playbook_run_failed", self._on_playbook_run_failed),
            ("notify.playbook_run_paused", self._on_playbook_run_paused),
            ("notify.playbook_run_timed_out", self._on_playbook_run_timed_out),
            ("notify.task_thread_open", self._on_task_thread_open),
            ("notify.task_message", self._on_task_message),
            ("notify.task_thread_close", self._on_task_thread_close),
            ("notify.text", self._on_text),
            # Phase 4 cutover (supervisor-agent.md §9 row 2): replies from
            # supervisor sessions to Discord users arrive as ``message.sent``
            # events with ``to_kind=user``; render them into the originating
            # project channel.
            ("message.sent", self._on_message_sent),
            # Wave 4 — work-graph gates as interactive Discord embeds.
            ("gate.created", self._on_gate_created),
            ("gate.resolved", self._on_gate_resolved),
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
        self._gate_messages.clear()

    def _get_handler(self) -> Any:
        """Get the command handler from the bot for interactive views."""
        try:
            return self.bot.handler
        except AttributeError:
            return None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_task_added(self, data: dict) -> None:
        event = TaskAddedEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import (
            format_task_added,
            format_task_added_embed,
        )

        task_p = _task_proxy(event.task)
        embed = format_task_added_embed(task_p, source=event.source)
        msg = await self.bot._send_message(
            format_task_added(task_p),
            project_id=event.project_id,
            embed=embed,
        )
        # Track the sent message so it can be auto-deleted when the task
        # transitions to task_started (reduces channel clutter).
        if msg is not None:
            orch = self.bot.orchestrator
            if hasattr(orch, "_task_added_messages"):
                orch._task_added_messages[event.task.id] = msg

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
            (d.get("id", ""), d.get("title", ""), d.get("status", "")) for d in event.blocking_deps
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
    # Playbook human-in-the-loop notifications (roadmap 5.4.2)
    # ------------------------------------------------------------------

    async def _on_playbook_run_paused(self, data: dict) -> None:
        event = PlaybookRunPausedEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import (
            PlaybookResumeView,
            format_playbook_paused,
            format_playbook_paused_embed,
        )

        handler_ref = self._get_handler()
        embed = format_playbook_paused_embed(
            playbook_id=event.playbook_id,
            run_id=event.run_id,
            node_id=event.node_id,
            last_response=event.last_response,
            running_seconds=event.running_seconds,
            tokens_used=event.tokens_used,
        )
        view = PlaybookResumeView(event.run_id, handler=handler_ref)

        msg = await self.bot._send_message(
            format_playbook_paused(
                playbook_id=event.playbook_id,
                run_id=event.run_id,
                node_id=event.node_id,
            ),
            project_id=event.project_id,
            embed=embed,
            view=view,
        )
        if msg:
            view.set_message(msg)

    async def _on_playbook_run_timed_out(self, data: dict) -> None:
        event = PlaybookRunTimedOutEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import (
            format_playbook_timed_out,
            format_playbook_timed_out_embed,
        )

        embed = format_playbook_timed_out_embed(
            playbook_id=event.playbook_id,
            run_id=event.run_id,
            node_id=event.node_id,
            timeout_seconds=event.timeout_seconds,
            waited_seconds=event.waited_seconds,
            tokens_used=event.tokens_used,
            transitioned_to=event.transitioned_to,
        )

        await self.bot._send_message(
            format_playbook_timed_out(
                playbook_id=event.playbook_id,
                run_id=event.run_id,
                node_id=event.node_id,
                transitioned_to=event.transitioned_to,
            ),
            project_id=event.project_id,
            embed=embed,
        )

    async def _on_playbook_run_started(self, data: dict) -> None:
        event = PlaybookRunStartedEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import (
            format_playbook_started,
            format_playbook_started_embed,
        )

        embed = format_playbook_started_embed(
            playbook_id=event.playbook_id,
            run_id=event.run_id,
            trigger_event_type=event.trigger_event_type,
            scope=event.scope,
        )
        await self.bot._send_message(
            format_playbook_started(
                playbook_id=event.playbook_id,
                run_id=event.run_id,
            ),
            project_id=event.project_id,
            embed=embed,
        )

    async def _on_playbook_run_completed(self, data: dict) -> None:
        event = PlaybookRunCompletedEvent(
            **{k: v for k, v in data.items() if k != "_event_type"}
        )

        from src.discord.notifications import (
            format_playbook_completed,
            format_playbook_completed_embed,
        )

        embed = format_playbook_completed_embed(
            playbook_id=event.playbook_id,
            run_id=event.run_id,
            duration_seconds=event.duration_seconds,
            tokens_used=event.tokens_used,
            node_count=event.node_count,
            final_context=event.final_context,
        )
        await self.bot._send_message(
            format_playbook_completed(
                playbook_id=event.playbook_id,
                run_id=event.run_id,
                duration_seconds=event.duration_seconds,
            ),
            project_id=event.project_id,
            embed=embed,
        )

    async def _on_playbook_run_failed(self, data: dict) -> None:
        event = PlaybookRunFailedEvent(**{k: v for k, v in data.items() if k != "_event_type"})

        from src.discord.notifications import (
            format_playbook_run_failed,
            format_playbook_run_failed_embed,
        )

        embed = format_playbook_run_failed_embed(
            playbook_id=event.playbook_id,
            run_id=event.run_id,
            failed_at_node=event.failed_at_node,
            error=event.error,
            duration_seconds=event.duration_seconds,
            tokens_used=event.tokens_used,
        )
        await self.bot._send_message(
            format_playbook_run_failed(
                playbook_id=event.playbook_id,
                run_id=event.run_id,
                failed_at_node=event.failed_at_node,
            ),
            project_id=event.project_id,
            embed=embed,
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

        # Streaming runtimes (ACPX) set stream_id and send the cumulative
        # turn text on each update.  Edit a single Discord message in place
        # rather than posting a new one per chunk.  Chain to additional
        # messages when the stream's text exceeds Discord's ~2000-char
        # limit.  Brief notifications never stream — they always post.
        if event.stream_id and event.message_type != "brief":
            await self._handle_streamed_task_message(event)
            return

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

    async def _handle_streamed_task_message(self, event: "TaskMessageEvent") -> None:
        """Enqueue a stream update and ensure a worker is rendering it.

        Decouples Discord I/O (which can stall on rate limits) from the
        event bus.  Each stream has at most one in-flight render task;
        new updates overwrite ``latest_text`` so the worker always sees
        the freshest cumulative text when it picks up the next round.
        Returns immediately — no awaiting Discord here.
        """
        thread = self.bot._task_thread_objects.get(event.task_id)
        if thread is None:
            # No thread → can't reliably edit.  Fall back to regular send.
            await self.bot._send_message(event.message, project_id=event.project_id)
            return

        state = self._stream_states.get(event.stream_id)
        if state is None:
            state = {
                "msg": None,
                "displayed": "",
                "prefix_len": 0,
                "latest_text": None,
                "latest_done": False,
                "ping": asyncio.Event(),
                "worker": None,
            }
            self._stream_states[event.stream_id] = state

        # Always overwrite — text is cumulative, newer strictly contains older.
        state["latest_text"] = event.message or ""
        if event.stream_done:
            state["latest_done"] = True
        state["ping"].set()

        worker = state["worker"]
        if worker is None or worker.done():
            state["worker"] = asyncio.create_task(
                self._stream_worker(event.stream_id, event.task_id, event.project_id, thread)
            )

    async def _stream_worker(
        self,
        stream_id: str,
        task_id: str,
        project_id: str,
        thread: Any,
    ) -> None:
        """Long-lived per-stream renderer: drains pending text into Discord.

        Owns the Discord ``Message`` for the current stream segment.  On
        each iteration: waits for the ping, reads the latest pending
        text, renders to Discord, loops.  Exits when ``latest_done`` is
        set after a render.  All exceptions are logged but do not kill
        the worker — the next ping retries.
        """
        DISCORD_LIMIT = 1990  # headroom under Discord's 2000 hard limit

        while True:
            state = self._stream_states.get(stream_id)
            if state is None:
                return  # stream was reaped externally
            try:
                await state["ping"].wait()
            except asyncio.CancelledError:
                return
            state["ping"].clear()

            text = state["latest_text"]
            done = state["latest_done"]
            if text is None:
                if done:
                    self._stream_states.pop(stream_id, None)
                    return
                continue  # spurious wake; re-wait

            # Snapshot what we'll render this round.  latest_text may be
            # overwritten while we're rendering — that's fine, the next
            # iteration picks it up.
            current_msg = state["msg"]
            displayed = state["displayed"]
            prefix_len = state["prefix_len"]

            suffix = text[prefix_len:]
            if not suffix:
                # No growth in the current message segment.  Honour done.
                if done:
                    self._stream_states.pop(stream_id, None)
                    return
                continue

            # Chunk the suffix into Discord-sized pieces (handles overflow
            # mid-stream by chaining new messages under the same stream_id).
            chunks: list[tuple[int, str]] = []
            i = prefix_len
            while i < len(text):
                end = min(len(text), i + DISCORD_LIMIT)
                chunks.append((end, text[i:end]))
                i = end

            for idx, (chunk_end, body) in enumerate(chunks):
                is_first = idx == 0
                is_last = idx == len(chunks) - 1
                try:
                    if is_first and current_msg is not None:
                        if body != displayed:
                            await current_msg.edit(content=body)
                        new_msg = current_msg
                    else:
                        new_msg = await thread.send(body or "…")
                except Exception:
                    logger.exception(
                        "Stream %s: Discord I/O failed for task %s — will retry "
                        "on next update",
                        stream_id,
                        task_id,
                    )
                    # Don't update state; the next ping will retry against
                    # the (still-current) Message.  Worker stays alive.
                    break

                if is_last:
                    state["msg"] = new_msg
                    state["displayed"] = body
                    state["prefix_len"] = chunk_end - len(body)
                current_msg = new_msg
                displayed = body

            if state["latest_done"] and not state["ping"].is_set():
                # Final flush done; release state so a future stream with
                # the same id (unlikely — stream_ids are uuid4) starts fresh.
                self._stream_states.pop(stream_id, None)
                return

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

    # ------------------------------------------------------------------
    # Supervisor-session chat replies (Phase 4 cutover)
    # ------------------------------------------------------------------

    async def _on_message_sent(self, data: dict) -> None:
        """Render ``message.sent`` events destined for Discord users.

        Two producer paths land here:

        1. ``from_kind == "session"``: supervisor/agent reply to a user
           (Phase-4 cutover reply path).  Rendered as plain text via
           ``_send_long_message``.
        2. ``from_kind == "system"`` with ``from_id == "delivery-engine"``:
           a parked-message notification from
           ``MessageDeliveryEngine._maybe_park`` — the user's original
           message could not be delivered.  Rendered as a warning embed
           so the failure is visible instead of silently dropped.

        Scope guards: ``to_kind`` must be ``user``; ``thread_id`` must
        carry the ``discord:`` prefix set by the cutover send path.  User-
        authored echoes (``from_kind == "user"``) are dropped.
        """
        import discord

        if data.get("to_kind") != "user":
            return
        from_kind = data.get("from_kind")
        if from_kind not in ("session", "system"):
            return
        thread_id = data.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.startswith("discord:"):
            return
        project_id = data.get("project_id")
        message_id = data.get("message_id")
        if not project_id or not message_id:
            return
        channel_id_str = thread_id.split(":", 1)[1] if ":" in thread_id else ""
        try:
            db = self.bot.orchestrator.db
            msg = await db.get_message(message_id)
        except Exception:
            logger.exception("message.sent: failed to load message %s", message_id)
            return
        if msg is None or not msg.body:
            return

        channel = None
        if channel_id_str.isdigit():
            try:
                channel = self.bot.get_channel(int(channel_id_str))
            except Exception:
                channel = None
        if channel is None and channel_id_str.isdigit():
            logger.warning(
                "message.sent: channel %s not resolvable via bot.get_channel; "
                "falling back to project channel resolver (project=%s)",
                channel_id_str,
                project_id,
            )

        try:
            if from_kind == "system":
                # Parked-message warning — render as an embed so it is
                # visually distinct from normal supervisor replies.
                body = msg.body
                desc = body if len(body) <= 3800 else body[:3800] + "…"
                embed = discord.Embed(
                    title="⚠️ Message not delivered",
                    description=desc,
                    color=discord.Color.orange(),
                )
                brief = "⚠️ A previous message was not delivered — see details."
                if channel is not None:
                    await self.bot._safe_api_call(
                        channel.send(content=brief, embed=embed),
                        critical=False,
                        context="message.sent parked warning",
                    )
                else:
                    await self.bot._send_message(brief, project_id=project_id, embed=embed)
            else:
                # from_kind == "session" — plain text reply.
                if channel is not None:
                    await self.bot._send_long_message(channel, msg.body)
                else:
                    await self.bot._send_message(msg.body, project_id=project_id)
        except Exception:
            logger.exception(
                "message.sent: failed to post reply for project %s", project_id
            )

    # ------------------------------------------------------------------
    # Work-graph gates (Wave 4)
    # ------------------------------------------------------------------

    async def _on_gate_created(self, data: dict) -> None:
        """Render a gate.created event as an embed with Approve/Deny buttons."""
        from src.discord.gate_view import GateView, build_gate_embed

        gate_id = data.get("gate_id")
        project_id = data.get("project_id")
        if not gate_id or not project_id:
            return
        embed = build_gate_embed(data)
        handler_ref = self._get_handler()
        gid = str(gate_id)
        view = GateView(
            gid,
            handler=handler_ref,
            bot=self.bot,
            on_timeout_evict=lambda g: self._gate_messages.pop(g, None),
        )
        brief = f"⏸ Gate `{gate_id}` — awaiting decision."
        try:
            msg = await self.bot._send_message(
                brief,
                project_id=str(project_id),
                embed=embed,
                view=view,
            )
        except Exception:
            logger.exception("gate.created: failed to post embed for %s", gate_id)
            return
        if msg is None:
            # ``_send_message`` routes through ``_safe_api_call(critical=True)``
            # which returns ``None`` under rate-guard HALT.  Log a WARNING so
            # dropped gate prompts are visible in the operator log — otherwise
            # a HALT would silently swallow user-facing approval requests.
            logger.warning(
                "gate.created: post for %s returned no message "
                "(channel missing or rate-guard drop) — gate will not be interactive",
                gate_id,
            )
            return
        self._gate_messages[gid] = msg

    async def _on_gate_resolved(self, data: dict) -> None:
        """Edit the posted gate message to show the resolution, disable buttons."""
        gate_id = data.get("gate_id")
        if not gate_id:
            return
        msg = self._gate_messages.pop(str(gate_id), None)
        if msg is None:
            return
        resolved_by = str(data.get("resolved_by") or "unknown")
        resolution = str(data.get("resolution") or "").strip() or "resolved"
        unblocked = data.get("unblocked_task_ids") or []
        try:
            import discord

            embed = discord.Embed(
                title=f"✅ Gate resolved — {resolution}",
                description=f"Resolved by `{resolved_by}`.",
                color=discord.Color.green(),
            )
            if unblocked:
                shown = ", ".join(f"`{t}`" for t in unblocked[:10])
                if len(unblocked) > 10:
                    shown += f" (+{len(unblocked) - 10} more)"
                embed.add_field(name="Unblocked tasks", value=shown, inline=False)
            await self.bot._safe_api_call(
                msg.edit(embed=embed, view=None),
                critical=False,
                context=f"gate.resolved edit {gate_id}",
            )
        except Exception:
            logger.exception("gate.resolved: failed to edit message for %s", gate_id)
