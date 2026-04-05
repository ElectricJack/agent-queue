"""Tests for the event-driven notification abstraction layer.

Covers:
- NotifyEvent models: construction, serialization, defaults
- Builder helpers: build_task_detail, build_agent_summary
- DiscordNotificationHandler: event routing, thread lifecycle, shutdown
- EventBus unsubscribe: subscribe returns callable that removes handler
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.models.agent import AgentSummary
from src.api.models.task import TaskDetail
from src.event_bus import EventBus
from src.models import (
    AgentState,
    Task,
    TaskStatus,
    TaskType,
    WorkspaceAgent,
)
from src.notifications.builder import build_agent_summary, build_task_detail
from src.notifications.events import (
    AgentQuestionEvent,
    BudgetWarningEvent,
    ChainStuckEvent,
    MergeConflictEvent,
    NotifyEvent,
    PlanAwaitingApprovalEvent,
    PRCreatedEvent,
    PushFailedEvent,
    StuckDefinedTaskEvent,
    SystemOnlineEvent,
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_task(**overrides) -> Task:
    defaults = dict(
        id="test-task",
        project_id="test-project",
        title="Test Task",
        description="A test task",
        priority=3,
        status=TaskStatus.IN_PROGRESS,
        task_type=TaskType.FEATURE,
        assigned_agent_id="ws-abc123",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _make_agent(**overrides) -> WorkspaceAgent:
    defaults = dict(
        workspace_id="ws-abc123",
        project_id="test-project",
        workspace_name="my-workspace",
        state="busy",
        current_task_id="test-task",
        current_task_title="Test Task",
    )
    defaults.update(overrides)
    return WorkspaceAgent(**defaults)


def _make_task_detail(**overrides) -> TaskDetail:
    defaults = dict(
        id="test-task",
        project_id="test-project",
        title="Test Task",
        description="A test task",
        status="IN_PROGRESS",
        priority=3,
        assigned_agent="ws-abc123",
        task_type="feature",
    )
    defaults.update(overrides)
    return TaskDetail(**defaults)


def _make_agent_summary(**overrides) -> AgentSummary:
    defaults = dict(
        workspace_id="ws-abc123",
        project_id="test-project",
        name="my-workspace",
        state="busy",
        current_task_id="test-task",
        current_task_title="Test Task",
    )
    defaults.update(overrides)
    return AgentSummary(**defaults)


# ---------------------------------------------------------------------------
# NotifyEvent models
# ---------------------------------------------------------------------------


class TestNotifyEventModels:
    """Event model construction, defaults, and serialization."""

    def test_base_event_defaults(self):
        e = NotifyEvent(event_type="notify.test")
        assert e.event_type == "notify.test"
        assert e.severity == "info"
        assert e.category == "system"
        assert e.project_id is None

    def test_task_started_event(self):
        td = _make_task_detail()
        ag = _make_agent_summary()
        e = TaskStartedEvent(task=td, agent=ag, project_id="proj")
        assert e.event_type == "notify.task_started"
        assert e.category == "task_lifecycle"
        assert e.task.id == "test-task"
        assert e.agent.workspace_id == "ws-abc123"
        assert e.is_reopened is False

    def test_task_completed_event(self):
        td = _make_task_detail()
        ag = _make_agent_summary()
        e = TaskCompletedEvent(
            task=td,
            agent=ag,
            summary="All done",
            files_changed=["src/foo.py"],
            tokens_used=5000,
        )
        assert e.event_type == "notify.task_completed"
        assert e.summary == "All done"
        assert e.files_changed == ["src/foo.py"]
        assert e.tokens_used == 5000

    def test_task_failed_event_severity(self):
        e = TaskFailedEvent(
            task=_make_task_detail(),
            agent=_make_agent_summary(),
            error_label="timeout",
            error_detail="exceeded 300s",
            retry_count=1,
            max_retries=3,
        )
        assert e.severity == "error"
        assert e.retry_count == 1

    def test_task_blocked_event_severity(self):
        e = TaskBlockedEvent(task=_make_task_detail(), last_error="max retries")
        assert e.severity == "critical"

    def test_agent_question_event(self):
        e = AgentQuestionEvent(
            task=_make_task_detail(),
            agent=_make_agent_summary(),
            question="What database?",
        )
        assert e.category == "interaction"
        assert e.question == "What database?"

    def test_plan_awaiting_approval_event(self):
        e = PlanAwaitingApprovalEvent(
            task=_make_task_detail(),
            subtasks=[{"title": "step 1"}],
            plan_url="http://example.com/plan",
        )
        assert e.category == "interaction"
        assert len(e.subtasks) == 1

    def test_vcs_events(self):
        pr = PRCreatedEvent(task=_make_task_detail(), pr_url="http://github.com/pr/1")
        assert pr.category == "vcs"

        mc = MergeConflictEvent(task=_make_task_detail(), branch="feature", target_branch="main")
        assert mc.severity == "error"

        pf = PushFailedEvent(task=_make_task_detail(), branch="feature", error_detail="rejected")
        assert pf.severity == "warning"

    def test_budget_warning_event(self):
        e = BudgetWarningEvent(project_name="myproject", usage=80000, limit=100000, percentage=80.0)
        assert e.category == "budget"
        assert e.percentage == 80.0

    def test_chain_stuck_event(self):
        e = ChainStuckEvent(
            blocked_task=_make_task_detail(),
            stuck_task_ids=["t1", "t2"],
            stuck_task_titles=["Task 1", "Task 2"],
        )
        assert len(e.stuck_task_ids) == 2

    def test_thread_events(self):
        o = TaskThreadOpenEvent(task_id="t1", thread_name="t1 | Work", initial_message="go")
        assert o.category == "task_stream"

        m = TaskMessageEvent(task_id="t1", message="doing stuff", message_type="agent_output")
        assert m.message_type == "agent_output"

        c = TaskThreadCloseEvent(task_id="t1", final_status="completed")
        assert c.final_status == "completed"

    def test_text_notify_event(self):
        e = TextNotifyEvent(message="hello", project_id="proj")
        assert e.event_type == "notify.text"
        assert e.message == "hello"

    def test_serialization_roundtrip(self):
        """Events survive model_dump → reconstruction."""
        td = _make_task_detail()
        ag = _make_agent_summary()
        original = TaskStartedEvent(task=td, agent=ag, project_id="proj", workspace_path="/tmp/ws")
        data = original.model_dump(mode="json")
        restored = TaskStartedEvent(**data)
        assert restored.task.id == original.task.id
        assert restored.agent.workspace_id == original.agent.workspace_id
        assert restored.workspace_path == "/tmp/ws"

    def test_all_events_have_event_type_prefix(self):
        """Every concrete event type has a default event_type starting with notify."""
        event_classes = [
            TaskStartedEvent,
            TaskCompletedEvent,
            TaskFailedEvent,
            TaskBlockedEvent,
            TaskStoppedEvent,
            AgentQuestionEvent,
            PlanAwaitingApprovalEvent,
            PRCreatedEvent,
            MergeConflictEvent,
            PushFailedEvent,
            BudgetWarningEvent,
            ChainStuckEvent,
            StuckDefinedTaskEvent,
            SystemOnlineEvent,
            TaskThreadOpenEvent,
            TaskMessageEvent,
            TaskThreadCloseEvent,
            TextNotifyEvent,
        ]
        for cls in event_classes:
            default_type = cls.model_fields["event_type"].default
            assert default_type.startswith("notify."), (
                f"{cls.__name__}.event_type={default_type!r} doesn't start with 'notify.'"
            )


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


class TestBuilders:
    """build_task_detail and build_agent_summary conversions."""

    def test_build_task_detail_basic(self):
        task = _make_task()
        detail = build_task_detail(task)
        assert isinstance(detail, TaskDetail)
        assert detail.id == "test-task"
        assert detail.project_id == "test-project"
        assert detail.title == "Test Task"
        assert detail.status == "IN_PROGRESS"
        assert detail.priority == 3
        assert detail.task_type == "feature"
        assert detail.assigned_agent == "ws-abc123"

    def test_build_task_detail_optional_fields(self):
        task = _make_task(
            pr_url="http://github.com/pr/1",
            parent_task_id="parent-1",
            is_plan_subtask=True,
            profile_id="fast-profile",
            requires_approval=True,
            auto_approve_plan=True,
        )
        detail = build_task_detail(task)
        assert detail.pr_url == "http://github.com/pr/1"
        assert detail.parent_task_id == "parent-1"
        assert detail.is_plan_subtask is True
        assert detail.profile_id == "fast-profile"
        assert detail.requires_approval is True
        assert detail.auto_approve_plan is True

    def test_build_task_detail_none_description(self):
        task = _make_task(description=None)
        detail = build_task_detail(task)
        assert detail.description == ""

    def test_build_task_detail_none_task_type(self):
        task = _make_task(task_type=None)
        detail = build_task_detail(task)
        assert detail.task_type is None

    def test_build_agent_summary_workspace_agent(self):
        agent = _make_agent()
        summary = build_agent_summary(agent)
        assert isinstance(summary, AgentSummary)
        assert summary.workspace_id == "ws-abc123"
        assert summary.project_id == "test-project"
        assert summary.name == "my-workspace"
        assert summary.state == "busy"
        assert summary.current_task_id == "test-task"

    def test_build_agent_summary_no_workspace_name(self):
        """Falls back to workspace_id when name is None."""
        agent = _make_agent(workspace_name=None)
        summary = build_agent_summary(agent)
        assert summary.name == "ws-abc123"

    def test_build_agent_summary_enum_state(self):
        """Handles AgentState enum values."""
        agent = _make_agent(state=AgentState.IDLE)
        summary = build_agent_summary(agent)
        assert summary.state == AgentState.IDLE.value


# ---------------------------------------------------------------------------
# EventBus unsubscribe
# ---------------------------------------------------------------------------


class TestEventBusUnsubscribe:
    """EventBus.subscribe() returns a working unsubscribe callable."""

    @pytest.mark.asyncio
    async def test_subscribe_returns_unsubscribe(self):
        bus = EventBus()
        calls = []

        async def handler(data):
            calls.append(data)

        unsub = bus.subscribe("test.event", handler)
        await bus.emit("test.event", {"x": 1})
        assert len(calls) == 1

        unsub()
        await bus.emit("test.event", {"x": 2})
        assert len(calls) == 1  # handler was removed

    @pytest.mark.asyncio
    async def test_double_unsubscribe_is_safe(self):
        bus = EventBus()
        unsub = bus.subscribe("test.event", lambda d: None)
        unsub()
        unsub()  # should not raise

    @pytest.mark.asyncio
    async def test_multiple_handlers_independent(self):
        bus = EventBus()
        calls_a, calls_b = [], []

        async def handler_a(data):
            calls_a.append(1)

        async def handler_b(data):
            calls_b.append(1)

        unsub_a = bus.subscribe("evt", handler_a)
        _unsub_b = bus.subscribe("evt", handler_b)

        unsub_a()
        await bus.emit("evt")
        assert len(calls_a) == 0
        assert len(calls_b) == 1


# ---------------------------------------------------------------------------
# DiscordNotificationHandler
# ---------------------------------------------------------------------------


def _make_mock_bot():
    """Create a mock bot with the methods DiscordNotificationHandler calls."""
    bot = AsyncMock()
    bot._send_message = AsyncMock(return_value=MagicMock())
    bot._create_task_thread = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
    bot.edit_thread_root_message = AsyncMock()
    bot.get_thread_last_message_url = AsyncMock(return_value="http://discord.com/thread/123")
    bot.orchestrator = MagicMock()
    bot.orchestrator._task_started_messages = {}
    bot.agent = MagicMock()
    bot.agent.handler = MagicMock()
    return bot


class TestDiscordNotificationHandler:
    """DiscordNotificationHandler event routing and thread management."""

    @pytest.mark.asyncio
    async def test_task_started_sends_message(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        event = TaskStartedEvent(
            task=_make_task_detail(),
            agent=_make_agent_summary(),
            project_id="test-project",
        )
        await bus.emit("notify.task_started", event.model_dump(mode="json"))
        bot._send_message.assert_called_once()

        handler.shutdown()

    @pytest.mark.asyncio
    async def test_task_started_suppressed_on_reopen(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        event = TaskStartedEvent(
            task=_make_task_detail(),
            agent=_make_agent_summary(),
            project_id="test-project",
            is_reopened=True,
        )
        await bus.emit("notify.task_started", event.model_dump(mode="json"))
        bot._send_message.assert_not_called()

        handler.shutdown()

    @pytest.mark.asyncio
    async def test_task_completed_to_channel(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        event = TaskCompletedEvent(
            task=_make_task_detail(),
            agent=_make_agent_summary(),
            summary="Done",
            project_id="test-project",
        )
        await bus.emit("notify.task_completed", event.model_dump(mode="json"))
        bot._send_message.assert_called_once()
        call_args = bot._send_message.call_args
        assert "completed" in call_args[0][0].lower() or "✅" in call_args[0][0]

        handler.shutdown()

    @pytest.mark.asyncio
    async def test_task_completed_routes_to_thread(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        send_thread = AsyncMock()
        notify_main = AsyncMock()
        handler._task_threads["test-task"] = (send_thread, notify_main)

        event = TaskCompletedEvent(
            task=_make_task_detail(),
            agent=_make_agent_summary(),
            project_id="test-project",
        )
        await bus.emit("notify.task_completed", event.model_dump(mode="json"))

        send_thread.assert_called_once()
        notify_main.assert_called_once()
        bot._send_message.assert_not_called()

        handler.shutdown()

    @pytest.mark.asyncio
    async def test_text_notify(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        event = TextNotifyEvent(message="hello world", project_id="proj")
        await bus.emit("notify.text", event.model_dump(mode="json"))

        bot._send_message.assert_called_once_with("hello world", project_id="proj")

        handler.shutdown()

    @pytest.mark.asyncio
    async def test_system_online(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        event = SystemOnlineEvent()
        await bus.emit("notify.system_online", event.model_dump(mode="json"))

        bot._send_message.assert_called_once()

        handler.shutdown()

    # -- Thread lifecycle ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_thread_open_stores_callbacks(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        event = TaskThreadOpenEvent(
            task_id="t1",
            thread_name="t1 | Work",
            initial_message="Starting",
            project_id="proj",
        )
        await bus.emit("notify.task_thread_open", event.model_dump(mode="json"))

        assert "t1" in handler._task_threads
        bot._create_task_thread.assert_called_once()

        handler.shutdown()

    @pytest.mark.asyncio
    async def test_thread_message_routes_to_thread(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        send_thread = AsyncMock()
        notify_main = AsyncMock()
        handler._task_threads["t1"] = (send_thread, notify_main)

        event = TaskMessageEvent(task_id="t1", message="working...", message_type="agent_output")
        await bus.emit("notify.task_message", event.model_dump(mode="json"))

        send_thread.assert_called_once_with("working...")
        notify_main.assert_not_called()

        handler.shutdown()

    @pytest.mark.asyncio
    async def test_thread_message_brief_routes_to_main(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        send_thread = AsyncMock()
        notify_main = AsyncMock()
        handler._task_threads["t1"] = (send_thread, notify_main)

        event = TaskMessageEvent(task_id="t1", message="summary", message_type="brief")
        await bus.emit("notify.task_message", event.model_dump(mode="json"))

        send_thread.assert_not_called()
        notify_main.assert_called_once_with("summary")

        handler.shutdown()

    @pytest.mark.asyncio
    async def test_thread_message_fallback_to_channel(self):
        """Without a thread, messages go to the channel."""
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        event = TaskMessageEvent(task_id="t1", message="no thread", project_id="proj")
        await bus.emit("notify.task_message", event.model_dump(mode="json"))

        bot._send_message.assert_called_once_with("no thread", project_id="proj")

        handler.shutdown()

    @pytest.mark.asyncio
    async def test_thread_close_cleans_up(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        handler._task_threads["t1"] = (AsyncMock(), AsyncMock())

        event = TaskThreadCloseEvent(task_id="t1", final_status="completed", final_message="Done")
        await bus.emit("notify.task_thread_close", event.model_dump(mode="json"))

        assert "t1" not in handler._task_threads
        bot.edit_thread_root_message.assert_called_once_with("t1", "Done", None)

        handler.shutdown()

    # -- Shutdown -----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_shutdown_unsubscribes_all(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        # Verify handlers are registered
        assert len(bus._handlers["notify.task_started"]) > 0

        handler.shutdown()

        # Verify all handlers removed
        for event_type in list(bus._handlers.keys()):
            if event_type.startswith("notify."):
                assert len(bus._handlers[event_type]) == 0, (
                    f"Handler still registered for {event_type}"
                )

    @pytest.mark.asyncio
    async def test_shutdown_clears_threads(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        handler._task_threads["t1"] = (AsyncMock(), AsyncMock())
        handler._task_threads["t2"] = (AsyncMock(), AsyncMock())

        handler.shutdown()
        assert len(handler._task_threads) == 0

    # -- Full roundtrip -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_roundtrip_emit_to_handler(self):
        """End-to-end: orchestrator emits event → handler receives and acts."""
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        # Simulate orchestrator emitting a text notification
        event = TextNotifyEvent(message="task queued", project_id="proj")
        await bus.emit("notify.text", event.model_dump(mode="json"))

        bot._send_message.assert_called_once_with("task queued", project_id="proj")

        # Emit thread open + message + close cycle
        bot.reset_mock()
        await bus.emit(
            "notify.task_thread_open",
            TaskThreadOpenEvent(
                task_id="t1",
                thread_name="t1 | Work",
                initial_message="go",
                project_id="proj",
            ).model_dump(mode="json"),
        )
        assert "t1" in handler._task_threads

        await bus.emit(
            "notify.task_message",
            TaskMessageEvent(
                task_id="t1",
                message="step 1 done",
            ).model_dump(mode="json"),
        )

        await bus.emit(
            "notify.task_thread_close",
            TaskThreadCloseEvent(
                task_id="t1",
                final_status="completed",
                final_message="All done",
            ).model_dump(mode="json"),
        )
        assert "t1" not in handler._task_threads

        handler.shutdown()
