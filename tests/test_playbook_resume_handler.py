"""Tests for PlaybookResumeHandler — event-driven resume of paused playbook runs.

Roadmap 5.4.3: Implements ``human.review.completed`` event handling to resume
runs from saved conversation state per playbooks spec Section 9.

Test cases cover:
(a) Handler subscribes to ``human.review.completed`` on EventBus
(b) Firing ``human.review.completed`` resumes a paused run from saved
    conversation state with the human's decision appended
(c) Conversation history is fully restored from the database
(d) Resumed run continues to the next node and completes
(e) Missing or invalid run_id is handled gracefully
(f) Non-paused runs are skipped (idempotent resume)
(g) Pause timeout enforcement marks timed-out runs
(h) Duplicate events for the same run are de-duplicated
(i) Handler shutdown cancels in-flight resumes and unsubscribes
(j) Graph resolution uses pinned_graph when available
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus():
    """Real EventBus instance for integration-style tests."""
    return EventBus()


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.get_playbook_run = AsyncMock(return_value=None)
    db.update_playbook_run = AsyncMock()
    return db


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock()
    orch.bus = None  # Overridden per-test
    return orch


@pytest.fixture
def mock_config():
    return MagicMock()


@pytest.fixture
def mock_playbook_manager():
    mgr = MagicMock()
    mgr._active = {}
    return mgr


@pytest.fixture
def human_review_graph():
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
def paused_run(human_review_graph):
    """A paused PlaybookRun with saved conversation state."""
    now = time.time()
    return PlaybookRun(
        run_id="paused-abc123",
        playbook_id="human-review-playbook",
        playbook_version=1,
        trigger_event=json.dumps(
            {"type": "git.commit", "project_id": "test-proj", "commit_hash": "abc123"}
        ),
        status="paused",
        current_node="review",
        conversation_history=json.dumps(
            [
                {"role": "user", "content": "Event received: git.commit"},
                {"role": "user", "content": "Analyse the issue and propose a plan."},
                {"role": "assistant", "content": "Analysis: the code has quality issues."},
                {"role": "user", "content": "Present your analysis for human review."},
                {"role": "assistant", "content": "Here is my analysis for your review."},
            ]
        ),
        node_trace=json.dumps(
            [
                {
                    "node_id": "analyse",
                    "started_at": now - 30,
                    "completed_at": now - 20,
                    "status": "completed",
                },
                {
                    "node_id": "review",
                    "started_at": now - 20,
                    "completed_at": now - 10,
                    "status": "completed",
                },
            ]
        ),
        tokens_used=50,
        started_at=now - 60,
        pinned_graph=json.dumps(human_review_graph),
    )


@pytest.fixture
def handler(mock_db, event_bus, mock_orchestrator, mock_playbook_manager, mock_config):
    """A fully wired PlaybookResumeHandler."""
    h = PlaybookResumeHandler(
        db=mock_db,
        event_bus=event_bus,
        orchestrator=mock_orchestrator,
        playbook_manager=mock_playbook_manager,
        config=mock_config,
    )
    h.subscribe()
    yield h
    h.shutdown()


# ---------------------------------------------------------------------------
# Tests: Subscription lifecycle
# ---------------------------------------------------------------------------


class TestSubscription:
    def test_subscribe_registers_handler(self, handler, event_bus):
        """Handler subscribes to human.review.completed on the EventBus."""
        # Verify the handler is subscribed by checking EventBus has subscribers
        assert len(handler._unsubscribes) == 1

    def test_unsubscribe_removes_handler(self, handler, event_bus):
        """Unsubscribe clears all subscriptions."""
        handler.unsubscribe()
        assert len(handler._unsubscribes) == 0

    def test_subscribe_idempotent(self, handler, event_bus):
        """Calling subscribe() multiple times clears previous subscriptions."""
        handler.subscribe()
        handler.subscribe()
        # Should have exactly 1 subscription, not accumulated
        assert len(handler._unsubscribes) == 1

    def test_shutdown_clears_state(self, handler, event_bus):
        """Shutdown unsubscribes and clears in-flight resumes."""
        handler.shutdown()
        assert len(handler._unsubscribes) == 0
        assert len(handler._running_resumes) == 0


# ---------------------------------------------------------------------------
# Tests: Event-driven resume from saved conversation state
# ---------------------------------------------------------------------------


class TestEventDrivenResume:
    async def test_event_triggers_resume(self, handler, event_bus, mock_db, paused_run):
        """Firing human.review.completed resumes the paused run."""
        mock_db.get_playbook_run.return_value = paused_run

        with patch("src.supervisor.Supervisor") as MockSupervisor:
            mock_sup = MagicMock()
            mock_sup.initialize.return_value = True
            mock_sup.chat = AsyncMock(side_effect=["1", "Plan executed."])
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
                        "run_id": "paused-abc123",
                        "node_id": "review",
                        "decision": "Approved, go ahead.",
                    },
                )

                # Give the background task a chance to run
                await asyncio.sleep(0.05)

                mock_db.get_playbook_run.assert_called_once_with("paused-abc123")
                mock_resume.assert_called_once()
                call_kwargs = mock_resume.call_args.kwargs
                assert call_kwargs["db_run"] == paused_run
                assert call_kwargs["human_input"] == "Approved, go ahead."
                assert call_kwargs["db"] == mock_db
                assert call_kwargs["event_bus"] == event_bus

    async def test_resume_restores_conversation_state(
        self, handler, event_bus, mock_db, paused_run
    ):
        """The resume call receives the paused run with full conversation history."""
        mock_db.get_playbook_run.return_value = paused_run

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
                        "run_id": "paused-abc123",
                        "node_id": "review",
                        "decision": "LGTM, proceed.",
                    },
                )

                await asyncio.sleep(0.05)

                # Verify the db_run passed to resume has the saved conversation
                db_run_arg = mock_resume.call_args.kwargs["db_run"]
                history = json.loads(db_run_arg.conversation_history)
                assert len(history) == 5
                assert history[0]["content"] == "Event received: git.commit"
                assert history[-1]["content"] == "Here is my analysis for your review."

    async def test_resume_uses_pinned_graph(
        self, handler, event_bus, mock_db, paused_run, human_review_graph
    ):
        """When the run has a pinned_graph, it is used instead of PlaybookManager."""
        mock_db.get_playbook_run.return_value = paused_run

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
                        "run_id": "paused-abc123",
                        "node_id": "review",
                        "decision": "Approved.",
                    },
                )

                await asyncio.sleep(0.05)

                graph_arg = mock_resume.call_args.kwargs["graph"]
                assert graph_arg == human_review_graph


# ---------------------------------------------------------------------------
# Tests: Validation and error handling
# ---------------------------------------------------------------------------


class TestValidation:
    async def test_missing_run_id_ignored(self, handler, event_bus, mock_db):
        """Events without run_id are silently ignored."""
        await event_bus.emit(
            "human.review.completed",
            {
                "playbook_id": "test",
                "node_id": "review",
                "decision": "ok",
            },
        )

        await asyncio.sleep(0.05)
        mock_db.get_playbook_run.assert_not_called()

    async def test_empty_decision_ignored(self, handler, event_bus, mock_db):
        """Events with empty decision are silently ignored."""
        await event_bus.emit(
            "human.review.completed",
            {
                "playbook_id": "test",
                "run_id": "run-1",
                "node_id": "review",
                "decision": "",
            },
        )

        await asyncio.sleep(0.05)
        mock_db.get_playbook_run.assert_not_called()

    async def test_run_not_found(self, handler, event_bus, mock_db):
        """When the run_id doesn't exist in DB, no error is raised."""
        mock_db.get_playbook_run.return_value = None

        await event_bus.emit(
            "human.review.completed",
            {
                "playbook_id": "test",
                "run_id": "nonexistent",
                "node_id": "review",
                "decision": "ok",
            },
        )

        await asyncio.sleep(0.05)
        mock_db.get_playbook_run.assert_called_once_with("nonexistent")

    async def test_non_paused_run_skipped(self, handler, event_bus, mock_db):
        """If the run is not paused (e.g. already completed), it is skipped."""
        completed_run = PlaybookRun(
            run_id="completed-1",
            playbook_id="test",
            playbook_version=1,
            status="completed",
            started_at=100.0,
        )
        mock_db.get_playbook_run.return_value = completed_run

        with patch(
            "src.playbooks.runner.PlaybookRunner.resume",
            new_callable=AsyncMock,
        ) as mock_resume:
            await event_bus.emit(
                "human.review.completed",
                {
                    "playbook_id": "test",
                    "run_id": "completed-1",
                    "node_id": "review",
                    "decision": "ok",
                },
            )

            await asyncio.sleep(0.05)
            mock_resume.assert_not_called()

    async def test_pause_timeout_marks_timed_out(self, handler, event_bus, mock_db):
        """If the run has been paused beyond the timeout, it is marked timed_out."""
        old_run = PlaybookRun(
            run_id="old-run-1",
            playbook_id="test",
            playbook_version=1,
            status="paused",
            current_node="review",
            conversation_history="[]",
            node_trace=json.dumps(
                [
                    {
                        "node_id": "review",
                        "started_at": 100.0,
                        "completed_at": 101.0,
                        "status": "completed",
                    }
                ]
            ),
            tokens_used=10,
            started_at=100.0,
        )
        mock_db.get_playbook_run.return_value = old_run

        # Use a very short timeout
        handler._pause_timeout_seconds = 1

        with patch(
            "src.playbooks.runner.PlaybookRunner.resume",
            new_callable=AsyncMock,
        ) as mock_resume:
            await event_bus.emit(
                "human.review.completed",
                {
                    "playbook_id": "test",
                    "run_id": "old-run-1",
                    "node_id": "review",
                    "decision": "finally approved",
                },
            )

            await asyncio.sleep(0.05)
            mock_resume.assert_not_called()

            # Verify DB was updated to timed_out
            mock_db.update_playbook_run.assert_called_once()
            call_kwargs = mock_db.update_playbook_run.call_args.kwargs
            assert call_kwargs.get("status") == "timed_out"

    async def test_supervisor_init_failure(self, handler, event_bus, mock_db, paused_run):
        """If Supervisor.initialize() fails, the resume is aborted gracefully."""
        mock_db.get_playbook_run.return_value = paused_run

        with patch("src.supervisor.Supervisor") as MockSupervisor:
            mock_sup = MagicMock()
            mock_sup.initialize.return_value = False
            MockSupervisor.return_value = mock_sup

            with patch(
                "src.playbooks.runner.PlaybookRunner.resume",
                new_callable=AsyncMock,
            ) as mock_resume:
                await event_bus.emit(
                    "human.review.completed",
                    {
                        "playbook_id": "human-review-playbook",
                        "run_id": "paused-abc123",
                        "node_id": "review",
                        "decision": "Approved.",
                    },
                )

                await asyncio.sleep(0.05)
                mock_resume.assert_not_called()

    async def test_no_graph_available(self, handler, event_bus, mock_db):
        """If no graph can be resolved (no pinned, no active), resume is skipped."""
        run_no_graph = PlaybookRun(
            run_id="no-graph-1",
            playbook_id="deleted-playbook",
            playbook_version=1,
            status="paused",
            current_node="review",
            conversation_history="[]",
            node_trace=json.dumps(
                [
                    {
                        "node_id": "review",
                        "started_at": time.time() - 10,
                        "completed_at": time.time() - 9,
                        "status": "completed",
                    }
                ]
            ),
            tokens_used=0,
            started_at=time.time() - 60,
            pinned_graph=None,  # No pinned graph
        )
        mock_db.get_playbook_run.return_value = run_no_graph

        with patch(
            "src.playbooks.runner.PlaybookRunner.resume",
            new_callable=AsyncMock,
        ) as mock_resume:
            await event_bus.emit(
                "human.review.completed",
                {
                    "playbook_id": "deleted-playbook",
                    "run_id": "no-graph-1",
                    "node_id": "review",
                    "decision": "ok",
                },
            )

            await asyncio.sleep(0.05)
            mock_resume.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Duplicate event de-duplication
