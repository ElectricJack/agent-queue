"""Tests for the Telegram bot package.

Covers:
- TelegramMessagingAdapter ABC compliance
- TelegramBot authorization, message routing, and chat management
- Telegram notification formatting (MarkdownV2 escaping, splitting)
- Command parsing and routing to CommandHandler
- Integration: message received -> supervisor invoked -> response sent
"""

from __future__ import annotations

import asyncio
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import AppConfig, TelegramConfig
from src.messaging.base import MessagingAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> AppConfig:
    """Create a minimal AppConfig for Telegram testing."""
    tg = TelegramConfig(
        bot_token="123:FAKE_TOKEN",
        chat_id="-100999",
        authorized_users=["111", "222"],
        per_project_chats={"proj1": "-100888"},
        use_topics=True,
    )
    config = AppConfig(
        data_dir=tempfile.mkdtemp(), messaging_platform="telegram", telegram=tg
    )
    for k, v in overrides.items():
        setattr(config, k, v)
    return config


def _make_orchestrator() -> MagicMock:
    """Create a mock orchestrator."""
    orch = MagicMock()
    orch.llm_logger = None
    orch.hooks = None
    orch.set_notify_callback = MagicMock()
    orch.set_create_thread_callback = MagicMock()
    orch.set_command_handler = MagicMock()
    orch.set_supervisor = MagicMock()
    return orch


# ---------------------------------------------------------------------------
# Notification formatting
# ---------------------------------------------------------------------------


class TestMarkdownV2Escaping:
    """Test MarkdownV2 escaping utilities."""

    def test_escape_special_chars(self):
        from src.telegram.notifications import escape_markdown

        result = escape_markdown("Hello *world* [test]")
        assert "\\*" in result
        assert "\\[" in result
        assert "\\]" in result

    def test_escape_all_special_chars(self):
        from src.telegram.notifications import escape_markdown

        # All special chars from the spec
        for ch in "_*[]()~`>#+-=|{}.!":
            assert f"\\{ch}" in escape_markdown(ch)

    def test_escape_plain_text_unchanged(self):
        from src.telegram.notifications import escape_markdown

        result = escape_markdown("Hello world 123")
        assert result == "Hello world 123"

    def test_bold(self):
        from src.telegram.notifications import bold

        result = bold("test")
        assert result == "*test*"

    def test_bold_with_special_chars(self):
        from src.telegram.notifications import bold

        result = bold("hello [world]")
        assert result == "*hello \\[world\\]*"

    def test_italic(self):
        from src.telegram.notifications import italic

        result = italic("test")
        assert result == "_test_"

    def test_code(self):
        from src.telegram.notifications import code

        result = code("var = 1")
        assert result == "`var = 1`"

    def test_code_block(self):
        from src.telegram.notifications import code_block

        result = code_block("print('hi')", "python")
        assert result == "```python\nprint('hi')\n```"

    def test_link(self):
        from src.telegram.notifications import link

        result = link("click here", "https://example.com")
        assert result == "[click here](https://example.com)"


class TestMessageSplitting:
    """Test message splitting for Telegram's 4096 char limit."""

    def test_short_message_not_split(self):
        from src.telegram.notifications import split_message

        result = split_message("short message")
        assert result == ["short message"]

    def test_long_message_split_on_newlines(self):
        from src.telegram.notifications import split_message

        # Create a message that exceeds limit
        lines = ["line " * 50 + "\n" for _ in range(30)]
        text = "".join(lines)
        result = split_message(text, limit=500)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 500

    def test_single_long_line_hard_split(self):
        from src.telegram.notifications import split_message

        text = "x" * 1000
        result = split_message(text, limit=300)
        assert len(result) == 4  # ceil(1000/300) = 4
        assert all(len(chunk) <= 300 for chunk in result)

    def test_empty_message(self):
        from src.telegram.notifications import split_message

        result = split_message("")
        assert result == [""]


