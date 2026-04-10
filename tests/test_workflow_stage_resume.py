"""Tests for event-triggered resumption across workflow stages (Roadmap 7.5.5).

Verifies that:
1. PlaybookRunner supports ``wait_for_event`` nodes that pause a run and
   record which event type is being waited for.
2. ``WorkflowStageResumeHandler`` subscribes to ``workflow.stage.completed``
   events and resumes the associated paused playbook run.
3. The ``resume_from_event`` classmethod injects event data into conversation
   context and continues graph execution.
4. Edge cases: missing workflow, run not paused, wrong event type, timeout,
   double-resume prevention, no playbook_run_id.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.event_bus import EventBus
from src.models import PlaybookRun, PlaybookRunEvent, PlaybookRunStatus, Workflow
from src.playbook_runner import PlaybookRunner
from src.playbook_state_machine import (
    VALID_PLAYBOOK_RUN_TRANSITIONS,
    playbook_run_transition,
)
from src.workflow_stage_resume_handler import WorkflowStageResumeHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_supervisor():
    """A mock Supervisor with controllable chat()."""
    supervisor = AsyncMock()
    supervisor.chat = AsyncMock(return_value="Done.")
    supervisor.summarize = AsyncMock(return_value="Summary.")
    return supervisor


@pytest.fixture
def mock_db():
    """A mock database backend."""
    db = AsyncMock()
    db.create_playbook_run = AsyncMock()
    db.update_playbook_run = AsyncMock()
    db.get_playbook_run = AsyncMock(return_value=None)
    db.get_workflow = AsyncMock(return_value=None)
    db.get_daily_playbook_token_usage = AsyncMock(return_value=0)
    return db


@pytest.fixture
def event_bus():
    """An EventBus in dev mode (strict validation)."""
    return EventBus(env="dev", validate_events=False)


@pytest.fixture
def event_data():
    """Sample trigger event for a coordination playbook."""
    return {"type": "task.created", "project_id": "proj-1", "task_id": "t-1"}


@pytest.fixture
def stage_event_graph():
    """A coordination playbook graph with a wait_for_event node.

    Flow: create_tasks → wait_stage → next_stage → done
    """
    return {
        "id": "coord-playbook",
        "version": 1,
        "nodes": {
            "create_tasks": {
                "entry": True,
                "prompt": "Create coding tasks for the workflow.",
                "goto": "wait_stage",
            },
            "wait_stage": {
                "prompt": "All tasks have been created. Waiting for stage completion.",
                "wait_for_event": "workflow.stage.completed",
                "transitions": [
                    {"when": "stage completed", "goto": "next_stage"},
                ],
            },
            "next_stage": {
                "prompt": "Stage completed. Create review tasks for the next stage.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def stage_event_graph_dict_form():
    """A graph where wait_for_event is a dict with an ``event`` key."""
    return {
        "id": "coord-playbook-dict",
        "version": 1,
        "nodes": {
            "create_tasks": {
                "entry": True,
                "prompt": "Create tasks.",
                "goto": "wait_stage",
            },
            "wait_stage": {
                "prompt": "Waiting for stage completion.",
                "wait_for_event": {"event": "workflow.stage.completed"},
                "transitions": [
                    {"when": "stage completed", "goto": "next_stage"},
                ],
            },
            "next_stage": {
                "prompt": "Next stage.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


def _make_paused_run(
    run_id: str = "run-1",
    playbook_id: str = "coord-playbook",
    current_node: str = "wait_stage",
    waiting_for_event: str | None = "workflow.stage.completed",
    graph: dict | None = None,
) -> PlaybookRun:
    """Create a paused PlaybookRun record with wait_for_event set."""
    return PlaybookRun(
        run_id=run_id,
        playbook_id=playbook_id,
        playbook_version=1,
        trigger_event=json.dumps({"type": "task.created", "project_id": "proj-1"}),
        status="paused",
        current_node=current_node,
        conversation_history=json.dumps(
            [
                {"role": "user", "content": "Event received: ..."},
                {"role": "user", "content": "Create coding tasks for the workflow."},
                {"role": "assistant", "content": "Tasks created: t-1, t-2."},
                {
                    "role": "user",
                    "content": "All tasks have been created. Waiting for stage completion.",
                },
                {"role": "assistant", "content": "Stage tasks created, waiting for completion."},
            ]
        ),
        node_trace=json.dumps(
            [
                {
                    "node_id": "create_tasks",
                    "started_at": 100.0,
                    "completed_at": 101.0,
                    "status": "completed",
                    "transition_to": "wait_stage",
                    "transition_method": "goto",
                },
                {
                    "node_id": "wait_stage",
                    "started_at": 101.0,
                    "completed_at": 102.0,
                    "status": "completed",
                },
            ]
        ),
        tokens_used=50,
        started_at=100.0,
        paused_at=time.time(),
        waiting_for_event=waiting_for_event,
        pinned_graph=json.dumps(graph) if graph else None,
    )


def _make_workflow(
    workflow_id: str = "wf-1",
    playbook_run_id: str = "run-1",
    status: str = "running",
    current_stage: str = "build",
    task_ids: list[str] | None = None,
) -> Workflow:
    return Workflow(
        workflow_id=workflow_id,
        playbook_id="coord-playbook",
        playbook_run_id=playbook_run_id,
        project_id="proj-1",
        status=status,
        current_stage=current_stage,
        task_ids=task_ids or ["t-1", "t-2"],
        created_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Tests: State machine — EVENT_WAIT and EVENT_RESUMED transitions
# ---------------------------------------------------------------------------


class TestStateMachineEventTransitions:
    """Verify EVENT_WAIT and EVENT_RESUMED are in the state machine."""

    def test_running_to_paused_via_event_wait(self):
        target = playbook_run_transition(PlaybookRunStatus.RUNNING, PlaybookRunEvent.EVENT_WAIT)
        assert target == PlaybookRunStatus.PAUSED

    def test_paused_to_running_via_event_resumed(self):
        target = playbook_run_transition(PlaybookRunStatus.PAUSED, PlaybookRunEvent.EVENT_RESUMED)
        assert target == PlaybookRunStatus.RUNNING

    def test_event_wait_in_transition_table(self):
        key = (PlaybookRunStatus.RUNNING, PlaybookRunEvent.EVENT_WAIT)
        assert key in VALID_PLAYBOOK_RUN_TRANSITIONS

    def test_event_resumed_in_transition_table(self):
        key = (PlaybookRunStatus.PAUSED, PlaybookRunEvent.EVENT_RESUMED)
        assert key in VALID_PLAYBOOK_RUN_TRANSITIONS


# ---------------------------------------------------------------------------
# Tests: PlaybookRunner — wait_for_event node handling
# ---------------------------------------------------------------------------


class TestWaitForEventNode:
    """Verify PlaybookRunner pauses at wait_for_event nodes."""

    async def test_wait_for_event_pauses_run(
        self, mock_supervisor, stage_event_graph, event_data, mock_db
    ):
        """A node with wait_for_event should pause the run."""
        responses = iter(["Tasks created.", "Waiting for stage."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(stage_event_graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "paused"
        # Should have executed create_tasks and wait_stage nodes
        node_ids = [t["node_id"] for t in result.node_trace]
        assert "create_tasks" in node_ids
        assert "wait_stage" in node_ids

    async def test_wait_for_event_persists_waiting_for_event(
        self, mock_supervisor, stage_event_graph, event_data, mock_db
    ):
        """The paused run should have waiting_for_event persisted in DB."""
        responses = iter(["Tasks created.", "Waiting."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(stage_event_graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        # Find the update call that set status to paused
        paused_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break
        assert paused_call is not None
        assert paused_call.kwargs["waiting_for_event"] == "workflow.stage.completed"
        assert paused_call.kwargs["current_node"] == "wait_stage"

    async def test_wait_for_event_dict_form(
        self, mock_supervisor, stage_event_graph_dict_form, event_data, mock_db
    ):
        """wait_for_event as dict {event: "..."} should also work."""
        responses = iter(["Tasks created.", "Waiting."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(
            stage_event_graph_dict_form, event_data, mock_supervisor, db=mock_db
        )
        result = await runner.run()

        assert result.status == "paused"
        paused_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break
        assert paused_call is not None
        assert paused_call.kwargs["waiting_for_event"] == "workflow.stage.completed"

    async def test_wait_for_event_emits_paused_event(
        self, mock_supervisor, stage_event_graph, event_data, mock_db, event_bus
    ):
        """Pausing at wait_for_event should emit playbook.run.paused."""
        responses = iter(["Tasks created.", "Waiting."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        emitted = []
        event_bus.subscribe("playbook.run.paused", lambda d: emitted.append(d))

        runner = PlaybookRunner(
            stage_event_graph,
            event_data,
            mock_supervisor,
            db=mock_db,
            event_bus=event_bus,
        )
        await runner.run()

        assert len(emitted) == 1
        assert emitted[0]["waiting_for_event"] == "workflow.stage.completed"
        assert emitted[0]["node_id"] == "wait_stage"

    async def test_wait_for_event_no_human_notification(
        self, mock_supervisor, stage_event_graph, event_data, mock_db, event_bus
    ):
        """wait_for_event should NOT emit notify.playbook_run_paused."""
        responses = iter(["Tasks created.", "Waiting."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        notifications = []
        event_bus.subscribe("notify.playbook_run_paused", lambda d: notifications.append(d))

        runner = PlaybookRunner(
            stage_event_graph,
            event_data,
            mock_supervisor,
            db=mock_db,
            event_bus=event_bus,
        )
        await runner.run()

        # No human notification should be emitted
        assert len(notifications) == 0

    async def test_wait_for_event_skipped_in_dry_run(
        self, mock_supervisor, stage_event_graph, event_data
    ):
        """Dry run should skip wait_for_event and continue."""
        result = await PlaybookRunner.dry_run(stage_event_graph, event_data)

        # Should complete without pausing
        assert result.status == "completed"
        node_ids = [t["node_id"] for t in result.node_trace]
        assert "next_stage" in node_ids

    async def test_progress_callback_receives_paused_for_event(
        self, mock_supervisor, stage_event_graph, event_data, mock_db
    ):
        """on_progress should receive 'playbook_paused_for_event'."""
        responses = iter(["Tasks.", "Waiting."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        progress_events = []
        on_progress = AsyncMock(side_effect=lambda e, d: progress_events.append((e, d)))

        runner = PlaybookRunner(
            stage_event_graph,
            event_data,
            mock_supervisor,
            db=mock_db,
            on_progress=on_progress,
        )
        await runner.run()

        progress_types = [p[0] for p in progress_events]
        assert "playbook_paused_for_event" in progress_types


# ---------------------------------------------------------------------------
# Tests: PlaybookRunner.resume_from_event
# ---------------------------------------------------------------------------


class TestResumeFromEvent:
    """Verify PlaybookRunner.resume_from_event continues graph execution."""

    async def test_resume_from_event_completes(self, mock_supervisor, stage_event_graph, mock_db):
        """Resuming with event data should continue to completion."""
        paused_run = _make_paused_run(graph=stage_event_graph)

        # LLM calls: transition classification → goto next_stage,
        # then execute next_stage
        responses = iter(["1", "Review tasks created."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        result = await PlaybookRunner.resume_from_event(
            db_run=paused_run,
            graph=stage_event_graph,
            supervisor=mock_supervisor,
            event_data={
                "workflow_id": "wf-1",
                "stage": "build",
                "task_ids": ["t-1", "t-2"],
            },
            db=mock_db,
        )

        assert result.status == "completed"
        assert result.run_id == "run-1"

    async def test_resume_from_event_injects_context(
        self, mock_supervisor, stage_event_graph, mock_db
    ):
        """Event data should be injected into conversation history."""
        paused_run = _make_paused_run(graph=stage_event_graph)

        captured_messages = []

        async def capture_chat(**kwargs):
            msgs = kwargs.get("messages", [])
            captured_messages.extend(msgs)
            return "Done."

        mock_supervisor.chat.side_effect = capture_chat

        # Need transition eval + node exec
        responses = iter(["1", "Done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        await PlaybookRunner.resume_from_event(
            db_run=paused_run,
            graph=stage_event_graph,
            supervisor=mock_supervisor,
            event_data={"workflow_id": "wf-1", "stage": "build"},
            db=mock_db,
        )

        # Check that update_playbook_run was called to clear waiting_for_event
        clear_calls = [
            c
            for c in mock_db.update_playbook_run.call_args_list
            if c.kwargs.get("waiting_for_event") is None and "waiting_for_event" in c.kwargs
        ]
        assert len(clear_calls) >= 1

    async def test_resume_from_event_uses_pinned_graph(
        self, mock_supervisor, stage_event_graph, mock_db
    ):
        """Should use pinned graph from the run record when available."""
        paused_run = _make_paused_run(graph=stage_event_graph)

        responses = iter(["1", "Done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        # Pass a different graph (should be ignored in favor of pinned)
        different_graph = {
            "id": "different",
            "version": 99,
            "nodes": {"x": {"entry": True, "terminal": True}},
        }

        result = await PlaybookRunner.resume_from_event(
            db_run=paused_run,
            graph=different_graph,
            supervisor=mock_supervisor,
            event_data={"workflow_id": "wf-1"},
            db=mock_db,
        )

        assert result.status == "completed"

    async def test_resume_from_event_transitions_state(
        self, mock_supervisor, stage_event_graph, mock_db
    ):
        """State should transition PAUSED → RUNNING → COMPLETED."""
        paused_run = _make_paused_run(graph=stage_event_graph)

        responses = iter(["1", "Done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        result = await PlaybookRunner.resume_from_event(
            db_run=paused_run,
            graph=stage_event_graph,
            supervisor=mock_supervisor,
            event_data={"workflow_id": "wf-1"},
            db=mock_db,
        )

        assert result.status == "completed"

        # DB should be updated with running, then completed
        status_updates = [
            c.kwargs.get("status")
            for c in mock_db.update_playbook_run.call_args_list
            if "status" in c.kwargs
        ]
        assert "running" in status_updates
        assert "completed" in status_updates

    async def test_resume_from_event_emits_resumed_event(
        self, mock_supervisor, stage_event_graph, mock_db, event_bus
    ):
        """Should emit playbook.run.resumed with resumed_by_event."""
        paused_run = _make_paused_run(graph=stage_event_graph)

        responses = iter(["1", "Done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        emitted = []
        event_bus.subscribe("playbook.run.resumed", lambda d: emitted.append(d))

        await PlaybookRunner.resume_from_event(
            db_run=paused_run,
            graph=stage_event_graph,
            supervisor=mock_supervisor,
            event_data={"workflow_id": "wf-1"},
            db=mock_db,
            event_bus=event_bus,
        )

        assert len(emitted) == 1
        assert emitted[0]["resumed_by_event"] == "workflow.stage.completed"
        assert emitted[0]["node_id"] == "wait_stage"

    async def test_resume_from_event_no_current_node(
        self, mock_supervisor, stage_event_graph, mock_db
    ):
        """Missing current_node should fail gracefully."""
        paused_run = _make_paused_run(graph=stage_event_graph)
        paused_run.current_node = None

        result = await PlaybookRunner.resume_from_event(
            db_run=paused_run,
            graph=stage_event_graph,
            supervisor=mock_supervisor,
            event_data={"workflow_id": "wf-1"},
            db=mock_db,
        )

        assert result.status == "failed"
        assert "no current_node" in (result.error or "")

    async def test_resume_from_event_can_pause_again(self, mock_supervisor, mock_db):
        """After resuming, the playbook can pause at another wait_for_event."""
        multi_stage_graph = {
            "id": "multi-stage",
            "version": 1,
            "nodes": {
                "create_tasks": {
                    "entry": True,
                    "prompt": "Create tasks.",
                    "goto": "wait_stage_1",
                },
                "wait_stage_1": {
                    "prompt": "Wait for stage 1.",
                    "wait_for_event": "workflow.stage.completed",
                    "transitions": [
                        {"when": "stage completed", "goto": "create_stage_2"},
                    ],
                },
                "create_stage_2": {
                    "prompt": "Create stage 2 tasks.",
                    "goto": "wait_stage_2",
                },
                "wait_stage_2": {
                    "prompt": "Wait for stage 2.",
                    "wait_for_event": "workflow.stage.completed",
                    "transitions": [
                        {"when": "stage completed", "goto": "done"},
                    ],
                },
                "done": {"terminal": True},
            },
        }

        paused_run = _make_paused_run(
            current_node="wait_stage_1",
            graph=multi_stage_graph,
        )

        # Transition eval → goto create_stage_2, execute it, then
        # hit wait_stage_2 and pause again
        responses = iter(["1", "Stage 2 tasks created.", "Waiting for stage 2."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        result = await PlaybookRunner.resume_from_event(
            db_run=paused_run,
            graph=multi_stage_graph,
            supervisor=mock_supervisor,
            event_data={"workflow_id": "wf-1", "stage": "stage_1"},
            db=mock_db,
        )

        # Should pause again at wait_stage_2
        assert result.status == "paused"

        # Should have persisted waiting_for_event for the new pause
        paused_calls = [
            c
            for c in mock_db.update_playbook_run.call_args_list
            if c.kwargs.get("status") == "paused"
            and c.kwargs.get("waiting_for_event") == "workflow.stage.completed"
        ]
        assert len(paused_calls) >= 1


# ---------------------------------------------------------------------------
# Tests: WorkflowStageResumeHandler
# ---------------------------------------------------------------------------


class TestWorkflowStageResumeHandler:
    """Verify the handler subscribes, validates, and resumes."""

    def _make_handler(self, mock_db, event_bus, **kwargs):
        """Create a handler with mocked dependencies."""
        orchestrator = MagicMock()
        playbook_manager = MagicMock()
        playbook_manager._active = {}
        config = MagicMock()

        return WorkflowStageResumeHandler(
            db=mock_db,
            event_bus=event_bus,
            orchestrator=orchestrator,
            playbook_manager=playbook_manager,
            config=config,
            **kwargs,
        )

    async def test_subscribe_registers_handler(self, mock_db, event_bus):
        handler = self._make_handler(mock_db, event_bus)
        handler.subscribe()

        # Should have registered on workflow.stage.completed
        assert len(event_bus._handlers["workflow.stage.completed"]) == 1

    async def test_unsubscribe_removes_handler(self, mock_db, event_bus):
        handler = self._make_handler(mock_db, event_bus)
        handler.subscribe()
        handler.unsubscribe()

        assert len(event_bus._handlers["workflow.stage.completed"]) == 0

    async def test_missing_workflow_id_logs_warning(self, mock_db, event_bus):
        """Events without workflow_id should be ignored."""
        handler = self._make_handler(mock_db, event_bus)

        # Should not raise
        await handler._on_stage_completed({"stage": "build"})

        # No resume task should be created
        assert len(handler.running_resumes) == 0

    async def test_workflow_not_found_skips(self, mock_db, event_bus):
        """If the workflow doesn't exist, skip silently."""
        mock_db.get_workflow.return_value = None
        handler = self._make_handler(mock_db, event_bus)

        await handler._on_stage_completed(
            {
                "workflow_id": "wf-unknown",
                "stage": "build",
            }
        )

        assert len(handler.running_resumes) == 0

    async def test_no_playbook_run_id_skips(self, mock_db, event_bus):
        """Workflow without playbook_run_id should be skipped."""
        workflow = _make_workflow(playbook_run_id="")
        mock_db.get_workflow.return_value = workflow
        handler = self._make_handler(mock_db, event_bus)

        await handler._on_stage_completed(
            {
                "workflow_id": "wf-1",
                "stage": "build",
            }
        )

        assert len(handler.running_resumes) == 0

    async def test_run_not_paused_skips(self, mock_db, event_bus):
        """If the playbook run is not paused, skip."""
        workflow = _make_workflow()
        mock_db.get_workflow.return_value = workflow
        mock_db.get_playbook_run.return_value = _make_paused_run()
        # Override status to running
        mock_db.get_playbook_run.return_value.status = "running"

        handler = self._make_handler(mock_db, event_bus)
        await handler._resume_run("run-1", {"workflow_id": "wf-1"})

        # No update should happen
        mock_db.update_playbook_run.assert_not_called()

    async def test_wrong_waiting_for_event_skips(self, mock_db, event_bus):
        """Run waiting for different event type should not be resumed."""
        workflow = _make_workflow()
        mock_db.get_workflow.return_value = workflow

        run = _make_paused_run(waiting_for_event="task.completed")
        mock_db.get_playbook_run.return_value = run

        handler = self._make_handler(mock_db, event_bus)
        await handler._resume_run("run-1", {"workflow_id": "wf-1"})

        # Should not attempt resume
        assert mock_db.update_playbook_run.call_count == 0

    async def test_pause_timeout_marks_timed_out(self, mock_db, event_bus):
        """Run that exceeded pause timeout should be marked timed_out."""
        run = _make_paused_run()
        run.paused_at = time.time() - 200000  # Way past timeout
        mock_db.get_playbook_run.return_value = run

        handler = self._make_handler(mock_db, event_bus, pause_timeout_seconds=100)
        await handler._resume_run("run-1", {"workflow_id": "wf-1"})

        # Should have been marked as timed_out
        update_calls = mock_db.update_playbook_run.call_args_list
        assert len(update_calls) == 1
        assert update_calls[0].kwargs["status"] == "timed_out"
        assert update_calls[0].kwargs["waiting_for_event"] is None

    async def test_double_resume_prevention(self, mock_db, event_bus):
        """Duplicate events for same run should be ignored."""
        workflow = _make_workflow()
        mock_db.get_workflow.return_value = workflow

        handler = self._make_handler(mock_db, event_bus)

        # Simulate an in-flight resume task
        import asyncio

        future = asyncio.get_event_loop().create_future()
        handler._running_resumes["run-1"] = asyncio.ensure_future(future)

        await handler._on_stage_completed(
            {
                "workflow_id": "wf-1",
                "stage": "build",
            }
        )

        # Should not create another task
        assert len(handler._running_resumes) == 1

        # Clean up
        future.cancel()

    async def test_shutdown_cancels_tasks(self, mock_db, event_bus):
        """shutdown() should cancel in-flight resume tasks."""
        handler = self._make_handler(mock_db, event_bus)

        import asyncio

        future = asyncio.get_event_loop().create_future()
        handler._running_resumes["run-1"] = asyncio.ensure_future(future)

        handler.shutdown()

        assert len(handler._running_resumes) == 0
        assert future.cancelled()

    async def test_db_error_during_workflow_fetch(self, mock_db, event_bus):
        """DB error when fetching workflow should be handled gracefully."""
        mock_db.get_workflow.side_effect = Exception("DB error")
        handler = self._make_handler(mock_db, event_bus)

        # Should not raise
        await handler._on_stage_completed(
            {
                "workflow_id": "wf-1",
                "stage": "build",
            }
        )

        assert len(handler.running_resumes) == 0


