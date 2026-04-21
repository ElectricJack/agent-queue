"""Tests for playbook human-review notification (roadmap 5.4.2).

Covers:
- PlaybookRunPausedEvent model: construction, serialization, defaults
- Event emission: _emit_paused_event fires both raw + notify events
- Discord notification handler: receives and routes the notify event
- Discord formatters: plain-text and embed output
- Telegram formatter: MarkdownV2 output
- End-to-end: playbook runner pause → EventBus → Discord handler
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.event_bus import EventBus
from src.notifications.events import PlaybookRunPausedEvent
from src.playbooks.runner import PlaybookRunner


# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture
def mock_supervisor():
    supervisor = AsyncMock()
    supervisor.chat = AsyncMock(return_value="Done.")
    supervisor.summarize = AsyncMock(return_value="Summary.")
    return supervisor


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.create_playbook_run = AsyncMock()
    db.update_playbook_run = AsyncMock()
    db.get_playbook_run = AsyncMock(return_value=None)
    return db


@pytest.fixture
def human_review_graph():
    """Graph with a wait_for_human node."""
    return {
        "id": "review-playbook",
        "version": 1,
        "nodes": {
            "analyse": {
                "entry": True,
                "prompt": "Analyse the issue.",
                "goto": "review",
            },
            "review": {
                "prompt": "Present findings for review.",
                "wait_for_human": True,
                "goto": "execute",
            },
            "execute": {
                "prompt": "Execute based on: {{human_input}}",
                "goto": "done",
            },
            "done": {"terminal": True},
        },
    }


@pytest.fixture
def event_data():
    return {"type": "test.trigger", "project_id": "test-project"}


# ---------------------------------------------------------------------------
# PlaybookRunPausedEvent model tests
# ---------------------------------------------------------------------------


class TestPlaybookRunPausedEvent:
    """Event model construction, defaults, and serialization."""

    def test_defaults(self):
        e = PlaybookRunPausedEvent()
        assert e.event_type == "notify.playbook_run_paused"
        assert e.category == "interaction"
        assert e.severity == "info"
        assert e.playbook_id == ""
        assert e.run_id == ""
        assert e.node_id == ""
        assert e.last_response == ""
        assert e.running_seconds == 0.0
        assert e.tokens_used == 0
        assert e.paused_at == 0.0
        assert e.project_id is None

    def test_construction_with_values(self):
        e = PlaybookRunPausedEvent(
            playbook_id="my-playbook",
            run_id="run-123",
            node_id="review",
            last_response="Here is my analysis.",
            running_seconds=45.5,
            tokens_used=1200,
            paused_at=1700000000.0,
            project_id="proj-1",
        )
        assert e.playbook_id == "my-playbook"
        assert e.run_id == "run-123"
        assert e.node_id == "review"
        assert e.last_response == "Here is my analysis."
        assert e.running_seconds == 45.5
        assert e.tokens_used == 1200
        assert e.paused_at == 1700000000.0
        assert e.project_id == "proj-1"

    def test_serialization_roundtrip(self):
        e = PlaybookRunPausedEvent(
            playbook_id="pb",
            run_id="r1",
            node_id="n1",
            last_response="context",
            tokens_used=500,
        )
        data = e.model_dump(mode="json")
        assert data["event_type"] == "notify.playbook_run_paused"
        assert data["playbook_id"] == "pb"
        assert data["last_response"] == "context"

        # Can reconstruct from dict
        e2 = PlaybookRunPausedEvent(**data)
        assert e2.playbook_id == "pb"
        assert e2.last_response == "context"


# ---------------------------------------------------------------------------
# Playbook runner emits notify event (integration)
# ---------------------------------------------------------------------------


class TestPlaybookRunnerPausedNotification:
    """Tests that _emit_paused_event fires both raw + notify events."""

    @pytest.mark.asyncio
    async def test_pause_emits_notify_event(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """When a run pauses, both playbook.run.paused and notify.playbook_run_paused fire."""
        responses = iter(["Analysis done.", "Ready for review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        runner = PlaybookRunner(
            human_review_graph, event_data, mock_supervisor, db=mock_db, event_bus=event_bus
        )
        result = await runner.run()

        assert result.status == "paused"

        # Both event types should be emitted
        event_types = [c.args[0] for c in event_bus.emit.call_args_list]
        assert "playbook.run.paused" in event_types
        assert "notify.playbook_run_paused" in event_types

    @pytest.mark.asyncio
    async def test_notify_event_contains_context_summary(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """The notify event includes last_response as the context summary."""
        responses = iter(["Analysis complete.", "Here is my analysis for your review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        runner = PlaybookRunner(
            human_review_graph, event_data, mock_supervisor, db=mock_db, event_bus=event_bus
        )
        await runner.run()

        # Find the notify.playbook_run_paused event
        notify_calls = [
            c for c in event_bus.emit.call_args_list if c.args[0] == "notify.playbook_run_paused"
        ]
        assert len(notify_calls) == 1
        payload = notify_calls[0].args[1]
        assert payload["last_response"] == "Here is my analysis for your review."
        assert payload["playbook_id"] == "review-playbook"
        assert payload["node_id"] == "review"
        assert payload["event_type"] == "notify.playbook_run_paused"

    @pytest.mark.asyncio
    async def test_notify_event_includes_project_id(
        self, mock_supervisor, human_review_graph, mock_db
    ):
        """The notify event propagates project_id from the trigger event."""
        responses = iter(["Analysis.", "Review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        event_data = {"type": "test", "project_id": "my-project"}
        runner = PlaybookRunner(
            human_review_graph, event_data, mock_supervisor, db=mock_db, event_bus=event_bus
        )
        await runner.run()

        notify_calls = [
            c for c in event_bus.emit.call_args_list if c.args[0] == "notify.playbook_run_paused"
        ]
        assert len(notify_calls) == 1
        payload = notify_calls[0].args[1]
        assert payload["project_id"] == "my-project"

    @pytest.mark.asyncio
    async def test_notify_event_includes_timing_info(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """The notify event includes running_seconds and tokens_used."""
        responses = iter(["Done.", "Review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        runner = PlaybookRunner(
            human_review_graph, event_data, mock_supervisor, db=mock_db, event_bus=event_bus
        )
        await runner.run()

        notify_calls = [
            c for c in event_bus.emit.call_args_list if c.args[0] == "notify.playbook_run_paused"
        ]
        assert len(notify_calls) == 1
        payload = notify_calls[0].args[1]
        # running_seconds should be present and non-negative
        assert payload["running_seconds"] >= 0
        # tokens_used should be present
        assert "tokens_used" in payload

    @pytest.mark.asyncio
    async def test_notify_event_not_emitted_without_event_bus(
        self, mock_supervisor, human_review_graph, event_data, mock_db
    ):
        """Without an event_bus, pause still works (no crash)."""
        responses = iter(["Analysis.", "Review."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        runner = PlaybookRunner(human_review_graph, event_data, mock_supervisor, db=mock_db)
        result = await runner.run()
        assert result.status == "paused"  # No error, graceful handling

    @pytest.mark.asyncio
    async def test_notify_event_caps_long_context(self, mock_supervisor, mock_db):
        """The last_response in the notify event is capped at 2000 chars."""
        graph = {
            "id": "cap-test",
            "version": 1,
            "nodes": {
                "start": {
                    "entry": True,
                    "prompt": "Go.",
                    "goto": "human",
                },
                "human": {
                    "prompt": "Review.",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
        }

        long_response = "x" * 5000
        responses = iter(["short.", long_response])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        event_bus = AsyncMock()
        event_bus.emit = AsyncMock()

        runner = PlaybookRunner(
            graph, {"type": "test"}, mock_supervisor, db=mock_db, event_bus=event_bus
        )
        await runner.run()

        notify_calls = [
            c for c in event_bus.emit.call_args_list if c.args[0] == "notify.playbook_run_paused"
        ]
        assert len(notify_calls) == 1
        last_resp = notify_calls[0].args[1].get("last_response", "")
        assert len(last_resp) <= 2000


# ---------------------------------------------------------------------------
# Discord notification handler routing
# ---------------------------------------------------------------------------


class TestDiscordPlaybookPausedHandler:
    """DiscordNotificationHandler routes notify.playbook_run_paused correctly."""

    @pytest.mark.asyncio
    async def test_handler_sends_message_on_paused_event(self):
        """Handler calls bot._send_message with embed and view."""
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        event = PlaybookRunPausedEvent(
            playbook_id="my-playbook",
            run_id="run-abc",
            node_id="review",
            last_response="Here is the analysis for your review.",
            running_seconds=30.5,
            tokens_used=800,
            project_id="proj-1",
        )
        await bus.emit("notify.playbook_run_paused", event.model_dump(mode="json"))

        bot._send_message.assert_called_once()
        call_kwargs = bot._send_message.call_args
        # Check project_id routing
        assert call_kwargs.kwargs.get("project_id") == "proj-1"
        # Check embed is present
        assert call_kwargs.kwargs.get("embed") is not None
        # Check view is present
        assert call_kwargs.kwargs.get("view") is not None
        # Check the plain-text message contains the run_id
        assert "run-abc" in call_kwargs.args[0]
        assert "my-playbook" in call_kwargs.args[0]

        handler.shutdown()

    @pytest.mark.asyncio
    async def test_handler_subscribes_to_paused_event(self):
        """The handler registers a subscription for notify.playbook_run_paused."""
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        assert len(bus._handlers.get("notify.playbook_run_paused", [])) > 0

        handler.shutdown()
        assert len(bus._handlers.get("notify.playbook_run_paused", [])) == 0

    @pytest.mark.asyncio
    async def test_handler_without_context(self):
        """Handler works when last_response is empty."""
        from src.discord.notification_handler import DiscordNotificationHandler

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        event = PlaybookRunPausedEvent(
            playbook_id="pb",
            run_id="r1",
            node_id="wait",
            last_response="",
        )
        await bus.emit("notify.playbook_run_paused", event.model_dump(mode="json"))

        bot._send_message.assert_called_once()
        handler.shutdown()


# ---------------------------------------------------------------------------
# Discord formatter tests
# ---------------------------------------------------------------------------


class TestDiscordPlaybookPausedFormatters:
    """Tests for format_playbook_paused and format_playbook_paused_embed."""

    def test_format_playbook_paused_plaintext(self):
        from src.discord.notifications import format_playbook_paused

        result = format_playbook_paused(
            playbook_id="my-pb",
            run_id="run-1",
            node_id="review",
        )
        assert "my-pb" in result
        assert "run-1" in result
        assert "review" in result
        assert "resume-playbook" in result.lower() or "resume" in result.lower()

    def test_format_playbook_paused_embed_with_context(self):
        from src.discord.notifications import format_playbook_paused_embed

        embed = format_playbook_paused_embed(
            playbook_id="my-pb",
            run_id="run-1",
            node_id="review",
            last_response="I analyzed the codebase and found 3 issues.",
            running_seconds=120.0,
            tokens_used=1500,
        )
        assert embed is not None
        assert embed.title is not None
        assert "Human Review" in embed.title or "Paused" in embed.title or "paused" in embed.title
        # The description should contain the context summary
        assert "analyzed" in embed.description or "context" in embed.description.lower()
        # Fields should contain metadata
        field_names = [f.name for f in embed.fields]
        assert any("Playbook" in n for n in field_names)
        assert any("Run" in n for n in field_names)

    def test_format_playbook_paused_embed_without_context(self):
        from src.discord.notifications import format_playbook_paused_embed

        embed = format_playbook_paused_embed(
            playbook_id="pb",
            run_id="r1",
            node_id="wait",
            last_response="",
        )
        assert embed is not None
        # Should mention no context available
        assert "no context" in embed.description.lower() or "summary" in embed.description.lower()

    def test_format_embed_duration_formatting(self):
        from src.discord.notifications import format_playbook_paused_embed

        # Seconds (< 60)
        embed = format_playbook_paused_embed(
            playbook_id="pb",
            run_id="r1",
            node_id="n1",
            running_seconds=45.3,
        )
        field_values = [f.value for f in embed.fields]
        duration_fields = [v for v in field_values if "45" in v or "s" in v]
        assert len(duration_fields) > 0

        # Minutes (>= 60)
        embed2 = format_playbook_paused_embed(
            playbook_id="pb",
            run_id="r1",
            node_id="n1",
            running_seconds=125.0,
        )
        field_values2 = [f.value for f in embed2.fields]
        duration_fields2 = [v for v in field_values2 if "2m" in v]
        assert len(duration_fields2) > 0

    def test_format_embed_truncates_long_context(self):
        from src.discord.notifications import format_playbook_paused_embed

        long_context = "A" * 3000
        embed = format_playbook_paused_embed(
            playbook_id="pb",
            run_id="r1",
            node_id="n1",
            last_response=long_context,
        )
        # Discord description limit is 4096
        assert len(embed.description) <= 4096


# ---------------------------------------------------------------------------
# PlaybookResumeView and Modal tests
# ---------------------------------------------------------------------------


class TestPlaybookResumeView:
    """Tests for PlaybookResumeView interactive button."""

    def test_view_creation(self):
        from src.discord.notifications import PlaybookResumeView

        view = PlaybookResumeView("run-123", handler=MagicMock())
        assert view.run_id == "run-123"
        assert view.timeout == 86400
        # Should have at least 2 buttons
        assert len(view.children) >= 2

    def test_modal_creation(self):
        from src.discord.notifications import PlaybookResumeModal

        modal = PlaybookResumeModal("run-123", handler=MagicMock())
        assert modal.run_id == "run-123"


# ---------------------------------------------------------------------------
# Telegram formatter tests
# ---------------------------------------------------------------------------


class TestTelegramPlaybookPausedFormatter:
    """Tests for Telegram MarkdownV2 playbook-paused notification."""

    def test_format_playbook_paused_basic(self):
        from src.telegram.notifications import format_playbook_paused

        result = format_playbook_paused(
            playbook_id="my-pb",
            run_id="run-1",
            node_id="review",
        )
        assert "my-pb" in result
        assert "run-1" in result
        assert "review" in result
        # Should mention "Human Review" or "Awaiting"
        assert "Human Review" in result or "Awaiting" in result

    def test_format_with_context(self):
        from src.telegram.notifications import format_playbook_paused

        result = format_playbook_paused(
            playbook_id="pb",
            run_id="r1",
            node_id="wait",
            last_response="Found 3 critical issues in the codebase.",
            running_seconds=60.0,
            tokens_used=1000,
        )
        assert "3 critical issues" in result
        assert "1,000" in result or "1000" in result
        assert "1m" in result

    def test_format_without_context(self):
        from src.telegram.notifications import format_playbook_paused

        result = format_playbook_paused(
            playbook_id="pb",
            run_id="r1",
            node_id="wait",
            last_response="",
        )
        # Should indicate no context available
        assert "No context" in result or "no context" in result

    def test_format_long_context_truncation(self):
        from src.telegram.notifications import (
            TELEGRAM_MESSAGE_LIMIT,
            format_playbook_paused,
        )

        long_context = "B" * 5000
        result = format_playbook_paused(
            playbook_id="pb",
            run_id="r1",
            node_id="wait",
            last_response=long_context,
        )
        # Should be within Telegram message limit
        assert len(result) <= TELEGRAM_MESSAGE_LIMIT

    def test_format_resume_command_hint(self):
        from src.telegram.notifications import format_playbook_paused

        result = format_playbook_paused(
            playbook_id="pb",
            run_id="run-abc",
            node_id="wait",
        )
        assert "resume" in result.lower()
        assert "run-abc" in result


# ---------------------------------------------------------------------------
# Event schema test
# ---------------------------------------------------------------------------


class TestPlaybookPausedEventSchema:
    """Verify playbook.run.paused is registered in event schemas."""

    def test_schema_registered(self):
        from src.event_schemas import EVENT_SCHEMAS

        assert "playbook.run.paused" in EVENT_SCHEMAS
        schema = EVENT_SCHEMAS["playbook.run.paused"]
        assert "playbook_id" in schema["required"]
        assert "run_id" in schema["required"]
        assert "node_id" in schema["required"]
        assert "last_response" in schema["optional"]
        assert "running_seconds" in schema["optional"]
        assert "tokens_used" in schema["optional"]


# ---------------------------------------------------------------------------
# End-to-end integration test
# ---------------------------------------------------------------------------


class TestEndToEndPausedNotification:
    """Full pipeline: PlaybookRunner pause → EventBus → DiscordNotificationHandler."""

    @pytest.mark.asyncio
    async def test_full_pipeline(self, mock_supervisor, human_review_graph, mock_db):
        """Verify end-to-end from playbook runner to Discord message."""
        from src.discord.notification_handler import DiscordNotificationHandler

        responses = iter(["Analysis done.", "Here is the review summary."])
        mock_supervisor.chat.side_effect = lambda **kw: next(responses)

        bus = EventBus()
        bot = _make_mock_bot()
        handler = DiscordNotificationHandler(bot, bus)

        event_data = {"type": "test", "project_id": "proj-1"}
        runner = PlaybookRunner(
            human_review_graph, event_data, mock_supervisor, db=mock_db, event_bus=bus
        )
        result = await runner.run()

        assert result.status == "paused"

        # The Discord bot should have received two messages: one for the run
        # starting, one for the pause for human review.
        assert bot._send_message.call_count == 2
        start_call, paused_call = bot._send_message.call_args_list
        assert start_call.kwargs.get("project_id") == "proj-1"
        assert "Started" in start_call.args[0] or "review-playbook" in start_call.args[0]

        # The pause message carries the context embed the reviewer needs.
        assert paused_call.kwargs.get("project_id") == "proj-1"
        assert paused_call.kwargs.get("embed") is not None
        assert "review-playbook" in paused_call.args[0]

        handler.shutdown()