class TestNotificationFormatters:
    """Test task notification formatters."""

    def test_format_server_started(self):
        from src.telegram.notifications import format_server_started

        result = format_server_started()
        assert "AgentQueue" in result
        assert "online" in result

    def test_format_task_started(self):
        from src.telegram.notifications import format_task_started

        task = MagicMock(title="Fix bug #123", id="t1", task_type="bugfix")
        project = MagicMock(name="my-project", id="p1")
        result = format_task_started(task, project)
        assert "Task Started" in result
        assert "Fix bug" in result
        assert "my\\-project" in result  # escaped

    def test_format_task_completed(self):
        from src.telegram.notifications import format_task_completed

        task = MagicMock(title="Add feature", id="t2")
        project = MagicMock(name="proj", id="p1")
        result = format_task_completed(task, project, summary="All tests pass")
        assert "Completed" in result
        assert "All tests pass" in result

    def test_format_task_failed(self):
        from src.telegram.notifications import format_task_failed

        task = MagicMock(title="Deploy", id="t3")
        project = MagicMock(name="proj", id="p1")
        result = format_task_failed(task, project, error="Timeout exceeded")
        assert "Failed" in result
        assert "Timeout exceeded" in result

    def test_format_embed_as_text(self):
        from src.telegram.notifications import format_embed_as_text

        result = format_embed_as_text(
            title="Server Online",
            description="All systems go",
            fields=[("Status", "Running"), ("Tasks", "5")],
            footer="Updated just now",
        )
        assert "*Server Online*" in result  # bold
        assert "All systems go" in result
        assert "*Status*" in result
        assert "Running" in result
        assert "_Updated just now_" in result  # italic

    def test_format_embed_with_url(self):
        from src.telegram.notifications import format_embed_as_text

        result = format_embed_as_text(
            title="PR Ready",
            url="https://github.com/org/repo/pull/1",
        )
        assert "[PR Ready]" in result
        assert "https://github.com/org/repo/pull/1" in result


class TestInlineKeyboard:
    """Test inline keyboard helpers."""

    def test_make_inline_keyboard(self):
        from src.telegram.notifications import make_inline_keyboard

        buttons = [("Retry", "retry_task:id=123"), ("Skip", "skip_task:id=123")]
        result = make_inline_keyboard(buttons)
        assert len(result) == 2
        assert result[0][0]["text"] == "Retry"
        assert result[0][0]["callback_data"] == "retry_task:id=123"
        assert result[1][0]["text"] == "Skip"


# ---------------------------------------------------------------------------
# TelegramMessagingAdapter ABC compliance
# ---------------------------------------------------------------------------


