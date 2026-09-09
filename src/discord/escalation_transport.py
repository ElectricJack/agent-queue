"""Discord implementation of the escalation transport port.

Everything Discord-specific about §7 lives here: how a channel is resolved by
ID, how a thread is created from the root message, how a deleted post is told
apart from a rate limit, and how the marker search reads back recent history
after an ambiguous send.

The rest of the feature never imports ``discord``.  That is deliberate: the
planner, renderer and dispatcher are exercised against
:class:`~src.escalations.transport.SinkTransport`, and no test in this repo
touches a real gateway.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from src.escalations.transport import (
    SendOutcome,
    ThreadHandle,
    TransportAmbiguous,
    TransportMissing,
    TransportRetryable,
    TransportUnavailable,
)

logger = logging.getLogger(__name__)

#: How far back the marker search reads when reconciling an ambiguous send.
HISTORY_LIMIT = 50


#: Mentions are decided by the renderer, from configuration.  Discord is told
#: to honour exactly what the text contains and nothing else — a belt to the
#: renderer's braces, so a mention that somehow survives sanitisation still
#: cannot ping ``@everyone`` or an arbitrary role.
def _allowed_mentions(escalation_config: Any) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        users=[discord.Object(id=int(uid)) for uid in escalation_config.mention_user_ids],
        roles=[discord.Object(id=int(rid)) for rid in escalation_config.mention_role_ids],
        replied_user=False,
    )


class DiscordEscalationTransport:
    """Adapts :class:`~src.discord.bot.AgentQueueBot` to the transport port."""

    def __init__(self, bot: Any, config: Any) -> None:
        self._bot = bot
        self._config = config

    # -- helpers --------------------------------------------------------
    @property
    def _settings(self) -> Any:
        return self._config.discord.escalation

    def _record(self, status: int) -> None:
        tracker = getattr(self._bot, "_rate_tracker", None)
        if tracker is not None:
            tracker.record(status)

    async def _channel(self, channel_id: str) -> Any:
        """Resolve the one configured channel by ID — never create one."""
        try:
            numeric = int(channel_id)
        except (TypeError, ValueError) as exc:
            raise TransportUnavailable(f"discord.channel_id {channel_id!r} is not an ID") from exc
        channel = self._bot.get_channel(numeric)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(numeric)
            except discord.NotFound as exc:
                raise TransportUnavailable(f"channel {channel_id} does not exist") from exc
            except discord.Forbidden as exc:
                self._record(403)
                raise TransportUnavailable(f"no access to channel {channel_id}") from exc
            except discord.HTTPException as exc:
                raise self._http_error(exc) from exc
        if not hasattr(channel, "send"):
            raise TransportUnavailable(f"channel {channel_id} cannot receive messages")
        return channel

    async def _thread(self, thread_id: str) -> Any:
        try:
            numeric = int(thread_id)
        except (TypeError, ValueError) as exc:
            raise TransportMissing(f"thread {thread_id!r} is not an ID") from exc
        thread = self._bot.get_channel(numeric)
        if thread is None:
            try:
                thread = await self._bot.fetch_channel(numeric)
            except discord.NotFound as exc:
                raise TransportMissing(f"thread {thread_id} no longer exists") from exc
            except discord.Forbidden as exc:
                self._record(403)
                raise TransportUnavailable(f"no access to thread {thread_id}") from exc
            except discord.HTTPException as exc:
                raise self._http_error(exc) from exc
        return thread

    def _http_error(self, exc: discord.HTTPException) -> Exception:
        """Classify a Discord HTTP failure into the port's fault taxonomy."""
        status = getattr(exc, "status", 0) or 0
        if status in (401, 429):
            self._record(status)
        if status == 403:
            self._record(403)
            return TransportUnavailable(f"missing permission ({exc})")
        if status == 404:
            return TransportMissing(str(exc))
        if status == 429 or status >= 500:
            return TransportRetryable(f"discord {status}: {exc}")
        return TransportRetryable(f"discord {status}: {exc}")

    def _guard(self) -> None:
        tracker = getattr(self._bot, "_rate_tracker", None)
        if tracker is not None and not tracker.should_allow(critical=True):
            raise TransportRetryable("held by the Discord invalid-request rate guard")

    # -- port ------------------------------------------------------------
    async def post_root(self, *, channel_id: str, content: str) -> SendOutcome:
        self._guard()
        channel = await self._channel(channel_id)
        try:
            message = await channel.send(
                content, allowed_mentions=_allowed_mentions(self._settings)
            )
        except discord.Forbidden as exc:
            self._record(403)
            raise TransportUnavailable(f"cannot post in channel {channel_id}: {exc}") from exc
        except (TimeoutError, discord.DiscordServerError) as exc:
            # The request left; whether it landed is unknown.
            raise TransportAmbiguous(f"root post outcome unknown: {exc}") from exc
        except discord.HTTPException as exc:
            raise self._http_error(exc) from exc
        return SendOutcome(
            receipt_id=str(message.id), channel_id=channel_id, root_message_id=str(message.id)
        )

    async def ensure_thread(
        self, *, channel_id: str, root_message_id: str, name: str
    ) -> ThreadHandle:
        """Bind the incident's thread without sending anything into it.

        A Discord message carries at most one thread, so re-finding it is the
        idempotent half of opening a thread; the opener message is the half
        that must never be replayed blindly, and the dispatcher sends that one
        separately through :meth:`post_thread_message`.
        """
        self._guard()
        channel = await self._channel(channel_id)
        try:
            message = await channel.fetch_message(int(root_message_id))
        except discord.NotFound as exc:
            raise TransportMissing(f"root message {root_message_id} was deleted") from exc
        except discord.Forbidden as exc:
            self._record(403)
            raise TransportUnavailable(str(exc)) from exc
        except discord.HTTPException as exc:
            raise self._http_error(exc) from exc

        existing = getattr(message, "thread", None)
        if existing is not None:
            return ThreadHandle(thread_id=str(existing.id), created=False)
        try:
            thread = await message.create_thread(name=name)
        except discord.Forbidden as exc:
            self._record(403)
            raise TransportUnavailable(f"cannot create threads here: {exc}") from exc
        except (TimeoutError, discord.DiscordServerError) as exc:
            raise TransportAmbiguous(f"thread creation outcome unknown: {exc}") from exc
        except discord.HTTPException as exc:
            raise self._http_error(exc) from exc
        return ThreadHandle(thread_id=str(thread.id), created=True)

    async def post_thread_message(self, *, thread_id: str, content: str) -> SendOutcome:
        self._guard()
        thread = await self._thread(thread_id)
        try:
            if getattr(thread, "archived", False):
                await thread.edit(archived=False)
            message = await thread.send(content, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden as exc:
            self._record(403)
            raise TransportUnavailable(f"cannot post in thread {thread_id}: {exc}") from exc
        except discord.NotFound as exc:
            raise TransportMissing(f"thread {thread_id} no longer exists") from exc
        except (TimeoutError, discord.DiscordServerError) as exc:
            raise TransportAmbiguous(f"thread post outcome unknown: {exc}") from exc
        except discord.HTTPException as exc:
            raise self._http_error(exc) from exc
        return SendOutcome(receipt_id=str(message.id), thread_id=thread_id)

    async def edit_root(self, *, channel_id: str, root_message_id: str, content: str) -> None:
        self._guard()
        channel = await self._channel(channel_id)
        try:
            message = await channel.fetch_message(int(root_message_id))
            await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound as exc:
            raise TransportMissing(f"root message {root_message_id} was deleted") from exc
        except discord.Forbidden as exc:
            self._record(403)
            raise TransportUnavailable(str(exc)) from exc
        except (TimeoutError, discord.DiscordServerError) as exc:
            raise TransportAmbiguous(f"root edit outcome unknown: {exc}") from exc
        except discord.HTTPException as exc:
            raise self._http_error(exc) from exc

    async def archive_thread(self, *, thread_id: str) -> None:
        self._guard()
        thread = await self._thread(thread_id)
        try:
            await thread.edit(archived=True)
        except discord.NotFound as exc:
            raise TransportMissing(f"thread {thread_id} no longer exists") from exc
        except discord.Forbidden as exc:
            self._record(403)
            raise TransportUnavailable(str(exc)) from exc
        except discord.HTTPException as exc:
            raise self._http_error(exc) from exc

    async def find_marker(
        self, *, channel_id: str, thread_id: str | None, marker: str
    ) -> SendOutcome | None:
        """Read back recent history looking for this delivery's own marker.

        This is the "available message history" §7 asks for before retrying an
        ambiguous send.  A miss is not proof the message is absent, which is
        exactly why the dispatcher records ``unknown`` rather than reposting.
        """
        self._guard()
        where = await (self._thread(thread_id) if thread_id else self._channel(channel_id))
        try:
            async for message in where.history(limit=HISTORY_LIMIT):
                if message.author.id != getattr(self._bot.user, "id", None):
                    continue
                if marker not in (message.content or ""):
                    continue
                thread = getattr(message, "thread", None)
                return SendOutcome(
                    receipt_id=str(message.id),
                    channel_id=channel_id,
                    root_message_id=(str(message.id) if thread_id is None else None),
                    thread_id=(thread_id or (str(thread.id) if thread is not None else None)),
                )
        except discord.Forbidden as exc:
            self._record(403)
            raise TransportUnavailable(str(exc)) from exc
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            raise self._http_error(exc) from exc
        return None