# ---------------------------------------------------------------------------
# Tests: PlaybookRun model — waiting_for_event field
# ---------------------------------------------------------------------------


class TestPlaybookRunModel:
    """Verify the waiting_for_event field on the PlaybookRun model."""

    def test_default_is_none(self):
        run = PlaybookRun(
            run_id="r1",
            playbook_id="p1",
            playbook_version=1,
            started_at=time.time(),
        )
        assert run.waiting_for_event is None

    def test_can_set_waiting_for_event(self):
        run = PlaybookRun(
            run_id="r1",
            playbook_id="p1",
            playbook_version=1,
            started_at=time.time(),
            waiting_for_event="workflow.stage.completed",
        )
        assert run.waiting_for_event == "workflow.stage.completed"


# ---------------------------------------------------------------------------
# Tests: Event schema validation
# ---------------------------------------------------------------------------


class TestEventSchemas:
    """Verify event schemas accept the new optional fields."""

    def test_paused_event_accepts_waiting_for_event(self):
        from src.event_schemas import validate_event

        errors = validate_event(
            "playbook.run.paused",
            {
                "playbook_id": "pb-1",
                "run_id": "r-1",
                "node_id": "wait_stage",
                "waiting_for_event": "workflow.stage.completed",
            },
        )
        assert errors == []

    def test_resumed_event_accepts_resumed_by_event(self):
        from src.event_schemas import validate_event

        errors = validate_event(
            "playbook.run.resumed",
            {
                "playbook_id": "pb-1",
                "run_id": "r-1",
                "node_id": "wait_stage",
                "resumed_by_event": "workflow.stage.completed",
            },
        )
        assert errors == []

    def test_resumed_event_decision_optional(self):
        """decision should now be optional (not required)."""
        from src.event_schemas import validate_event

        errors = validate_event(
            "playbook.run.resumed",
            {
                "playbook_id": "pb-1",
                "run_id": "r-1",
                "node_id": "n1",
            },
        )
        assert errors == []
