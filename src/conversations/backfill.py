"""Reconnect history recovery with durable cursors and explicit bounded gaps."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.conversations.limits import BACKFILL_HOURS, BACKFILL_MAX_MESSAGES
from src.escalations.transport import TransportUnavailable

# These are final input refusals, rather than a failed persistence attempt.
_FINAL_REFUSALS = frozenset(
    {
        "oversize",
        "rate_limited",
        "conversation_closed",
        "conversation_not_found",
        "foreign_destination",
        "author_not_allowlisted",
        "empty_text",
        "invalid_envelope",
    }
)


class ConversationBackfill:
    """Reuse the exclusive gateway router; checkpoint only fully completed pages."""

    def __init__(
        self,
        *,
        db: Any,
        router: Any,
        transport: Any,
        config: Any,
        clock=time.time,
        page_size: int = 100,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self._db = db
        self._router = router
        self._transport = transport
        self._config = config
        self._clock = clock
        self._page_size = min(page_size, BACKFILL_MAX_MESSAGES)
        self._lock = asyncio.Lock()

    async def run(self, *, bot_user_id: int | None) -> dict:
        # READY/RESUMED may overlap. Never let an older pass overwrite a newer cursor.
        async with self._lock:
            return await self._run(bot_user_id=bot_user_id)

    async def _run(self, *, bot_user_id: int | None) -> dict:
        result = {"channels": 0, "messages": 0, "gaps": [], "skipped": None}
        preconditions = self._router.preconditions()
        if not preconditions.ok:
            result["skipped"] = preconditions.unmet[0]
            return result

        now = self._clock()
        window_start = now - BACKFILL_HOURS * 3600
        settings = self._config.discord
        targets = [str(settings.channel_id)]
        # The status API defaults to 50 rows; recovery must cover every active thread,
        # including conversations with equal updated_at values (not timestamp paging).
        conversations = await self._db.list_conversations(states=["open"], limit=2**31 - 1)
        targets.extend(
            str(row["external_thread_id"])
            for row in conversations
            if row["transport"] == "discord"
            and row["guild_id"] == str(settings.guild_id)
            and row["channel_id"] == str(settings.channel_id)
            and row["external_thread_id"]
        )
        for channel_id in dict.fromkeys(targets):
            result["channels"] += 1
            cursor = await self._db.get_backfill_cursor(transport="discord", channel_id=channel_id)
            after_id = cursor["last_external_message_id"] if cursor else None
            gap_start = max(window_start, cursor["advanced_at"]) if cursor else window_start
            if cursor and cursor["advanced_at"] < window_start:
                await self._gap(
                    result, channel_id, cursor["advanced_at"], window_start, "cursor_expired", now
                )
                after_id = None
            if result["messages"] >= BACKFILL_MAX_MESSAGES:
                await self._gap(result, channel_id, gap_start, now, "pass_cap", now)
                continue

            while True:
                limit = min(self._page_size, BACKFILL_MAX_MESSAGES - result["messages"])
                try:
                    page = await self._transport.read_history(
                        channel_id=channel_id,
                        after_message_id=after_id,
                        after_ts=window_start,
                        before_ts=now,
                        limit=limit,
                    )
                except TransportUnavailable:
                    await self._gap(result, channel_id, gap_start, now, "history_forbidden", now)
                    break
                if not page:
                    break
                for message in page:
                    outcome = await self._router.route(
                        message,
                        bot_user_id=bot_user_id,
                        source="backfill",
                    )
                    if not _completed(outcome):
                        raise RuntimeError(f"backfill page not persisted: {outcome}")
                    result["messages"] += 1
                after_id = str(page[-1].id)
                gap_start = page[-1].created_at.timestamp()
                await self._db.advance_backfill_cursor(
                    transport="discord",
                    channel_id=channel_id,
                    last_external_message_id=after_id,
                    now=now,
                )
                if result["messages"] >= BACKFILL_MAX_MESSAGES:
                    await self._gap(result, channel_id, gap_start, now, "pass_cap", now)
                    break
                if len(page) < limit:
                    break
        return result

    async def _gap(self, result, channel_id, start, end, reason, now) -> None:
        result["gaps"].append(
            await self._db.record_intake_gap(
                transport="discord",
                channel_id=channel_id,
                gap_from=start,
                gap_to=end,
                reason=reason,
                now=now,
            )
        )

    def diagnostics(self, bot: Any) -> dict:
        """Read intent and cached channel permissions without fetching Discord data."""
        intent = getattr(getattr(bot, "intents", None), "message_content", None)
        permissions = None
        channel_id = self._config.discord.channel_id
        channel = bot.get_channel(int(channel_id)) if channel_id else None
        member = getattr(getattr(channel, "guild", None), "me", None)
        if channel is not None and member is not None:
            available = channel.permissions_for(member)
            permissions = {
                name: bool(getattr(available, name, False))
                for name in (
                    "view_channel",
                    "read_message_history",
                    "send_messages",
                    "create_public_threads",
                    "send_messages_in_threads",
                )
            }
        return {"message_content_intent": intent, "permissions": permissions}


def _completed(outcome: str) -> bool:
    if outcome in {"escalation", "posted:created", "posted:replayed"}:
        return True
    if outcome.startswith("ignored:"):
        return outcome not in {"ignored:classify_error", "ignored:preconditions_unmet"}
    return outcome.removeprefix("refused:") in _FINAL_REFUSALS