# ---------------------------------------------------------------------------


class TestDeDuplication:
    async def test_duplicate_events_deduplicated(self, handler, event_bus, mock_db, paused_run):
        """Firing human.review.completed twice for the same run deduplicates."""
        mock_db.get_playbook_run.return_value = paused_run

        # Make the resume slow so we can fire a second event while it's running
        resume_started = asyncio.Event()
        resume_gate = asyncio.Event()

        async def slow_resume(*args, **kwargs):
            resume_started.set()
            await resume_gate.wait()
            return MagicMock(status="completed", tokens_used=100)

        with patch("src.supervisor.Supervisor") as MockSupervisor:
            mock_sup = MagicMock()
            mock_sup.initialize.return_value = True
            MockSupervisor.return_value = mock_sup

            with patch(
                "src.playbooks.runner.PlaybookRunner.resume",
                new_callable=AsyncMock,
                side_effect=slow_resume,
            ) as mock_resume:
                # Fire first event
                await event_bus.emit(
                    "human.review.completed",
                    {
                        "playbook_id": "human-review-playbook",
                        "run_id": "paused-abc123",
                        "node_id": "review",
                        "decision": "Approved.",
                    },
                )

                # Wait for resume to start
                await asyncio.wait_for(resume_started.wait(), timeout=2.0)

                # Fire duplicate event while first is still running
                await event_bus.emit(
                    "human.review.completed",
                    {
                        "playbook_id": "human-review-playbook",
                        "run_id": "paused-abc123",
                        "node_id": "review",
                        "decision": "Approved again.",
                    },
                )

                # Release the gate
                resume_gate.set()
                await asyncio.sleep(0.05)

                # Only one resume call should have been made
                assert mock_resume.call_count == 1


