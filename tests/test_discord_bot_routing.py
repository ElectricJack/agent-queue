"""Discord inbound routing is escalation-only after cutover."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.config import AppConfig, DiscordConfig
from src.discord.bot import AgentQueueBot


def make_bot(*, authorized=("42",), cutover=True):
    bot = AgentQueueBot.__new__(AgentQueueBot)
    bot.config = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1", authorized_users=list(authorized))
    )
    bot.orchestrator = SimpleNamespace(_command_handler=MagicMock())
    bot._cutover_complete = MagicMock()
    bot._cutover_complete.is_set.return_value = cutover
    bot._cutover_report = SimpleNamespace(status="complete")
    intake = MagicMock()
    intake.handle = AsyncMock()
    bot._escalation_intake = MagicMock(return_value=intake)
    user = SimpleNamespace(id=1)
    bot._connection = SimpleNamespace(user=user)
    return bot, user, intake


def message(user_id=42):
    return SimpleNamespace(author=SimpleNamespace(id=user_id))


async def test_authorized_message_reaches_only_escalation_intake():
    bot, user, intake = make_bot()
    msg = message()

    await AgentQueueBot.on_message(bot, msg)

    intake.handle.assert_awaited_once_with(msg, bot_user_id=user.id)


async def test_unauthorized_self_and_precrossover_messages_are_inert():
    bot, user, intake = make_bot()
    await AgentQueueBot.on_message(bot, message(999))
    own = message(1)
    own.author = user
    await AgentQueueBot.on_message(bot, own)
    bot._cutover_complete.is_set.return_value = False
    await AgentQueueBot.on_message(bot, message())

    intake.handle.assert_not_awaited()


async def test_failed_cutover_cannot_activate_reply_routing():
    bot, _user, intake = make_bot()
    bot._cutover_report = SimpleNamespace(status="failed")

    await AgentQueueBot.on_message(bot, message())

    intake.handle.assert_not_awaited()