class TestTelegramAdapterABC:
    """Verify TelegramMessagingAdapter implements MessagingAdapter correctly."""

    @patch("src.telegram.bot.TelegramBot")
    def test_is_messaging_adapter(self, mock_bot_cls):
        """Adapter is a subclass of MessagingAdapter."""
        from src.telegram.adapter import TelegramMessagingAdapter

        assert issubclass(TelegramMessagingAdapter, MessagingAdapter)

    @patch("src.telegram.bot.TelegramBot")
    def test_instantiation(self, mock_bot_cls):
        """Adapter can be instantiated with config and orchestrator."""
        from src.telegram.adapter import TelegramMessagingAdapter

        config = _make_config()
        orch = _make_orchestrator()
        adapter = TelegramMessagingAdapter(config, orch)
        assert isinstance(adapter, MessagingAdapter)

    @patch("src.telegram.bot.TelegramBot")
    async def test_start_delegates(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        await adapter.start()
        mock_bot.start.assert_awaited_once()

    @patch("src.telegram.bot.TelegramBot")
    async def test_wait_until_ready_delegates(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        await adapter.wait_until_ready()
        mock_bot.wait_until_ready.assert_awaited_once()

    @patch("src.telegram.bot.TelegramBot")
    async def test_close_delegates(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        await adapter.close()
        mock_bot.stop.assert_awaited_once()

    @patch("src.telegram.bot.TelegramBot")
    async def test_send_message_delegates(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        await adapter.send_message("hello", "proj1", embed="fake_embed")
        mock_bot.send_notification.assert_awaited_once_with(
            "hello", "proj1", embed="fake_embed", view=None
        )

    @patch("src.telegram.bot.TelegramBot")
    async def test_create_task_thread_delegates(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot.create_task_topic.return_value = (AsyncMock(), AsyncMock())
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        result = await adapter.create_task_thread(
            "Test task", "Agent working on: Test task", "proj1", "t1"
        )
        assert result is not None
        assert len(result) == 2

    @patch("src.telegram.bot.TelegramBot")
    async def test_create_task_thread_returns_noop_on_failure(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = AsyncMock()
        mock_bot.create_task_topic.return_value = None
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        result = await adapter.create_task_thread("Test", "Agent working on: Test", "p1", "t1")
        # Should return None when topic creation failed
        assert result is None

    @patch("src.telegram.bot.TelegramBot")
    def test_get_command_handler(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = MagicMock()
        mock_bot.handler = MagicMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        assert adapter.get_command_handler() is mock_bot.handler

    @patch("src.telegram.bot.TelegramBot")
    def test_get_supervisor(self, mock_bot_cls):
        from src.telegram.adapter import TelegramMessagingAdapter

        mock_bot = MagicMock()
        mock_bot.supervisor = MagicMock()
        mock_bot_cls.return_value = mock_bot

        adapter = TelegramMessagingAdapter(_make_config(), _make_orchestrator())
        assert adapter.get_supervisor() is mock_bot.supervisor


# ---------------------------------------------------------------------------
# TelegramBot unit tests
# ---------------------------------------------------------------------------


class TestTelegramBotAuth:
    """Test authorization logic."""

    def test_authorized_user(self):
        """User in authorized_users list passes."""
        from src.telegram.bot import TelegramBot

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot.config = _make_config()
            assert bot._is_authorized(111) is True
            assert bot._is_authorized("222") is True

    def test_unauthorized_user(self):
        """User not in authorized_users list is rejected."""
        from src.telegram.bot import TelegramBot

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot.config = _make_config()
            assert bot._is_authorized(999) is False

    def test_empty_authorized_list_allows_all(self):
        """When authorized_users is empty, all users are allowed."""
        from src.telegram.bot import TelegramBot

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            config = _make_config()
            config.telegram.authorized_users = []
            bot.config = config
            assert bot._is_authorized(999) is True


class TestTelegramBotChatRouting:
    """Test per-project chat routing."""

    def _make_bot(self) -> Any:
        from src.telegram.bot import TelegramBot

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot.config = _make_config()
            bot._main_chat_id = -100999
            bot._project_chats = {"proj1": -100888}
            bot._chat_to_project = {-100888: "proj1"}
            return bot

    def test_get_chat_id_project(self):
        bot = self._make_bot()
        assert bot._get_chat_id("proj1") == -100888

    def test_get_chat_id_fallback(self):
        bot = self._make_bot()
        assert bot._get_chat_id("unknown_proj") == -100999

    def test_get_chat_id_none(self):
        bot = self._make_bot()
        assert bot._get_chat_id(None) == -100999

    def test_update_project_chat(self):
        bot = self._make_bot()
        bot.update_project_chat("proj2", -100777)
        assert bot._get_chat_id("proj2") == -100777
        assert bot._chat_to_project[-100777] == "proj2"

    def test_clear_project_chats(self):
        bot = self._make_bot()
        bot.clear_project_chats("proj1")
        assert bot._get_chat_id("proj1") == -100999  # falls back to main
        assert -100888 not in bot._chat_to_project

    def test_update_replaces_old_chat(self):
        bot = self._make_bot()
        bot.update_project_chat("proj1", -100777)
        assert bot._get_chat_id("proj1") == -100777
        # Old chat ID should be removed from reverse lookup
        assert -100888 not in bot._chat_to_project


class TestTelegramBotMessageHistory:
    """Test message buffer and history building."""

    def _make_bot(self) -> Any:
        import collections as c
        from src.telegram.bot import TelegramBot, CachedMessage

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot._chat_buffers = {}
            bot._buffer_last_access = {}
            return bot

    def test_append_to_buffer_creates_deque(self):
        from src.telegram.bot import CachedMessage

        bot = self._make_bot()
        msg = CachedMessage(
            message_id=1,
            author_name="Alice",
            is_bot=False,
            content="Hello",
            created_at=1000.0,
            chat_id=100,
        )
        bot._append_to_buffer(100, msg)
        assert 100 in bot._chat_buffers
        assert len(bot._chat_buffers[100]) == 1

    def test_build_message_history(self):
        from src.telegram.bot import CachedMessage

        bot = self._make_bot()
        bot._append_to_buffer(
            100,
            CachedMessage(
                message_id=1,
                author_name="Alice",
                is_bot=False,
                content="What's the status?",
                created_at=1000.0,
                chat_id=100,
            ),
        )
        bot._append_to_buffer(
            100,
            CachedMessage(
                message_id=0,
                author_name="AgentQueue",
                is_bot=True,
                content="All systems running.",
                created_at=1001.0,
                chat_id=100,
            ),
        )
        history = bot._build_message_history(100)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert "[Alice]" in history[0]["content"]
        assert history[1]["role"] == "assistant"

    def test_empty_buffer_returns_empty(self):
        bot = self._make_bot()
        assert bot._build_message_history(999) == []


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


class TestTelegramCommands:
    """Test command parsing and routing to CommandHandler."""

    async def test_create_task_routes_to_handler(self):
        from src.telegram.commands import cmd_create_task

        handler = AsyncMock()
        handler.execute.return_value = {"task_id": "t1", "title": "Do stuff"}

        update = MagicMock()
        update.effective_message = AsyncMock()
        context = MagicMock()
        context.args = ["Fix", "the", "login", "bug"]

        await cmd_create_task(update, context, handler)
        handler.execute.assert_awaited_once_with(
            "create_task", {"description": "Fix the login bug"}
        )

    async def test_create_task_no_args(self):
        from src.telegram.commands import cmd_create_task

        handler = AsyncMock()
        update = MagicMock()
        update.effective_message = AsyncMock()
        context = MagicMock()
        context.args = []

        await cmd_create_task(update, context, handler)
        handler.execute.assert_not_awaited()
        update.effective_message.reply_text.assert_awaited_once()

    async def test_list_tasks_routes_to_handler(self):
        from src.telegram.commands import cmd_list_tasks

        handler = AsyncMock()
        handler.execute.return_value = {"tasks": []}

        update = MagicMock()
        update.effective_message = AsyncMock()
        context = MagicMock()
        context.args = ["ready"]

        await cmd_list_tasks(update, context, handler)
        handler.execute.assert_awaited_once_with("list_tasks", {"status": "ready"})

    async def test_status_command(self):
        from src.telegram.commands import cmd_status

        handler = AsyncMock()
        handler.execute.return_value = {"agents": 2, "tasks": 5}

        update = MagicMock()
        update.effective_message = AsyncMock()
        context = MagicMock()
        context.args = []

        await cmd_status(update, context, handler)
        handler.execute.assert_awaited_once_with("status", {})

    async def test_error_result_formatting(self):
        from src.telegram.commands import _send_result

        update = MagicMock()
        update.effective_message = AsyncMock()

        await _send_result(update, {"error": "Task not found"}, "Failed")
        call_args = update.effective_message.reply_text.call_args
        assert "Error" in call_args[0][0]
        assert "Task not found" in call_args[0][0]


# ---------------------------------------------------------------------------
# Integration: message -> supervisor -> response
# ---------------------------------------------------------------------------


class TestTelegramBotIntegration:
    """Integration tests for the message -> supervisor -> response flow."""

    async def test_process_chat_message_calls_supervisor(self):
        """Verify that an incoming message triggers Supervisor.chat()."""
        from src.telegram.bot import TelegramBot, CachedMessage

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot._chat_buffers = {}
            bot._buffer_last_access = {}
            bot._application = MagicMock()
            bot._application.bot = AsyncMock()
            bot.handler = MagicMock()
            bot.handler._active_project_id = None

            # Mock supervisor
            bot._supervisor = MagicMock()
            bot._supervisor._provider = MagicMock()  # truthy = configured
            bot._supervisor.chat = AsyncMock(return_value="I'm on it!")

            # Seed buffer with a user message
            bot._append_to_buffer(
                100,
                CachedMessage(
                    message_id=1,
                    author_name="Alice",
                    is_bot=False,
                    content="What is the status?",
                    created_at=1000.0,
                    chat_id=100,
                ),
            )

            await bot._process_chat_message(chat_id=100, project_id="proj1", user_name="Alice")

            bot._supervisor.chat.assert_awaited_once()
            # Bot should have sent the response
            bot._application.bot.send_message.assert_awaited()

    async def test_process_chat_message_no_provider(self):
        """Verify graceful handling when no LLM provider is configured."""
        from src.telegram.bot import TelegramBot, CachedMessage

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot._chat_buffers = {}
            bot._buffer_last_access = {}
            bot._application = MagicMock()
            bot._application.bot = AsyncMock()

            bot._supervisor = MagicMock()
            bot._supervisor._provider = None  # No provider

            await bot._process_chat_message(chat_id=100, project_id=None, user_name="Alice")

            bot._application.bot.send_message.assert_awaited()
            call_text = bot._application.bot.send_message.call_args[1]["text"]
            assert "LLM provider" in call_text or "ANTHROPIC_API_KEY" in call_text


# ---------------------------------------------------------------------------
# Views: inline keyboard builders
# ---------------------------------------------------------------------------


class TestCallbackDataParsing:
    """Test callback_data encoding and parsing round-trip."""

    def test_parse_simple_action(self):
        from src.telegram.views import parse_callback_data

        action, args = parse_callback_data("stop_task")
        assert action == "stop_task"
        assert args == {}

    def test_parse_action_with_args(self):
        from src.telegram.views import parse_callback_data

        action, args = parse_callback_data("restart_task:task_id=abc123")
        assert action == "restart_task"
        assert args == {"task_id": "abc123"}

    def test_parse_action_with_multiple_args(self):
        from src.telegram.views import parse_callback_data

        action, args = parse_callback_data("some_cmd:a=1,b=two")
        assert action == "some_cmd"
        assert args == {"a": "1", "b": "two"}

    def test_round_trip(self):
        from src.telegram.views import _make_callback_data, parse_callback_data

        data = _make_callback_data("approve_task", task_id="xyz789")
        action, args = parse_callback_data(data)
        assert action == "approve_task"
        assert args["task_id"] == "xyz789"

    def test_make_callback_data_no_args(self):
        from src.telegram.views import _make_callback_data

        assert _make_callback_data("noop") == "noop"


class TestTaskStartedKeyboard:
    """Test task_started_keyboard builder."""

    def test_has_view_context_and_stop_buttons(self):
        from src.telegram.views import task_started_keyboard

        kb = task_started_keyboard("t1")
        buttons = kb.inline_keyboard
        assert len(buttons) == 1
        # First button: View Context
        assert "View Context" in buttons[0][0].text
        assert "view_context" in buttons[0][0].callback_data
        assert "t1" in buttons[0][0].callback_data
        # Second button: Stop Task
        assert "Stop" in buttons[0][1].text
        assert "stop_task" in buttons[0][1].callback_data
        assert "t1" in buttons[0][1].callback_data


class TestTaskFailedKeyboard:
    """Test task_failed_keyboard builder."""

    def test_has_retry_skip_view_error_buttons(self):
        from src.telegram.views import task_failed_keyboard

        kb = task_failed_keyboard("t2")
        buttons = kb.inline_keyboard
        # Row 0: Retry + Skip, Row 1: View Error
        assert len(buttons) == 2
        labels = [b.text for row in buttons for b in row]
        assert any("Retry" in l for l in labels)
        assert any("Skip" in l for l in labels)
        assert any("View Error" in l for l in labels)

    def test_callback_data_contains_task_id(self):
        from src.telegram.views import task_failed_keyboard

        kb = task_failed_keyboard("my-task-99")
        for row in kb.inline_keyboard:
            for btn in row:
                assert "my-task-99" in btn.callback_data


class TestTaskApprovalKeyboard:
    """Test task_approval_keyboard builder."""

    def test_has_approve_restart_buttons(self):
        from src.telegram.views import task_approval_keyboard

        kb = task_approval_keyboard("t3")
        buttons = kb.inline_keyboard
        assert len(buttons) == 1  # single row
        labels = [b.text for b in buttons[0]]
        assert any("Approve" in l for l in labels)
        assert any("Restart" in l for l in labels)

    def test_approve_callback_data(self):
        from src.telegram.views import task_approval_keyboard

        kb = task_approval_keyboard("t3")
        approve_btn = kb.inline_keyboard[0][0]
        assert "approve_task" in approve_btn.callback_data
        assert "t3" in approve_btn.callback_data


class TestTaskBlockedKeyboard:
    """Test task_blocked_keyboard builder."""

    def test_has_restart_skip_buttons(self):
        from src.telegram.views import task_blocked_keyboard

        kb = task_blocked_keyboard("t4")
        buttons = kb.inline_keyboard
        assert len(buttons) == 1
        labels = [b.text for b in buttons[0]]
        assert any("Restart" in l for l in labels)
        assert any("Skip" in l for l in labels)


class TestAgentQuestionKeyboard:
    """Test agent_question_keyboard builder."""

    def test_has_reply_skip_buttons(self):
        from src.telegram.views import agent_question_keyboard

        kb = agent_question_keyboard("t5")
        buttons = kb.inline_keyboard
        assert len(buttons) == 1
        labels = [b.text for b in buttons[0]]
        assert any("Reply" in l for l in labels)
        assert any("Skip" in l for l in labels)

    def test_reply_uses_pseudo_action(self):
        from src.telegram.views import agent_question_keyboard

        kb = agent_question_keyboard("t5")
        reply_btn = kb.inline_keyboard[0][0]
        assert "agent_reply_prompt" in reply_btn.callback_data


class TestPlanApprovalKeyboard:
    """Test plan_approval_keyboard builder."""

    def test_has_approve_delete_buttons(self):
        from src.telegram.views import plan_approval_keyboard

        kb = plan_approval_keyboard("t6")
        buttons = kb.inline_keyboard
        assert len(buttons) == 1
        labels = [b.text for b in buttons[0]]
        assert any("Approve" in l for l in labels)
        assert any("Delete" in l for l in labels)

    def test_callback_data_actions(self):
        from src.telegram.views import plan_approval_keyboard, parse_callback_data

        kb = plan_approval_keyboard("t6")
        approve_btn = kb.inline_keyboard[0][0]
        delete_btn = kb.inline_keyboard[0][1]
        action1, args1 = parse_callback_data(approve_btn.callback_data)
        action2, args2 = parse_callback_data(delete_btn.callback_data)
        assert action1 == "approve_plan"
        assert args1["task_id"] == "t6"
        assert action2 == "delete_plan"
        assert args2["task_id"] == "t6"


class TestNotificationActionsKeyboard:
    """Test notification_actions_keyboard builder."""

    def test_empty_actions_returns_none(self):
        from src.telegram.views import notification_actions_keyboard

        assert notification_actions_keyboard([]) is None
        assert notification_actions_keyboard(None) is None

    def test_single_action(self):
        from src.telegram.views import notification_actions_keyboard

        action = MagicMock(label="Do it", action_id="do_it", args={"task_id": "t1"})
        kb = notification_actions_keyboard([action])
        assert kb is not None
        assert len(kb.inline_keyboard) == 1
        assert kb.inline_keyboard[0][0].text == "Do it"

    def test_multiple_actions_row_grouping(self):
        from src.telegram.views import notification_actions_keyboard

        actions = [MagicMock(label=f"Action {i}", action_id=f"act_{i}", args={}) for i in range(5)]
        kb = notification_actions_keyboard(actions)
        # 5 actions -> 2 rows of 3 + 1 row of 2
        assert len(kb.inline_keyboard) == 2
        assert len(kb.inline_keyboard[0]) == 3
        assert len(kb.inline_keyboard[1]) == 2


class TestDisableKeyboardAfterAction:
    """Test disable_keyboard_after_action utility."""

    async def test_edits_message_text(self):
        from src.telegram.views import disable_keyboard_after_action

        query = MagicMock()
        query.message.text = "Original notification text"
        query.edit_message_text = AsyncMock()

        await disable_keyboard_after_action(query, "Task restarted")

        query.edit_message_text.assert_awaited_once()
        call_args = query.edit_message_text.call_args
        assert "Original notification text" in call_args[1]["text"]
        assert "Task restarted" in call_args[1]["text"]
        assert call_args[1]["reply_markup"] is None

    async def test_fallback_on_edit_failure(self):
        from src.telegram.views import disable_keyboard_after_action

        query = MagicMock()
        query.message.text = "Some text"
        query.edit_message_text = AsyncMock(side_effect=Exception("API error"))
        query.edit_message_reply_markup = AsyncMock()

        await disable_keyboard_after_action(query, "Done")

        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


# ---------------------------------------------------------------------------
# Callback query handler integration
# ---------------------------------------------------------------------------


class TestCallbackQueryHandler:
    """Test the bot's _handle_callback_query method."""

    def _make_bot(self) -> Any:
        from src.telegram.bot import TelegramBot

        with patch("src.telegram.bot.TelegramBot.__init__", return_value=None):
            bot = TelegramBot.__new__(TelegramBot)
            bot.config = _make_config()
            bot.handler = AsyncMock()
            return bot

    async def test_unauthorized_user_rejected(self):
        bot = self._make_bot()
        update = MagicMock()
        query = MagicMock()
        query.from_user.id = 999  # Not in authorized_users
        query.answer = AsyncMock()
        update.callback_query = query

        await bot._handle_callback_query(update, MagicMock())

        query.answer.assert_awaited_once()
        assert "Unauthorized" in query.answer.call_args[0][0]

    async def test_empty_callback_data(self):
        bot = self._make_bot()
        update = MagicMock()
        query = MagicMock()
        query.from_user.id = 111  # Authorized
        query.data = ""
        query.answer = AsyncMock()
        update.callback_query = query

        await bot._handle_callback_query(update, MagicMock())

        query.answer.assert_awaited_once()
        bot.handler.execute.assert_not_awaited()

    async def test_agent_reply_prompt_pseudo_action(self):
        bot = self._make_bot()
        update = MagicMock()
        query = MagicMock()
        query.from_user.id = 111
        query.data = "agent_reply_prompt:task_id=t5"
        query.answer = AsyncMock()
        update.callback_query = query

        await bot._handle_callback_query(update, MagicMock())

        query.answer.assert_awaited_once()
        # show_alert=True for the prompt
        assert query.answer.call_args[1].get("show_alert") is True
        # CommandHandler should NOT be called for pseudo-actions
        bot.handler.execute.assert_not_awaited()

    async def test_successful_command_execution(self):
        bot = self._make_bot()
        bot.handler.execute.return_value = {"status": "ok"}

        update = MagicMock()
        query = MagicMock()
        query.from_user.id = 111
        query.data = "restart_task:task_id=abc123"
        query.answer = AsyncMock()
        query.message.text = "Task failed: something broke"
        query.edit_message_text = AsyncMock()
        update.callback_query = query

        await bot._handle_callback_query(update, MagicMock())

        bot.handler.execute.assert_awaited_once_with("restart_task", {"task_id": "abc123"})
        query.edit_message_text.assert_awaited_once()
        edited_text = query.edit_message_text.call_args[1]["text"]
        assert "Restart Task" in edited_text

    async def test_command_error_shown(self):
        bot = self._make_bot()
        bot.handler.execute.return_value = {"error": "Task not found"}

        update = MagicMock()
        query = MagicMock()
        query.from_user.id = 222
        query.data = "skip_task:task_id=nonexistent"
        query.answer = AsyncMock()
        query.message.text = "Task blocked"
        query.edit_message_text = AsyncMock()
        update.callback_query = query

        await bot._handle_callback_query(update, MagicMock())

        edited_text = query.edit_message_text.call_args[1]["text"]
        assert "Error" in edited_text
        assert "Task not found" in edited_text

    async def test_exception_during_execution(self):
        bot = self._make_bot()
        bot.handler.execute.side_effect = RuntimeError("DB down")

        update = MagicMock()
        query = MagicMock()
        query.from_user.id = 111
        query.data = "approve_task:task_id=t9"
        query.answer = AsyncMock()
        query.message.text = "Awaiting approval"
        query.edit_message_text = AsyncMock()
        update.callback_query = query

        await bot._handle_callback_query(update, MagicMock())

        edited_text = query.edit_message_text.call_args[1]["text"]
        assert "Error" in edited_text
        assert "DB down" in edited_text
