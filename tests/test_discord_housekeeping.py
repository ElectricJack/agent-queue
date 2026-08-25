"""``discord_purge_channel`` / ``discord_cleanup_threads``.

Both are destructive and irreversible against a live Discord guild, so both
are dry runs unless ``confirm`` is passed. A mistyped channel id that deletes
a few hundred messages has no undo.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.discord_commands import DiscordCommandsMixin


def _now(days_ago=0):
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)


class _Channel:
    def __init__(self, messages=(), threads=(), archived=()):
        self.id = 111
        self.name = "agent-queue-win"
        self._messages = list(messages)
        self.threads = list(threads)
        self._archived = list(archived)
        self.deleted_batches = []

    async def history(self, limit=1000):
        for m in self._messages[:limit]:
            yield m

    async def archived_threads(self, limit=500):
        for t in self._archived[:limit]:
            yield t

    async def delete_messages(self, chunk):
        self.deleted_batches.append(list(chunk))


def _thread(tid, archived=False):
    t = MagicMock()
    t.id = tid
    t.archived = archived
    t.delete = AsyncMock()
    t.edit = AsyncMock()
    return t


def _handler(channel, tasks=()):
    h = DiscordCommandsMixin()
    h.orchestrator = SimpleNamespace(_discord_bot=MagicMock())
    h.orchestrator._discord_bot.get_channel = MagicMock(return_value=channel)
    h.db = MagicMock()
    h.db.list_tasks = AsyncMock(return_value=list(tasks))
    return h


class TestPurgeChannel:
    @pytest.mark.asyncio
    async def test_dry_run_by_default_deletes_nothing(self):
        ch = _Channel(messages=[SimpleNamespace(created_at=_now(1)) for _ in range(5)])
        res = await _handler(ch)._cmd_discord_purge_channel({"channel_id": "111"})
        assert res["dry_run"] is True
        assert res["deletable"] == 5
        assert ch.deleted_batches == []

    @pytest.mark.asyncio
    async def test_confirm_deletes_in_chunks_of_100(self):
        ch = _Channel(messages=[SimpleNamespace(created_at=_now(1)) for _ in range(250)])
        res = await _handler(ch)._cmd_discord_purge_channel(
            {"channel_id": "111", "confirm": True}
        )
        assert res["deleted"] == 250
        assert [len(b) for b in ch.deleted_batches] == [100, 100, 50]

    @pytest.mark.asyncio
    async def test_messages_over_14_days_are_reported_not_hidden(self):
        """Discord refuses to bulk-delete them; saying so beats a false 'done'."""
        ch = _Channel(
            messages=[SimpleNamespace(created_at=_now(1))]
            + [SimpleNamespace(created_at=_now(30)) for _ in range(3)]
        )
        res = await _handler(ch)._cmd_discord_purge_channel(
            {"channel_id": "111", "confirm": True}
        )
        assert res["deleted"] == 1
        assert res["too_old_to_bulk_delete"] == 3

    @pytest.mark.asyncio
    async def test_requires_a_target(self):
        res = await _handler(_Channel())._cmd_discord_purge_channel({})
        assert "error" in res


class TestCleanupThreads:
    @pytest.mark.asyncio
    async def test_dry_run_by_default(self):
        ch = _Channel(threads=[_thread(1), _thread(2)])
        res = await _handler(ch)._cmd_discord_cleanup_threads({"channel_id": "111"})
        assert res["dry_run"] is True
        assert res["would_archive"] == 2

    @pytest.mark.asyncio
    async def test_threads_for_running_tasks_are_skipped(self):
        """only_closed defaults true — a live task keeps its thread."""
        live = SimpleNamespace(discord_thread_id="1", status="IN_PROGRESS")
        done = SimpleNamespace(discord_thread_id="2", status="COMPLETED")
        ch = _Channel(threads=[_thread(1), _thread(2)])
        res = await _handler(ch, tasks=[live, done])._cmd_discord_cleanup_threads(
            {"channel_id": "111", "confirm": True}
        )
        assert res["archived"] == 1
        assert res["skipped_live"] == 1

    @pytest.mark.asyncio
    async def test_delete_mode_removes_threads(self):
        threads = [_thread(1), _thread(2)]
        ch = _Channel(threads=threads)
        res = await _handler(ch)._cmd_discord_cleanup_threads(
            {"channel_id": "111", "mode": "delete", "confirm": True}
        )
        assert res["deleted"] == 2
        assert all(t.delete.await_count == 1 for t in threads)

    @pytest.mark.asyncio
    async def test_archive_mode_skips_already_archived(self):
        ch = _Channel(threads=[_thread(1, archived=True), _thread(2)])
        res = await _handler(ch)._cmd_discord_cleanup_threads(
            {"channel_id": "111", "confirm": True}
        )
        assert res["archived"] == 1

    @pytest.mark.asyncio
    async def test_invalid_mode_is_rejected(self):
        res = await _handler(_Channel())._cmd_discord_cleanup_threads(
            {"channel_id": "111", "mode": "nuke"}
        )
        assert "error" in res


class TestArchivedListingFailureIsReported:
    """A failed archived listing must not masquerade as "nothing archived".

    Swallowing it reported a tidy count while leaving every archived thread
    behind, and the caller could not tell "none archived" from "could not
    look" — the same silent-success shape this codebase keeps getting bitten
    by.
    """

    class _BadChannel(_Channel):
        async def archived_threads(self, limit=500):
            raise PermissionError("missing Read Message History")
            yield  # pragma: no cover - makes this an async generator

    @pytest.mark.asyncio
    async def test_dry_run_carries_the_warning(self):
        ch = self._BadChannel(threads=[_thread(1)])
        res = await _handler(ch)._cmd_discord_cleanup_threads({"channel_id": "111"})
        assert res["success"] is True
        assert "warning" in res
        assert "archived" in res["warning"]

    @pytest.mark.asyncio
    async def test_active_threads_are_still_cleaned(self):
        ch = self._BadChannel(threads=[_thread(1)])
        res = await _handler(ch)._cmd_discord_cleanup_threads(
            {"channel_id": "111", "confirm": True}
        )
        assert res["archived"] == 1
        assert "warning" in res
