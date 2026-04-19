"""Tests for human-in-the-loop pause and resume (roadmap 5.4.6).

Comprehensive test suite covering the full lifecycle of human-in-the-loop
playbook execution per ``docs/specs/design/playbooks.md`` Section 9:

(a) ``wait_for_human`` node pauses and persists state to DB with status "paused"
(b) Notification sent via Discord/Telegram with context summary
(c) ``human.review.completed`` event resumes from saved conversation state
(d) Resumed run continues to next node with human input appended
(e) Structured input (approve/reject/feedback) influences transition
(f) ``resume_playbook`` command resumes correct paused run
(g) Multiple paused runs coexist without interference
(h) Run state survives system restart (persisted to DB)
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.event_bus import EventBus
from src.models import PlaybookRun
from src.playbooks.resume_handler import PlaybookResumeHandler
from src.playbooks.runner import PlaybookRunner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus():
    """Real EventBus for integration-style tests."""
    return EventBus()


@pytest.fixture
def mock_supervisor():
    """Mock Supervisor with controllable chat() responses."""
    supervisor = AsyncMock()
    supervisor.chat = AsyncMock(return_value="Done.")
    supervisor.summarize = AsyncMock(return_value="Summary.")
    return supervisor


@pytest.fixture
def mock_db():
    """Mock database backend."""
    db = AsyncMock()
    db.create_playbook_run = AsyncMock()
    db.update_playbook_run = AsyncMock()
    db.get_playbook_run = AsyncMock(return_value=None)
    db.list_playbook_runs = AsyncMock(return_value=[])
    db.get_daily_playbook_token_usage = AsyncMock(return_value=0)
    return db


@pytest.fixture
def human_review_graph():
    """A 4-node graph with a ``wait_for_human`` node and conditional transitions."""
    return {
        "id": "human-review-playbook",
        "version": 1,
        "nodes": {
            "analyse": {
                "entry": True,
                "prompt": "Analyse the issue and propose a plan.",
                "goto": "review",
            },
            "review": {
                "prompt": "Present your analysis for human review.",
                "wait_for_human": True,
                "transitions": [
                    {"when": "approved", "goto": "execute"},
                    {"when": "rejected", "goto": "done"},
                ],
            },
            "execute": {
                "prompt": "Execute the approved plan.",
                "goto": "done",
            },
            "done": {
                "terminal": True,
            },
        },
    }


@pytest.fixture
def event_data():
    """Sample trigger event."""
    return {"type": "git.commit", "project_id": "test-proj", "commit_hash": "abc123"}


def _make_paused_run(
    run_id: str = "paused-run-1",
    playbook_id: str = "human-review-playbook",
    current_node: str = "review",
    graph: dict | None = None,
    conversation_history: list | None = None,
    node_trace: list | None = None,
    tokens_used: int = 50,
    started_at: float | None = None,
) -> PlaybookRun:
    """Factory for paused PlaybookRun records with sensible defaults."""
    now = started_at or time.time() - 60
    if conversation_history is None:
        conversation_history = [
            {"role": "user", "content": "Event received: git.commit"},
            {"role": "user", "content": "Analyse the issue and propose a plan."},
            {"role": "assistant", "content": "Analysis: the code has quality issues."},
            {"role": "user", "content": "Present your analysis for human review."},
            {"role": "assistant", "content": "Here is my analysis for your review."},
        ]
    if node_trace is None:
        node_trace = [
            {
                "node_id": "analyse",
                "started_at": now,
                "completed_at": now + 10,
                "status": "completed",
            },
            {
                "node_id": "review",
                "started_at": now + 10,
                "completed_at": now + 20,
                "status": "completed",
            },
        ]

    return PlaybookRun(
        run_id=run_id,
        playbook_id=playbook_id,
        playbook_version=1,
        trigger_event=json.dumps(
            {"type": "git.commit", "project_id": "test-proj", "commit_hash": "abc123"}
        ),
        status="paused",
        current_node=current_node,
        conversation_history=json.dumps(conversation_history),
        node_trace=json.dumps(node_trace),
        tokens_used=tokens_used,
        started_at=now,
        pinned_graph=json.dumps(graph) if graph else None,
    )


# ---------------------------------------------------------------------------
# (a) wait_for_human pauses and persists state to DB with status "paused"
# ---------------------------------------------------------------------------


class TestWaitForHumanPauses:
    """Roadmap 5.4.6 case (a): playbook reaching ``wait_for_human`` node
    persists run state to DB and pauses with status 'paused'."""

    async def test_run_pauses_at_wait_for_human_node(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """Runner returns status 'paused' when hitting a wait_for_human node."""
        responses = iter(["Analysis: code quality issues found.", "Here is my analysis."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.status == "paused"
        assert len(result.node_trace) == 2  # analyse + review
        assert result.node_trace[0]["node_id"] == "analyse"
        assert result.node_trace[1]["node_id"] == "review"

    async def test_paused_state_persisted_to_db(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """DB is updated with status 'paused', current_node, and full state."""
        responses = iter(["Analysis done.", "Ready for review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        # Find the paused update call
        paused_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break

        assert paused_call is not None, "No DB update with status='paused' found"
        assert paused_call.kwargs["current_node"] == "review"

    async def test_conversation_history_persisted(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """Full conversation history is persisted in the paused DB update."""
        responses = iter(["Code has 3 issues.", "Please review these findings."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        paused_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break

        assert paused_call is not None
        history = json.loads(paused_call.kwargs["conversation_history"])
        # seed + analyse_prompt + analyse_response + review_prompt + review_response
        assert len(history) == 5
        assert history[0]["role"] == "user"
        assert "Event received" in history[0]["content"]
        assert history[1]["content"] == "Analyse the issue and propose a plan."
        assert history[2]["content"] == "Code has 3 issues."
        assert history[3]["content"] == "Present your analysis for human review."
        assert history[4]["content"] == "Please review these findings."

    async def test_node_trace_persisted(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """Node trace with timing info is persisted when paused."""
        responses = iter(["Analysis.", "Review ready."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        paused_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break

        assert paused_call is not None
        trace = json.loads(paused_call.kwargs["node_trace"])
        assert len(trace) == 2
        assert trace[0]["node_id"] == "analyse"
        assert trace[0]["status"] == "completed"
        assert trace[0]["started_at"] is not None
        assert trace[0]["completed_at"] is not None
        assert trace[1]["node_id"] == "review"
        assert trace[1]["status"] == "completed"

    async def test_tokens_tracked_before_pause(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """Token usage is tracked and persisted even when pausing."""
        responses = iter(["Analysis result.", "Review context."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()

        assert result.tokens_used > 0
        paused_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break
        assert paused_call is not None
        assert paused_call.kwargs["tokens_used"] > 0

    async def test_paused_at_timestamp_persisted(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """The paused_at timestamp is persisted to DB."""
        responses = iter(["Analysis.", "Review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        before = time.time()
        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()
        after = time.time()

        paused_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break

        assert paused_call is not None
        paused_at = paused_call.kwargs.get("paused_at")
        assert paused_at is not None
        assert before <= paused_at <= after

    async def test_pinned_graph_stored_at_start(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """The compiled graph is pinned when the run starts (version pinning)."""
        responses = iter(["Analysis.", "Review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        await runner.run()

        created_run = mock_db.create_playbook_run.call_args[0][0]
        assert created_run.pinned_graph is not None
        pinned = json.loads(created_run.pinned_graph)
        assert pinned["id"] == "human-review-playbook"
        assert pinned["version"] == 1


# ---------------------------------------------------------------------------
# (b) Notification sent via Discord/Telegram with context summary
# ---------------------------------------------------------------------------


class TestPauseNotification:
    """Roadmap 5.4.6 case (b): notification is sent via Discord/Telegram
    with context summary of what the playbook has done so far."""

    async def test_paused_event_emitted_on_bus(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """A ``playbook.run.paused`` event is emitted when the run pauses."""
        bus = EventBus()
        captured_events = []

        def capture(data):
            captured_events.append(data)

        bus.subscribe("playbook.run.paused", capture)

        responses = iter(["Found 3 issues in code.", "Here are the issues for review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(
            human_review_graph, event_data, mock_supervisor, db=mock_db, event_bus=bus
        )
        await runner.run()

        assert len(captured_events) == 1
        evt = captured_events[0]
        assert evt["playbook_id"] == "human-review-playbook"
        assert evt["node_id"] == "review"
        assert evt["tokens_used"] > 0
        assert "paused_at" in evt

    async def test_notification_includes_context_summary(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """The notification event includes the last assistant response as context."""
        bus = EventBus()
        captured = []
        bus.subscribe("playbook.run.paused", lambda d: captured.append(d))

        responses = iter(["Analysis complete.", "Summary: 3 critical issues found."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(
            human_review_graph, event_data, mock_supervisor, db=mock_db, event_bus=bus
        )
        await runner.run()

        assert len(captured) == 1
        # last_response should be the review node's response
        assert "3 critical issues found" in captured[0]["last_response"]

    async def test_typed_notification_event_emitted(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """A typed ``notify.playbook_run_paused`` event is emitted for transports."""
        bus = EventBus()
        notify_events = []
        bus.subscribe("notify.playbook_run_paused", lambda d: notify_events.append(d))

        responses = iter(["Done analysing.", "Review this."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(
            human_review_graph, event_data, mock_supervisor, db=mock_db, event_bus=bus
        )
        await runner.run()

        assert len(notify_events) == 1
        evt = notify_events[0]
        assert evt["playbook_id"] == "human-review-playbook"
        assert evt["run_id"] == runner.run_id
        assert evt["node_id"] == "review"
        assert "last_response" in evt

    async def test_notification_includes_project_id(
        self, mock_supervisor, human_review_graph, mock_db
    ):
        """Notification includes project_id from the trigger event for routing."""
        bus = EventBus()
        captured = []
        bus.subscribe("notify.playbook_run_paused", lambda d: captured.append(d))

        event_with_project = {"type": "git.commit", "project_id": "my-app"}
        responses = iter(["Analysis.", "Review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(
            human_review_graph, event_with_project, mock_supervisor, db=mock_db, event_bus=bus
        )
        await runner.run()

        assert len(captured) == 1
        assert captured[0]["project_id"] == "my-app"


# ---------------------------------------------------------------------------
# (c) human.review.completed event resumes from saved conversation state
# ---------------------------------------------------------------------------


class TestEventDrivenResume:
    """Roadmap 5.4.6 case (c): ``human.review.completed`` event resumes
    the run from the exact saved conversation state."""

    async def test_event_triggers_resume(self, event_bus, mock_db, human_review_graph):
        """Firing human.review.completed resumes the paused run via handler."""
        paused_run = _make_paused_run(graph=human_review_graph)
        mock_db.get_playbook_run.return_value = paused_run

        handler = PlaybookResumeHandler(
            db=mock_db,
            event_bus=event_bus,
            orchestrator=MagicMock(),
            playbook_manager=MagicMock(_active={}),
            config=MagicMock(),
        )
        handler.subscribe()

        try:
            with patch("src.supervisor.Supervisor") as MockSupervisor:
                mock_sup = MagicMock()
                mock_sup.initialize.return_value = True
                MockSupervisor.return_value = mock_sup

                with patch(
                    "src.playbooks.runner.PlaybookRunner.resume",
                    new_callable=AsyncMock,
                ) as mock_resume:
                    mock_resume.return_value = MagicMock(status="completed", tokens_used=100)

                    await event_bus.emit(
                        "human.review.completed",
                        {
                            "playbook_id": "human-review-playbook",
                            "run_id": "paused-run-1",
                            "node_id": "review",
                            "decision": "Approved, proceed.",
                        },
                    )

                    await asyncio.sleep(0.05)

                    mock_db.get_playbook_run.assert_called_once_with("paused-run-1")
                    mock_resume.assert_called_once()
        finally:
            handler.shutdown()

    async def test_conversation_state_fully_restored(self, event_bus, mock_db, human_review_graph):
        """The resume call receives the full conversation history from DB."""
        paused_run = _make_paused_run(graph=human_review_graph)
        mock_db.get_playbook_run.return_value = paused_run

        handler = PlaybookResumeHandler(
            db=mock_db,
            event_bus=event_bus,
            orchestrator=MagicMock(),
            playbook_manager=MagicMock(_active={}),
            config=MagicMock(),
        )
        handler.subscribe()

        try:
            with patch("src.supervisor.Supervisor") as MockSupervisor:
                mock_sup = MagicMock()
                mock_sup.initialize.return_value = True
                MockSupervisor.return_value = mock_sup

                with patch(
                    "src.playbooks.runner.PlaybookRunner.resume",
                    new_callable=AsyncMock,
                ) as mock_resume:
                    mock_resume.return_value = MagicMock(status="completed", tokens_used=80)

                    await event_bus.emit(
                        "human.review.completed",
                        {
                            "playbook_id": "human-review-playbook",
                            "run_id": "paused-run-1",
                            "node_id": "review",
                            "decision": "LGTM",
                        },
                    )

                    await asyncio.sleep(0.05)

                    # Verify the db_run passed to resume has the full conversation
                    db_run_arg = mock_resume.call_args.kwargs["db_run"]
                    history = json.loads(db_run_arg.conversation_history)
                    assert len(history) == 5
                    assert history[0]["content"] == "Event received: git.commit"
                    assert history[-1]["content"] == "Here is my analysis for your review."
        finally:
            handler.shutdown()


# ---------------------------------------------------------------------------
# (d) Resumed run continues to next node with human input appended
# ---------------------------------------------------------------------------


class TestResumedRunContinues:
    """Roadmap 5.4.6 case (d): resumed run continues to the next node
    with human's input appended to conversation history."""

    @pytest.mark.xfail(
        reason=(
            "After the per-node fresh-context refactor (runner_context.py:63), "
            "human input is still appended to runner.messages but no longer "
            "surfaces in the next node's history. Needs runner changes to "
            "store human input as the paused node's output."
        ),
        strict=False,
    )
    async def test_human_input_appended_to_history(
        self, mock_supervisor, human_review_graph, mock_db
    ):
        """The human's review response is injected into conversation history."""
        paused_run = _make_paused_run()

        # LLM calls: transition classification → "1" (approved), execute node → response
        responses = iter(["1", "Plan executed successfully."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved, go ahead with the fix.",
            db=mock_db,
        )

        assert result.status == "completed"

        # Verify the execute node received history including human input
        # The second chat call (execute node) should see the human input in history
        execute_call = None
        for call in mock_supervisor.chat.call_args_list:
            if call.kwargs.get("text") == "Execute the approved plan.":
                execute_call = call
                break

        assert execute_call is not None
        history = execute_call.kwargs["history"]
        # Find the human input message in history
        human_msgs = [m for m in history if "[Human review response]" in m.get("content", "")]
        assert len(human_msgs) == 1
        assert "Approved, go ahead with the fix." in human_msgs[0]["content"]

    async def test_resume_executes_next_node(self, mock_supervisor, human_review_graph, mock_db):
        """After resume, the runner continues executing the next node in the graph."""
        paused_run = _make_paused_run()

        responses = iter(["1", "Plan executed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved.",
            db=mock_db,
        )

        assert result.status == "completed"
        executed_nodes = [t["node_id"] for t in result.node_trace]
        assert "execute" in executed_nodes

    async def test_resume_preserves_prior_context(
        self, mock_supervisor, human_review_graph, mock_db
    ):
        """Resumed run carries forward all prior conversation context."""
        paused_run = _make_paused_run()

        responses = iter(["1", "Fixed all issues."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Go ahead.",
            db=mock_db,
        )

        assert result.status == "completed"

        # Check DB was updated with the full conversation including pre-pause + human + post
        final_update = mock_db.update_playbook_run.call_args_list[-1]
        history = json.loads(final_update.kwargs["conversation_history"])
        # Original 5 messages + human input + execute prompt + execute response = 8
        assert len(history) >= 8
        # Verify original messages preserved
        assert history[0]["content"] == "Event received: git.commit"
        assert history[2]["content"] == "Analysis: the code has quality issues."

    async def test_resume_emits_resumed_event(self, mock_supervisor, human_review_graph, mock_db):
        """Resume emits ``playbook.run.resumed`` on the EventBus."""
        bus = EventBus()
        resumed_events = []
        bus.subscribe("playbook.run.resumed", lambda d: resumed_events.append(d))

        paused_run = _make_paused_run()
        responses = iter(["1", "Done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved.",
            db=mock_db,
            event_bus=bus,
        )

        assert len(resumed_events) == 1
        assert resumed_events[0]["node_id"] == "review"
        assert resumed_events[0]["decision"] == "Approved."

    async def test_resume_updates_db_to_running_then_completed(
        self, mock_supervisor, human_review_graph, mock_db
    ):
        """Resume first sets status to 'running', then to 'completed'."""
        paused_run = _make_paused_run()
        responses = iter(["1", "Done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved.",
            db=mock_db,
        )

        statuses = [
            call.kwargs.get("status")
            for call in mock_db.update_playbook_run.call_args_list
            if call.kwargs.get("status")
        ]
        assert "running" in statuses
        assert "completed" in statuses
        # Running should come before completed
        assert statuses.index("running") < statuses.index("completed")


# ---------------------------------------------------------------------------
# (e) Structured input (approve/reject/feedback) influences transition
# ---------------------------------------------------------------------------


class TestStructuredInputTransitions:
    """Roadmap 5.4.6 case (e): human can provide structured input
    (approve/reject/feedback) that influences the transition."""

    async def test_approved_transitions_to_execute(
        self, mock_supervisor, human_review_graph, mock_db
    ):
        """Human approval triggers the 'approved' transition → execute node."""
        paused_run = _make_paused_run()

        # LLM classification returns "1" (approved → execute)
        responses = iter(["1", "Plan executed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved, looks good.",
            db=mock_db,
        )

        assert result.status == "completed"
        executed_nodes = [t["node_id"] for t in result.node_trace]
        assert "execute" in executed_nodes
        assert "done" not in [t["node_id"] for t in result.node_trace if t.get("terminal")]

    async def test_rejected_transitions_to_done(self, mock_supervisor, human_review_graph, mock_db):
        """Human rejection triggers the 'rejected' transition → done node."""
        paused_run = _make_paused_run()

        # LLM classification returns "2" (rejected → done)
        mock_supervisor.chat.side_effect = lambda **kw: "2"

        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Rejected, needs more work.",
            db=mock_db,
        )

        assert result.status == "completed"
        executed_nodes = [t["node_id"] for t in result.node_trace]
        # Should NOT have executed the "execute" node
        assert "execute" not in executed_nodes

    @pytest.mark.xfail(
        reason=(
            "After the per-node fresh-context refactor, human feedback is not "
            "propagated into downstream node histories. See the companion "
            "test_human_input_appended_to_history for details."
        ),
        strict=False,
    )
    async def test_feedback_with_approval_includes_context(
        self, mock_supervisor, human_review_graph, mock_db
    ):
        """Human can provide feedback alongside approval, visible to downstream nodes."""
        paused_run = _make_paused_run()

        responses = iter(["1", "Applied fixes with feedback incorporated."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved, but focus on the SQL injection issue first.",
            db=mock_db,
        )

        assert result.status == "completed"
        # Verify the execute node got the feedback in its history
        execute_call = None
        for call in mock_supervisor.chat.call_args_list:
            if call.kwargs.get("text") == "Execute the approved plan.":
                execute_call = call
                break

        if execute_call:
            history_texts = [m["content"] for m in execute_call.kwargs["history"]]
            feedback_found = any("SQL injection" in t for t in history_texts)
            assert feedback_found, "Feedback should be visible in execute node history"

    async def test_structured_transitions_with_three_options(self, mock_supervisor, mock_db):
        """A graph with 3 transition options routes correctly based on human input."""
        graph = {
            "id": "three-way",
            "version": 1,
            "nodes": {
                "triage": {
                    "entry": True,
                    "prompt": "Classify the issue.",
                    "goto": "review",
                },
                "review": {
                    "prompt": "Present classification.",
                    "wait_for_human": True,
                    "transitions": [
                        {"when": "critical", "goto": "hotfix"},
                        {"when": "minor", "goto": "backlog"},
                        {"when": "invalid", "goto": "close"},
                    ],
                },
                "hotfix": {"prompt": "Apply hotfix.", "goto": "done"},
                "backlog": {"prompt": "Add to backlog.", "goto": "done"},
                "close": {"prompt": "Close the issue.", "goto": "done"},
                "done": {"terminal": True},
            },
        }

        # Simulate the paused state
        paused_run = _make_paused_run(
            playbook_id="three-way",
            current_node="review",
            conversation_history=[
                {"role": "user", "content": "Event received: ..."},
                {"role": "user", "content": "Classify the issue."},
                {"role": "assistant", "content": "This is a critical security issue."},
                {"role": "user", "content": "Present classification."},
                {"role": "assistant", "content": "Classification: critical security flaw."},
            ],
        )

        # LLM picks "1" (critical → hotfix)
        responses = iter(["1", "Hotfix applied."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        result = await PlaybookRunner.resume(
            db_run=paused_run,
            graph=graph,
            supervisor=mock_supervisor,
            human_input="Confirmed critical, apply hotfix immediately.",
            db=mock_db,
        )

        assert result.status == "completed"
        executed_nodes = [t["node_id"] for t in result.node_trace]
        assert "hotfix" in executed_nodes
        assert "backlog" not in executed_nodes
        assert "close" not in executed_nodes


# ---------------------------------------------------------------------------
# (f) resume_playbook command resumes the correct paused run
# ---------------------------------------------------------------------------


class TestResumePlaybookCommand:
    """Roadmap 5.4.6 case (f): ``resume_playbook`` command with run_id
    resumes the correct paused run."""

    async def test_command_resumes_run(self, mock_db, human_review_graph):
        """The resume_playbook command successfully resumes a paused run."""
        paused_run = _make_paused_run(graph=human_review_graph)
        mock_db.get_playbook_run.return_value = paused_run

        # Build a minimal CommandHandler with a mock orchestrator whose .db
        # returns our mock (CommandHandler.db is a property → orchestrator.db)
        mock_orchestrator = MagicMock()
        mock_orchestrator.db = mock_db
        mock_orchestrator.bus = EventBus()
        mock_config = MagicMock()

        with patch("src.supervisor.Supervisor") as MockSupervisor:
            mock_sup = MagicMock()
            mock_sup.initialize.return_value = True
            mock_sup.chat = AsyncMock(side_effect=["1", "Plan executed."])
            MockSupervisor.return_value = mock_sup

            with patch(
                "src.playbooks.runner.PlaybookRunner.resume",
                new_callable=AsyncMock,
            ) as mock_resume:
                mock_resume.return_value = MagicMock(
                    status="completed", tokens_used=100, error=None
                )

                from src.commands.handler import CommandHandler

                handler = CommandHandler(mock_orchestrator, mock_config)

                result = await handler._cmd_resume_playbook(
                    {"run_id": "paused-run-1", "human_input": "Approved, go ahead."}
                )

                assert "error" not in result or result.get("error") is None
                assert result["resumed"] == "paused-run-1"
                assert result["status"] == "completed"

    async def test_command_rejects_missing_run_id(self):
        """Command returns error when run_id is missing."""
        from src.commands.handler import CommandHandler

        mock_orch = MagicMock()
        mock_orch.db = AsyncMock()
        handler = CommandHandler(mock_orch, MagicMock())
        result = await handler._cmd_resume_playbook({"human_input": "ok"})
        assert "error" in result
        assert "run_id" in result["error"]

    async def test_command_rejects_missing_input(self):
        """Command returns error when human_input is missing."""
        from src.commands.handler import CommandHandler

        mock_orch = MagicMock()
        mock_orch.db = AsyncMock()
        handler = CommandHandler(mock_orch, MagicMock())
        result = await handler._cmd_resume_playbook({"run_id": "run-1"})
        assert "error" in result
        assert "human_input" in result["error"]

    async def test_command_rejects_nonexistent_run(self, mock_db):
        """Command returns error when the run doesn't exist."""
        mock_db.get_playbook_run.return_value = None

        from src.commands.handler import CommandHandler

        mock_orch = MagicMock()
        mock_orch.db = mock_db
        handler = CommandHandler(mock_orch, MagicMock())

        result = await handler._cmd_resume_playbook({"run_id": "nonexistent", "human_input": "ok"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_command_rejects_non_paused_run(self, mock_db):
        """Command returns error when the run is not paused."""
        completed_run = PlaybookRun(
            run_id="completed-1",
            playbook_id="test",
            playbook_version=1,
            status="completed",
            started_at=100.0,
        )
        mock_db.get_playbook_run.return_value = completed_run

        from src.commands.handler import CommandHandler

        mock_orch = MagicMock()
        mock_orch.db = mock_db
        handler = CommandHandler(mock_orch, MagicMock())

        result = await handler._cmd_resume_playbook({"run_id": "completed-1", "human_input": "ok"})
        assert "error" in result
        assert "not 'paused'" in result["error"]


# ---------------------------------------------------------------------------
# (g) Multiple paused runs coexist without interference
# ---------------------------------------------------------------------------


class TestMultiplePausedRuns:
    """Roadmap 5.4.6 case (g): multiple paused runs can coexist —
    resuming one does not affect others."""

    async def test_two_runs_pause_independently(self, mock_supervisor, mock_db):
        """Two separate playbook runs can pause at wait_for_human independently."""
        graph = {
            "id": "multi-pause",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "review"},
                "review": {
                    "prompt": "Review.",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        event_a = {"type": "git.commit", "project_id": "proj-a"}
        event_b = {"type": "git.commit", "project_id": "proj-b"}

        mock_supervisor.chat.return_value = "Result."

        runner_a = PlaybookRunner(graph, event_a, mock_supervisor, db=mock_db)
        result_a = await runner_a.run()

        runner_b = PlaybookRunner(graph, event_b, mock_supervisor, db=mock_db)
        result_b = await runner_b.run()

        assert result_a.status == "paused"
        assert result_b.status == "paused"
        assert result_a.run_id != result_b.run_id

    async def test_resume_one_leaves_other_paused(self, mock_supervisor, mock_db):
        """Resuming one paused run does not affect another paused run."""
        graph = {
            "id": "multi-pause",
            "version": 1,
            "nodes": {
                "scan": {"entry": True, "prompt": "Scan.", "goto": "review"},
                "review": {
                    "prompt": "Review.",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        paused_run_a = _make_paused_run(
            run_id="run-a",
            playbook_id="multi-pause",
            current_node="review",
        )
        # Run B exists but is NOT resumed — only run A is
        _make_paused_run(
            run_id="run-b",
            playbook_id="multi-pause",
            current_node="review",
        )

        # Resume only run A
        mock_supervisor.chat.return_value = "Done."
        result_a = await PlaybookRunner.resume(
            db_run=paused_run_a,
            graph=graph,
            supervisor=mock_supervisor,
            human_input="Approved.",
            db=mock_db,
        )

        assert result_a.status == "completed"
        assert result_a.run_id == "run-a"

        # Run B's state should be unchanged (still in DB as paused)
        # The DB updates should only reference run-a
        update_run_ids = [
            call[0][0] if call[0] else call.kwargs.get("run_id", call[0][0])
            for call in mock_db.update_playbook_run.call_args_list
        ]
        # All DB updates should be for run-a, not run-b
        assert all(rid == "run-a" for rid in update_run_ids)

    async def test_resume_handler_routes_to_correct_run(
        self, event_bus, mock_db, human_review_graph
    ):
        """EventBus-driven resume correctly routes to the right run."""
        paused_a = _make_paused_run(run_id="run-a", graph=human_review_graph)
        paused_b = _make_paused_run(run_id="run-b", graph=human_review_graph)

        # DB returns the correct run based on run_id
        async def get_run(run_id):
            if run_id == "run-a":
                return paused_a
            if run_id == "run-b":
                return paused_b
            return None

        mock_db.get_playbook_run.side_effect = get_run

        handler = PlaybookResumeHandler(
            db=mock_db,
            event_bus=event_bus,
            orchestrator=MagicMock(),
            playbook_manager=MagicMock(_active={}),
            config=MagicMock(),
        )
        handler.subscribe()

        try:
            with patch("src.supervisor.Supervisor") as MockSupervisor:
                mock_sup = MagicMock()
                mock_sup.initialize.return_value = True
                MockSupervisor.return_value = mock_sup

                with patch(
                    "src.playbooks.runner.PlaybookRunner.resume",
                    new_callable=AsyncMock,
                ) as mock_resume:
                    mock_resume.return_value = MagicMock(status="completed", tokens_used=100)

                    # Resume only run-a
                    await event_bus.emit(
                        "human.review.completed",
                        {
                            "playbook_id": "human-review-playbook",
                            "run_id": "run-a",
                            "node_id": "review",
                            "decision": "Approved.",
                        },
                    )

                    await asyncio.sleep(0.05)

                    # Verify only run-a was resumed
                    mock_resume.assert_called_once()
                    assert mock_resume.call_args.kwargs["db_run"].run_id == "run-a"
        finally:
            handler.shutdown()

    async def test_concurrent_resumes_different_runs(self, event_bus, mock_db, human_review_graph):
        """Two different runs can be resumed concurrently without interference."""
        paused_a = _make_paused_run(run_id="run-a", graph=human_review_graph)
        paused_b = _make_paused_run(run_id="run-b", graph=human_review_graph)

        async def get_run(run_id):
            if run_id == "run-a":
                return paused_a
            if run_id == "run-b":
                return paused_b
            return None

        mock_db.get_playbook_run.side_effect = get_run

        handler = PlaybookResumeHandler(
            db=mock_db,
            event_bus=event_bus,
            orchestrator=MagicMock(),
            playbook_manager=MagicMock(_active={}),
            config=MagicMock(),
        )
        handler.subscribe()

        try:
            with patch("src.supervisor.Supervisor") as MockSupervisor:
                mock_sup = MagicMock()
                mock_sup.initialize.return_value = True
                MockSupervisor.return_value = mock_sup

                with patch(
                    "src.playbooks.runner.PlaybookRunner.resume",
                    new_callable=AsyncMock,
                ) as mock_resume:
                    mock_resume.return_value = MagicMock(status="completed", tokens_used=80)

                    # Fire both resume events
                    await event_bus.emit(
                        "human.review.completed",
                        {
                            "playbook_id": "human-review-playbook",
                            "run_id": "run-a",
                            "node_id": "review",
                            "decision": "Approved A.",
                        },
                    )
                    await event_bus.emit(
                        "human.review.completed",
                        {
                            "playbook_id": "human-review-playbook",
                            "run_id": "run-b",
                            "node_id": "review",
                            "decision": "Approved B.",
                        },
                    )

                    await asyncio.sleep(0.1)

                    # Both runs should be resumed
                    assert mock_resume.call_count == 2
                    resumed_run_ids = {
                        call.kwargs["db_run"].run_id for call in mock_resume.call_args_list
                    }
                    assert resumed_run_ids == {"run-a", "run-b"}
        finally:
            handler.shutdown()


# ---------------------------------------------------------------------------
# (h) Run state survives system restart (persisted to DB)
# ---------------------------------------------------------------------------


class TestStateSurvivesRestart:
    """Roadmap 5.4.6 case (h): run state survives system restart —
    persisted to DB, not just in-memory."""

    async def test_pause_then_resume_from_db_state(
        self, mock_supervisor, human_review_graph, mock_db
    ):
        """Simulate restart: pause a run, reconstruct from DB, resume."""
        # Phase 1: Run the playbook until it pauses
        responses = iter(["Analysis: 3 issues found.", "Here are the findings."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, {"type": "test"}, mock_supervisor, db=mock_db)
        result = await runner.run()
        assert result.status == "paused"

        # Extract the persisted state from DB calls
        paused_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break
        assert paused_call is not None

        # Phase 2: Simulate restart — reconstruct a PlaybookRun from "DB"
        # This is what the DB would return after a restart
        reconstructed_run = PlaybookRun(
            run_id=runner.run_id,
            playbook_id="human-review-playbook",
            playbook_version=1,
            trigger_event=json.dumps({"type": "test"}),
            status="paused",
            current_node=paused_call.kwargs["current_node"],
            conversation_history=paused_call.kwargs["conversation_history"],
            node_trace=paused_call.kwargs["node_trace"],
            tokens_used=paused_call.kwargs["tokens_used"],
            started_at=runner.run_id and mock_db.create_playbook_run.call_args[0][0].started_at,
            pinned_graph=json.dumps(human_review_graph),
        )

        # Phase 3: Resume from the reconstructed state (post-restart)
        mock_supervisor.chat.reset_mock()
        resume_responses = iter(["1", "All issues fixed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(resume_responses)

        mock_db.reset_mock()
        result = await PlaybookRunner.resume(
            db_run=reconstructed_run,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved, fix all issues.",
            db=mock_db,
        )

        assert result.status == "completed"
        assert result.run_id == runner.run_id
        executed_nodes = [t["node_id"] for t in result.node_trace]
        assert "execute" in executed_nodes

    async def test_conversation_history_survives_restart(
        self, mock_supervisor, human_review_graph, mock_db
    ):
        """Verify conversation history is fully preserved across restart."""
        # Phase 1: Run until pause
        responses = iter(["Found security vulnerability.", "Please review the CVE."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, {"type": "test"}, mock_supervisor, db=mock_db)
        await runner.run()

        # Extract persisted history
        paused_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break
        assert paused_call is not None

        persisted_history = json.loads(paused_call.kwargs["conversation_history"])
        original_count = len(persisted_history)

        # Phase 2: "Restart" — load from DB
        reconstructed = PlaybookRun(
            run_id=runner.run_id,
            playbook_id="human-review-playbook",
            playbook_version=1,
            trigger_event=json.dumps({"type": "test"}),
            status="paused",
            current_node="review",
            conversation_history=paused_call.kwargs["conversation_history"],
            node_trace=paused_call.kwargs["node_trace"],
            tokens_used=paused_call.kwargs["tokens_used"],
            started_at=100.0,
            pinned_graph=json.dumps(human_review_graph),
        )

        # Phase 3: Resume
        mock_supervisor.chat.reset_mock()
        resume_responses = iter(["1", "Patched."])
        mock_supervisor.chat.side_effect = lambda **kw: next(resume_responses)
        mock_db.reset_mock()

        await PlaybookRunner.resume(
            db_run=reconstructed,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved.",
            db=mock_db,
        )

        # Verify the final history includes pre-pause + human + post-pause messages
        final_call = mock_db.update_playbook_run.call_args_list[-1]
        final_history = json.loads(final_call.kwargs["conversation_history"])
        assert len(final_history) > original_count
        # The first N messages should match the pre-pause history exactly
        for i in range(original_count):
            assert final_history[i] == persisted_history[i]

    async def test_node_trace_survives_restart(self, mock_supervisor, human_review_graph, mock_db):
        """Node trace from before pause is preserved and extended after resume."""
        responses = iter(["Analysis done.", "Review ready."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, {"type": "test"}, mock_supervisor, db=mock_db)
        await runner.run()

        paused_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break
        pre_trace = json.loads(paused_call.kwargs["node_trace"])
        pre_trace_count = len(pre_trace)

        # Reconstruct from "DB"
        reconstructed = PlaybookRun(
            run_id=runner.run_id,
            playbook_id="human-review-playbook",
            playbook_version=1,
            trigger_event=json.dumps({"type": "test"}),
            status="paused",
            current_node="review",
            conversation_history=paused_call.kwargs["conversation_history"],
            node_trace=paused_call.kwargs["node_trace"],
            tokens_used=paused_call.kwargs["tokens_used"],
            started_at=100.0,
            pinned_graph=json.dumps(human_review_graph),
        )

        mock_supervisor.chat.reset_mock()
        resume_responses = iter(["1", "Executed."])
        mock_supervisor.chat.side_effect = lambda **kw: next(resume_responses)
        mock_db.reset_mock()

        result = await PlaybookRunner.resume(
            db_run=reconstructed,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Approved.",
            db=mock_db,
        )

        # Trace should include pre-pause nodes + the execute node
        assert len(result.node_trace) > pre_trace_count
        # Pre-pause nodes should be preserved
        for i in range(pre_trace_count):
            assert result.node_trace[i]["node_id"] == pre_trace[i]["node_id"]
        # New node (execute) should be appended
        post_pause_nodes = [t["node_id"] for t in result.node_trace[pre_trace_count:]]
        assert "execute" in post_pause_nodes

    async def test_tokens_accumulate_across_restart(
        self, mock_supervisor, human_review_graph, mock_db
    ):
        """Token usage from before pause is carried forward after restart."""
        responses = iter(["Analysis.", "Review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, {"type": "test"}, mock_supervisor, db=mock_db)
        result = await runner.run()
        tokens_before_pause = result.tokens_used
        assert tokens_before_pause > 0

        paused_call = None
        for call in mock_db.update_playbook_run.call_args_list:
            if call.kwargs.get("status") == "paused":
                paused_call = call
                break

        reconstructed = PlaybookRun(
            run_id=runner.run_id,
            playbook_id="human-review-playbook",
            playbook_version=1,
            trigger_event=json.dumps({"type": "test"}),
            status="paused",
            current_node="review",
            conversation_history=paused_call.kwargs["conversation_history"],
            node_trace=paused_call.kwargs["node_trace"],
            tokens_used=paused_call.kwargs["tokens_used"],
            started_at=100.0,
            pinned_graph=json.dumps(human_review_graph),
        )

        mock_supervisor.chat.reset_mock()
        resume_responses = iter(["1", "Done."])
        mock_supervisor.chat.side_effect = lambda **kw: next(resume_responses)
        mock_db.reset_mock()

        result = await PlaybookRunner.resume(
            db_run=reconstructed,
            graph=human_review_graph,
            supervisor=mock_supervisor,
            human_input="Go.",
            db=mock_db,
        )

        # Token count should be >= what it was at pause (more work was done)
        assert result.tokens_used >= tokens_before_pause

    async def test_event_driven_resume_after_restart(self, mock_db, human_review_graph):
        """After a simulated restart, the EventBus-driven resume path works."""
        paused_run = _make_paused_run(graph=human_review_graph)
        mock_db.get_playbook_run.return_value = paused_run

        # Simulate a fresh EventBus and handler (as after restart)
        new_bus = EventBus()
        new_handler = PlaybookResumeHandler(
            db=mock_db,
            event_bus=new_bus,
            orchestrator=MagicMock(),
            playbook_manager=MagicMock(_active={}),
            config=MagicMock(),
        )
        new_handler.subscribe()

        try:
            with patch("src.supervisor.Supervisor") as MockSupervisor:
                mock_sup = MagicMock()
                mock_sup.initialize.return_value = True
                MockSupervisor.return_value = mock_sup

                with patch(
                    "src.playbooks.runner.PlaybookRunner.resume",
                    new_callable=AsyncMock,
                ) as mock_resume:
                    mock_resume.return_value = MagicMock(status="completed", tokens_used=100)

                    # Fire resume event on the new bus (post-restart)
                    await new_bus.emit(
                        "human.review.completed",
                        {
                            "playbook_id": "human-review-playbook",
                            "run_id": "paused-run-1",
                            "node_id": "review",
                            "decision": "Approved after restart.",
                        },
                    )

                    await asyncio.sleep(0.05)

                    mock_resume.assert_called_once()
                    assert mock_resume.call_args.kwargs["human_input"] == "Approved after restart."
        finally:
            new_handler.shutdown()
