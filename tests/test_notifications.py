"""Tests for the event-driven notification abstraction layer.

Covers notification event models/builders, the platform-neutral text formatters
in ``render``, and EventBus unsubscribe behavior.
"""

from __future__ import annotations

import pytest

from src.api.models.agent import AgentSummary
from src.api.models.task import TaskDetail
from src.event_bus import EventBus
from src.models import (
    Agent,
    AgentState,
    RepoSourceType,
    Task,
    TaskStatus,
    TaskType,
    Workspace,
    WorkspaceAgent,
)
from src.notifications.builder import build_agent_summary, build_task_detail
from src.notifications.events import (
    AgentQuestionEvent,
    BudgetWarningEvent,
    ChainStuckEvent,
    MergeConflictEvent,
    NotifyEvent,
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
from src.notifications.render import (
    classify_error,
    format_failed_blocked_report,
    format_task_started,
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
        id="worker-abc123",
        profile_id="coder",
        settings={"name": "my-workspace", "profile_id": "coder"},
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
            integration_mode="pull_request",
        )
        detail = build_task_detail(task)
        assert detail.pr_url == "http://github.com/pr/1"
        assert detail.parent_task_id == "parent-1"
        assert detail.is_plan_subtask is True
        assert detail.profile_id == "fast-profile"
        assert detail.integration_mode == "pull_request"

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

    def test_build_agent_summary_preserves_global_identity_and_settings(self):
        agent = Agent(
            id="worker-a",
            name="Ada",
            profile_id="coder",
            state=AgentState.BUSY,
            current_task_id="task-a",
            harness="codex",
            model="configured-next-model",
            intelligence_class="deep",
            enabled=False,
        )
        summary = build_agent_summary(agent)
        assert summary.id == "worker-a"
        assert summary.workspace_id is None
        assert summary.project_id is None
        assert summary.profile_id == "coder"
        assert summary.settings.model == "configured-next-model"
        assert summary.settings.harness == "codex"
        assert summary.settings.intelligence_class == "deep"
        assert summary.settings.enabled is False
        assert summary.current_task_id == "task-a"
        # A notification without a session snapshot must not report settings
        # as the model that is already running.
        assert summary.model is None

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
# Platform-neutral text formatters (src/notifications/render.py)
# ---------------------------------------------------------------------------


class TestNotificationRender:
    """The text formatters that outlived the retired Discord notification path."""

    def test_format_task_started(self):
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Add feature",
            description="D",
            branch_name="feat/add-feature",
        )
        agent = Agent(id="a-1", name="claude-1", profile_id="claude")
        result = format_task_started(task, agent)
        assert "Task Started" in result
        assert "t-1" in result
        assert "Add feature" in result
        assert "p-1" in result
        assert "claude-1" in result
        assert "feat/add-feature" in result
        assert "IN_PROGRESS" in result

    def test_format_task_started_no_branch(self):
        task = Task(id="t-2", project_id="p-1", title="Quick fix", description="D")
        agent = Agent(id="a-1", name="claude-2", profile_id="claude")
        result = format_task_started(task, agent)
        assert "Task Started" in result
        assert "t-2" in result
        assert "claude-2" in result
        assert "Branch" not in result

    def test_format_task_started_with_workspace(self):
        task = Task(
            id="t-1",
            project_id="p-1",
            title="Add feature",
            description="D",
            branch_name="feat/add-feature",
        )
        agent = Agent(id="a-1", name="claude-1", profile_id="claude")
        ws = Workspace(
            id="ws-1",
            project_id="p-1",
            workspace_path="/home/user/workspaces/ws-1",
            source_type=RepoSourceType.CLONE,
            name="workspace-alpha",
        )
        result = format_task_started(task, agent, workspace=ws)
        assert "workspace-alpha" in result
        assert "Workspace" in result

    def test_format_task_started_with_workspace_no_name(self):
        task = Task(id="t-1", project_id="p-1", title="Add feature", description="D")
        agent = Agent(id="a-1", name="claude-1", profile_id="claude")
        ws = Workspace(
            id="ws-1",
            project_id="p-1",
            workspace_path="/home/user/workspaces/ws-1",
            source_type=RepoSourceType.CLONE,
        )
        result = format_task_started(task, agent, workspace=ws)
        assert "/home/user/workspaces/ws-1" in result
        assert "Workspace" in result

    def test_format_failed_blocked_report_both(self):
        failed = [
            Task(
                id="t-1",
                project_id="p-1",
                title="Broken build",
                description="D",
                retry_count=2,
                max_retries=3,
                status=TaskStatus.FAILED,
            ),
        ]
        blocked = [
            Task(
                id="t-2",
                project_id="p-1",
                title="Stuck deploy",
                description="D",
                status=TaskStatus.BLOCKED,
            ),
        ]
        result = format_failed_blocked_report(failed, blocked)
        assert "Attention Required" in result
        assert "2 tasks" in result
        assert "Failed (1)" in result
        assert "Blocked (1)" in result
        assert "t-1" in result
        assert "t-2" in result
        assert "Broken build" in result
        assert "Stuck deploy" in result
        assert "2/3" in result  # retry count
        assert "/restart-task" in result

    def test_format_failed_blocked_report_failed_only(self):
        failed = [
            Task(
                id="t-1",
                project_id="p-1",
                title="Broken",
                description="D",
                retry_count=1,
                max_retries=3,
                status=TaskStatus.FAILED,
            ),
        ]
        result = format_failed_blocked_report(failed, [])
        assert "Attention Required" in result
        assert "1 task" in result
        assert "Failed (1)" in result
        assert "Blocked" not in result.split("Failed")[0]  # no blocked section header

    def test_format_failed_blocked_report_blocked_only(self):
        blocked = [
            Task(
                id="t-1",
                project_id="p-1",
                title="Stuck",
                description="D",
                status=TaskStatus.BLOCKED,
            ),
        ]
        result = format_failed_blocked_report([], blocked)
        assert "Attention Required" in result
        assert "1 task" in result
        assert "Blocked (1)" in result

    def test_format_failed_blocked_report_truncates_long_lists(self):
        failed = [
            Task(
                id=f"t-{i}",
                project_id="p-1",
                title=f"Task {i}",
                description="D",
                status=TaskStatus.FAILED,
            )
            for i in range(15)
        ]
        result = format_failed_blocked_report(failed, [])
        assert "+5 more" in result
        # Should show first 10 tasks
        assert "t-9" in result
        # Should NOT show task 11+
        assert "t-10" not in result

    def test_classify_error_matches_a_known_pattern(self):
        assert classify_error("HTTP 429 rate_limit from provider") == (
            "Rate-limit",
            "The API rate limit was hit. The task will be retried automatically.",
        )

    def test_classify_error_falls_back_without_a_match(self):
        label, suggestion = classify_error("kaboom")
        assert label == "Unexpected error"
        assert "daemon.log" in suggestion

    def test_classify_error_without_a_message(self):
        assert classify_error(None) == ("Unknown error", "Check daemon logs for details.")
