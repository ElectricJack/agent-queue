"""Only explicit channel-message purge remains; task-thread cleanup is retired."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.commands.discord_commands import DiscordCommandsMixin


class Channel:
    id = 111
    name = "shared"

    def __init__(self, messages=()):
        self.messages = list(messages)
        self.deleted_batches = []

    async def history(self, limit=1000):
        for message in self.messages[:limit]:
            yield message

    async def delete_messages(self, messages):
        self.deleted_batches.append(list(messages))


def handler(channel):
    result = DiscordCommandsMixin()
    bot = MagicMock()
    bot.get_channel.return_value = channel
    result.orchestrator = SimpleNamespace(_discord_bot=bot)
    return result


async def test_purge_is_dry_run_by_default_and_reports_old_messages():
    now = dt.datetime.now(dt.timezone.utc)
    channel = Channel(
        [
            SimpleNamespace(created_at=now - dt.timedelta(days=1)),
            SimpleNamespace(created_at=now - dt.timedelta(days=30)),
        ]
    )

    result = await handler(channel)._cmd_discord_purge_channel({"channel_id": "111"})

    assert result["dry_run"] is True
    assert result["deletable"] == 1
    assert result["too_old_to_bulk_delete"] == 1
    assert channel.deleted_batches == []


async def test_task_thread_cleanup_is_absent():
    assert not hasattr(DiscordCommandsMixin, "_cmd_discord_cleanup_threads")