# ---------------------------------------------------------------------------
# Tests: Graph resolution
# ---------------------------------------------------------------------------


class TestGraphResolution:
    async def test_fallback_to_playbook_manager(
        self, handler, event_bus, mock_db, mock_playbook_manager
    ):
        """Without pinned_graph, the handler falls back to PlaybookManager."""
        run_no_pinned = PlaybookRun(
            run_id="no-pin-1",
            playbook_id="active-playbook",
            playbook_version=1,
            status="paused",
            current_node="review",
            conversation_history=json.dumps(
                [
                    {"role": "user", "content": "Prompt."},
                    {"role": "assistant", "content": "Response."},
                ]
            ),
            node_trace=json.dumps(
                [
                    {
                        "node_id": "review",
                        "started_at": time.time() - 10,
                        "completed_at": time.time() - 9,
                        "status": "completed",
                    }
                ]
            ),
            tokens_used=10,
            started_at=time.time() - 60,
            pinned_graph=None,
        )
        mock_db.get_playbook_run.return_value = run_no_pinned

        # Set up PlaybookManager with an active playbook
        mock_pb = MagicMock()
        active_graph = {
            "id": "active-playbook",
            "version": 2,
            "nodes": {
                "review": {
                    "prompt": "Review.",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }
        mock_pb.to_dict.return_value = active_graph
        mock_playbook_manager._active = {"active-playbook": mock_pb}

        with patch("src.supervisor.Supervisor") as MockSupervisor:
            mock_sup = MagicMock()
            mock_sup.initialize.return_value = True
            MockSupervisor.return_value = mock_sup

            with patch(
                "src.playbooks.runner.PlaybookRunner.resume",
                new_callable=AsyncMock,
            ) as mock_resume:
                mock_resume.return_value = MagicMock(status="completed", tokens_used=20)

                await event_bus.emit(
                    "human.review.completed",
                    {
                        "playbook_id": "active-playbook",
                        "run_id": "no-pin-1",
                        "node_id": "review",
                        "decision": "Proceed.",
                    },
                )

                await asyncio.sleep(0.05)

                mock_resume.assert_called_once()
                graph_arg = mock_resume.call_args.kwargs["graph"]
                assert graph_arg == active_graph


# ---------------------------------------------------------------------------
# Tests: Pause timestamp extraction
# ---------------------------------------------------------------------------


class TestPausedAtExtraction:
    def test_paused_at_from_node_trace(self):
        """Extracts completed_at from last node trace entry."""
        run = PlaybookRun(
            run_id="r1",
            playbook_id="pb1",
            playbook_version=1,
            status="paused",
            started_at=100.0,
            node_trace=json.dumps(
                [
                    {
                        "node_id": "a",
                        "started_at": 100.0,
                        "completed_at": 105.0,
                        "status": "completed",
                    }
                ]
            ),
        )
        assert PlaybookResumeHandler._get_paused_at(run) == 105.0

    def test_paused_at_fallback_to_started_at(self):
        """Falls back to started_at when completed_at is missing."""
        run = PlaybookRun(
            run_id="r2",
            playbook_id="pb1",
            playbook_version=1,
            status="paused",
            started_at=100.0,
            node_trace=json.dumps(
                [
                    {
                        "node_id": "a",
                        "started_at": 102.0,
                        "status": "running",
                    }
                ]
            ),
        )
        assert PlaybookResumeHandler._get_paused_at(run) == 102.0

    def test_paused_at_empty_trace(self):
        """Falls back to run started_at when trace is empty."""
        run = PlaybookRun(
            run_id="r3",
            playbook_id="pb1",
            playbook_version=1,
            status="paused",
            started_at=100.0,
            node_trace="[]",
        )
        assert PlaybookResumeHandler._get_paused_at(run) == 100.0


# ---------------------------------------------------------------------------
# Tests: Running resumes property
# ---------------------------------------------------------------------------


class TestRunningResumesProperty:
    def test_running_resumes_returns_copy(self, handler):
        """running_resumes returns a copy, not internal state."""
        result = handler.running_resumes
        assert result is not handler._running_resumes
        assert result == {}
