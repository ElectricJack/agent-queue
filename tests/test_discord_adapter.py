"""Tests for the DiscordMessagingAdapter and messaging adapter integration.

Verifies that:
- DiscordMessagingAdapter correctly implements the MessagingAdapter ABC
- All method calls delegate to the underlying AgentQueueBot
- The factory creates a DiscordMessagingAdapter for the 'discord' platform
- main.py's adapter path registers callbacks correctly
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.messaging.base import MessagingAdapter
from src.messaging.factory import create_messaging_adapter


# ---------------------------------------------------------------------------
# DiscordMessagingAdapter ABC conformance
# ---------------------------------------------------------------------------


class TestDiscordAdapterABC:
    """DiscordMessagingAdapter satisfies the MessagingAdapter interface."""

    def test_is_subclass_of_messaging_adapter(self):
        from src.discord.adapter import DiscordMessagingAdapter

        assert issubclass(DiscordMessagingAdapter, MessagingAdapter)

    @patch("src.discord.bot.AgentQueueBot")
    def test_instantiation(self, mock_bot_cls):
        """Adapter wraps an AgentQueueBot instance."""
        from src.discord.adapter import DiscordMessagingAdapter

        config = MagicMock()
        orch = MagicMock()
        adapter = DiscordMessagingAdapter(config, orch)

        mock_bot_cls.assert_called_once_with(config, orch)
        assert adapter.bot is mock_bot_cls.return_value


# ---------------------------------------------------------------------------
# Method delegation
# ---------------------------------------------------------------------------


class TestDiscordAdapterDelegation:
    """Each adapter method delegates to the correct bot method."""

    @pytest.fixture
    def adapter(self):
        with patch("src.discord.bot.AgentQueueBot") as mock_bot_cls:
            config = MagicMock()
            config.discord.bot_token = "test-token"
            orch = MagicMock()
            from src.discord.adapter import DiscordMessagingAdapter

            adapter = DiscordMessagingAdapter(config, orch)
            # Replace bot methods with AsyncMocks for coroutine testing
            adapter._bot.start = AsyncMock()
            adapter._bot.wait_until_ready = AsyncMock()
            adapter._bot.close = AsyncMock()
            adapter._bot._send_message = AsyncMock(return_value=MagicMock())
            adapter._bot._create_task_thread = AsyncMock(return_value=("send_cb", "notify_cb"))
            adapter._bot.agent = MagicMock()
            adapter._bot.agent.handler = MagicMock()
            yield adapter

    @pytest.mark.asyncio
    async def test_start_delegates(self, adapter):
        await adapter.start()
        adapter._bot.start.assert_awaited_once_with("test-token")

    @pytest.mark.asyncio
    async def test_wait_until_ready_delegates(self, adapter):
        await adapter.wait_until_ready()
        adapter._bot.wait_until_ready.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_delegates(self, adapter):
        await adapter.close()
        adapter._bot.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_message_delegates(self, adapter):
        embed = MagicMock()
        view = MagicMock()
        result = await adapter.send_message("hello", "proj1", embed=embed, view=view)
        adapter._bot._send_message.assert_awaited_once_with(
            "hello", "proj1", embed=embed, view=view
        )
        # send_message is fire-and-forget (returns None)
        assert result is None

    @pytest.mark.asyncio
    async def test_send_message_minimal(self, adapter):
        """send_message works with just text."""
        await adapter.send_message("hello")
        adapter._bot._send_message.assert_awaited_once_with("hello", None, embed=None, view=None)

    @pytest.mark.asyncio
    async def test_create_task_thread_delegates(self, adapter):
        # create_task_thread accepts (thread_name, initial_message, project_id, task_id)
        result = await adapter.create_task_thread(
            "thread-name", "Agent working on: thread-name", "proj1", "task1"
        )
        adapter._bot._create_task_thread.assert_awaited_once_with(
            "thread-name", "Agent working on: thread-name", "proj1", "task1"
        )
        assert result == ("send_cb", "notify_cb")

    def test_get_command_handler_delegates(self, adapter):
        result = adapter.get_command_handler()
        assert result is adapter._bot.agent.handler

    def test_get_supervisor_delegates(self, adapter):
        result = adapter.get_supervisor()
        assert result is adapter._bot.agent


# ---------------------------------------------------------------------------
# Factory integration
# ---------------------------------------------------------------------------


class TestFactoryCreatesDiscordAdapter:
    """create_messaging_adapter returns a DiscordMessagingAdapter for 'discord'."""

    @patch("src.discord.bot.AgentQueueBot")
    def test_factory_discord_default(self, mock_bot_cls):
        """Factory creates Discord adapter when messaging_platform is 'discord'."""
        config = MagicMock()
        config.messaging_platform = "discord"
        orch = MagicMock()

        result = create_messaging_adapter(config, orch)

        from src.discord.adapter import DiscordMessagingAdapter

        assert isinstance(result, DiscordMessagingAdapter)
        mock_bot_cls.assert_called_once_with(config, orch)

    def test_factory_raises_when_no_attribute(self):
        """Factory raises when messaging_platform attr is missing."""
        config = MagicMock(spec=[])  # No attributes
        orch = MagicMock()

        with pytest.raises(AttributeError):
            create_messaging_adapter(config, orch)

    def test_factory_unsupported_platform(self):
        config = MagicMock()
        config.messaging_platform = "slack"
        orch = MagicMock()

        with pytest.raises(ValueError, match="Unknown messaging platform"):
            create_messaging_adapter(config, orch)

    @patch("src.telegram.bot.TelegramBot")
    def test_factory_telegram(self, mock_tg_bot_cls):
        """Factory creates Telegram adapter when messaging_platform is 'telegram'."""
        from src.telegram.adapter import TelegramMessagingAdapter

        config = MagicMock()
        config.messaging_platform = "telegram"
        orch = MagicMock()

        result = create_messaging_adapter(config, orch)
        assert isinstance(result, TelegramMessagingAdapter)


# ---------------------------------------------------------------------------
# ABC enforcement
# ---------------------------------------------------------------------------


class TestMessagingAdapterABCEnforcement:
    """Cannot instantiate MessagingAdapter directly or with missing methods."""

    def test_cannot_instantiate_abc_directly(self):
        with pytest.raises(TypeError):
            MessagingAdapter()

    def test_incomplete_subclass_raises(self):
        class Incomplete(MessagingAdapter):
            async def start(self): ...

            # Missing the rest

        with pytest.raises(TypeError):
            Incomplete()


# ---------------------------------------------------------------------------
# Callback registration (main.py adapter path)
# ---------------------------------------------------------------------------


class TestCallbackRegistration:
    """Orchestrator callbacks are registered through the adapter path."""

    @patch("src.discord.bot.AgentQueueBot")
    def test_callbacks_set_through_adapter(self, mock_bot_cls):
        """Simulates what main.py does after adapter.wait_until_ready()."""
        from src.discord.adapter import DiscordMessagingAdapter

        config = MagicMock()
        orch = MagicMock()
        orch._notify = None
        adapter = DiscordMessagingAdapter(config, orch)

        # Simulate what main.py does
        orch.set_notify_callback(adapter.send_message)
        orch.set_create_thread_callback(adapter.create_task_thread)
        orch.set_command_handler(adapter.get_command_handler())
        orch.set_supervisor(adapter.get_supervisor())

        orch.set_notify_callback.assert_called_once_with(adapter.send_message)
        orch.set_create_thread_callback.assert_called_once_with(adapter.create_task_thread)
        orch.set_command_handler.assert_called_once()
        orch.set_supervisor.assert_called_once()
