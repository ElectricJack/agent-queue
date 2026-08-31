"""Discord bot inbound-routing authorization (approved plan item 19).

``AgentQueueBot.on_message`` must silently drop messages from users not in
``discord.authorized_users`` *before* any state change or side effect: no
command-handler call, no ``message_send``, no reaction, no reply, no thread
handling, no dedup bookkeeping.  A leak here would let any guild member
drive the supervisor session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import AppConfig, DiscordConfig


@pytest.fixture
async def orch(orchestrator_factory):
    return await orchestrator_factory()


def _make_message(*, author_id: int, channel_id: int = 123):
    message = MagicMock()
    message.author = MagicMock(id=author_id, display_name="mallory", bot=False)
    message.channel = MagicMock(id=channel_id)
    message.attachments = []
    message.content = "do something"
    message.reference = None
    message.id = 4001
    message.created_at = MagicMock()
    message.created_at.timestamp = MagicMock(return_value=1.0)
    message.add_reaction = AsyncMock()
    message.reply = AsyncMock()
    message.create_thread = AsyncMock()
    return message


async def test_unauthorized_project_channel_message_never_reaches_handler_or_reacts(orch):
    from src.discord.bot import AgentQueueBot

    # Real bot object (no gateway) with a real authorized-users config and
    # the real _is_authorized method deciding.
    config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1", authorized_users=["42"]),
        workspace_dir=orch.config.workspace_dir,
        database_path=orch.config.database_path,
        data_dir=orch.config.data_dir,
    )
    config.supervisor_agent.enabled = True
    config.messages.enabled = True

    called: list[tuple[str, dict]] = []

    async def _recording_execute(cmd, args):
        called.append((cmd, dict(args)))
        return {"success": True}

    orch.command_handler.execute = _recording_execute  # type: ignore[assignment]
    orch._command_handler = orch.command_handler

    bot = AgentQueueBot.__new__(AgentQueueBot)
    bot.config = config
    bot.orchestrator = orch
    bot._channel_locks = {}
    bot._processed_messages = set()
    bot._task_threads = {}
    bot._channel = None
    bot._channel_to_project = {123: "p1"}
    bot._project_channels = {"p1": MagicMock(id=123)}
    bot._boot_time = 0.0
    bot._download_attachments = AsyncMock(return_value=[])
    bot._send_long_message = AsyncMock()
    bot._safe_api_call = AsyncMock(return_value=None)
    _bot_user = MagicMock(id=1)
    type(bot).user = _bot_user

    # Author 999 is not in authorized_users=["42"].
    message = _make_message(author_id=999)
    message.mentions = [_bot_user]  # mentioned in a mapped project channel

    await AgentQueueBot.on_message(bot, message)

    # Dropped before anything happened: no command (so no message_send),
    # no reaction, no reply, no thread creation, no long-message post —
    # and not even dedup bookkeeping for the foreign message.
    assert called == []
    message.add_reaction.assert_not_awaited()
    message.reply.assert_not_awaited()
    message.create_thread.assert_not_awaited()
    bot._send_long_message.assert_not_awaited()
    bot._safe_api_call.assert_not_awaited()
    assert message.id not in bot._processed_messages

    # Control: the same message from the authorized user does route.
    allowed = _make_message(author_id=42)
    allowed.id = 4002
    allowed.mentions = [_bot_user]
    await AgentQueueBot.on_message(bot, allowed)
    assert [cmd for cmd, _ in called] == ["message_send"]
    assert called[0][1]["from_id"] == "discord:42"
    assert called[0][1]["to_id"] == "supervisor-p1"
