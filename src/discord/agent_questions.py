"""Persistent worker question replies and acknowledged Discord notifications.

Question policy and fenced terminal delivery remain in AgentQuestionService.
Only this transport renders untrusted worker text and authenticates Discord users.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Any
from weakref import WeakValueDictionary

import discord

logger = logging.getLogger(__name__)
_PENDING = frozenset({"supervisor", "human", "answered"})
_STATE_LABELS = {
    "supervisor": "Awaiting supervisor",
    "human": "Waiting for your reply",
    "answered": "Answer queued for the original session",
    "delivered": "Answer delivered",
    "resolved": "No longer waiting",
    "stale": "Expired — original task or session changed",
}


async def _authorized(bot: Any, interaction: discord.Interaction) -> bool:
    check = getattr(bot, "_is_authorized", None)
    try:
        allowed = callable(check) and check(interaction.user.id) is True
    except Exception:
        allowed = False
    if not allowed:
        await interaction.response.send_message(
            "You don't have permission to answer this question.", ephemeral=True,
        )
    return bool(allowed)


class AgentQuestionReplyModal(discord.ui.Modal):
    def __init__(self, question_id: str, *, bot: Any):
        super().__init__(title="Reply to agent", custom_id=f"aq:question:answer:{question_id}")
        self.question_id = question_id
        self.bot = bot
        self.answer = discord.ui.TextInput(
            label="Your answer", style=discord.TextStyle.paragraph,
            required=True, min_length=1, max_length=4000,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Permissions may have changed since the Reply button opened this modal.
        if not await _authorized(self.bot, interaction):
            return
        body = str(self.answer.value).strip()
        handler = getattr(self.bot, "handler", None)
        if not body or handler is None:
            await interaction.response.send_message(
                "An answer and an available command handler are required.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            # Do not accept actor/human flags from the modal or call session input
            # directly. The trusted command scope and question service own both.
            result = await handler.execute("question_answer", {
                "question_id": self.question_id, "body": body, "_scope": {"kind": "local"},
            })
        except Exception:
            logger.exception("Failed to answer agent question %s", self.question_id)
            await interaction.followup.send(
                "Could not save the answer. Please try again.", ephemeral=True,
            )
            return
        if result.get("error"):
            await interaction.followup.send(str(result["error"]), ephemeral=True)
            return
        state = result.get("state")
        message = (
            "Answer delivered to the original agent session."
            if state == "delivered"
            else "Answer saved and queued for the original agent session."
        )
        await interaction.followup.send(message, ephemeral=True)


class AgentQuestionView(discord.ui.View):
    def __init__(self, question_id: str, *, bot: Any, disabled: bool = False):
        super().__init__(timeout=None)
        self.question_id = question_id
        self.bot = bot
        reply = discord.ui.Button(
            label="Reply", style=discord.ButtonStyle.primary,
            custom_id=f"aq:question:reply:{question_id}", disabled=disabled,
        )
        reply.callback = self._reply
        self.add_item(reply)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _authorized(self.bot, interaction)

    async def _reply(self, interaction: discord.Interaction) -> None:
        # Check here too: callbacks must remain safe when invoked independently.
        if not await _authorized(self.bot, interaction):
            return
        await interaction.response.send_modal(
            AgentQuestionReplyModal(self.question_id, bot=self.bot),
        )


def _literal(text: Any, limit: int) -> str:
    escaped = discord.utils.escape_markdown(str(text or ""), as_needed=False)
    return escaped if len(escaped) <= limit else escaped[:limit - 1] + "…"


def build_agent_question_embed(question: dict) -> discord.Embed:
    state = str(question.get("state") or "human")
    embed = discord.Embed(
        title="Agent question",
        description=_literal(question.get("question"), 3900),
        colour=discord.Colour.orange() if state in _PENDING else discord.Colour.green(),
    )
    for label, key in (("Agent", "agent_id"), ("Task", "task_id"), ("Project", "project_id")):
        embed.add_field(name=label, value=_literal(question.get(key) or "—", 256))
    session = f"{question.get('session_name') or '—'}\n{question.get('session_id') or '—'}"
    embed.add_field(name="Original session", value=_literal(session, 512), inline=False)
    embed.add_field(name="Status", value=_STATE_LABELS.get(state, state), inline=False)
    embed.set_footer(text=f"Question {question['id']} · Worker-authored text")
    return embed


def _sent_identity(message: Any) -> tuple[str, str] | None:
    if message is None:
        return None
    message_id = getattr(message, "id", None)
    channel_id = getattr(getattr(message, "channel", None), "id", None)
    if not isinstance(message_id, (int, str)) or not isinstance(channel_id, (int, str)):
        return None
    if not str(message_id).isdigit() or not str(channel_id).isdigit():
        return None
    return str(channel_id), str(message_id)


class AgentQuestionNotifications:
    def __init__(self, bot: Any):
        self.bot = bot
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._views: dict[str, AgentQuestionView] = {}
        # If the send succeeds but its DB acknowledgement fails, retry the
        # acknowledgement in this process without sending another card.
        self._sent: dict[str, tuple[str, str]] = {}

    @property
    def db(self):
        return self.bot.orchestrator.db

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _register(self, question: dict) -> None:
        ident = question["id"]
        if ident in self._views or not question.get("discord_message_id"):
            return
        if question.get("state") != "human":
            return
        view = AgentQuestionView(ident, bot=self.bot)
        self.bot.add_view(view, message_id=int(question["discord_message_id"]))
        self._views[ident] = view

    async def restore_views(self) -> None:
        for question in await self.db.list_agent_questions(pending_only=True):
            self._register(question)
            if question.get("discord_message_id") and question.get("state") == "answered":
                await self.update(question)

    async def notify(self, data: dict) -> None:
        ident = data.get("id")
        if not ident:
            return
        async with self._lock(f"question:{ident}"):
            # An event can arrive late, after the worker continued or was stopped.
            question = await self.db.get_agent_question(ident)
            if not question or question.get("state") != "human":
                return
            if question.get("discord_message_id"):
                self._register(question)
                return
            identity = self._sent.get(ident)
            if identity is None:
                view = AgentQuestionView(ident, bot=self.bot)
                kwargs: dict[str, Any] = {}
                body = str(question.get("question") or "")
                if len(discord.utils.escape_markdown(body)) > 3900:
                    kwargs["file"] = discord.File(io.BytesIO(body.encode()), filename="question.txt")
                message = await self.bot._send_message(
                    f"Agent question {ident}", project_id=question.get("project_id"),
                    embed=build_agent_question_embed(question), view=view,
                    allowed_mentions=discord.AllowedMentions.none(), **kwargs,
                )
                identity = _sent_identity(message)
                if identity is None:
                    logger.warning("Agent question %s was not posted; leaving it pending", ident)
                    return
                self._sent[ident] = identity
                self._views[ident] = view
            await self.db.mark_agent_question_notified(ident, *identity)
            self._sent.pop(ident, None)

    async def update(self, data: dict) -> None:
        ident = data.get("id")
        if not ident:
            return
        async with self._lock(f"question:{ident}"):
            question = await self.db.get_agent_question(ident)
            if not question or not question.get("discord_message_id"):
                return
            state = question.get("state")
            old_view = self._views.pop(ident, None)
            if old_view is not None:
                old_view.stop()
            if state == "human":
                self._register(question)
            channel = self.bot.get_channel(int(question["discord_channel_id"]))
            if channel is None:
                return
            view = self._views.get(ident) or AgentQuestionView(ident, bot=self.bot, disabled=True)
            message = channel.get_partial_message(int(question["discord_message_id"]))
            await self.bot._safe_api_call(
                message.edit(embed=build_agent_question_embed(question), view=view,
                             allowed_mentions=discord.AllowedMentions.none()),
                critical=False, context=f"agent question update {ident}",
            )

    async def send_user_message(self, data: dict) -> None:
        ident = data.get("message_id")
        if not ident:
            return
        async with self._lock(f"message:{ident}"):
            msg = await self.db.get_message(ident)
            if msg is None or msg.to_kind != "user" or not msg.body or msg.archived_at is not None:
                return
            thread_id = msg.thread_id
            discord_thread = isinstance(thread_id, str) and thread_id.startswith("discord:")
            if thread_id and not discord_thread:
                return  # an explicit conversation on a different transport
            parked = msg.from_kind == "system" and msg.from_id == "delivery-engine"
            if msg.from_kind != "session" and not (parked and discord_thread):
                return
            if not discord_thread and ":" in msg.to_id and not msg.to_id.startswith("discord:"):
                return
            receipt = await self.db.get_message_discord_receipt(ident)
            if receipt and receipt.get("discord_message_id"):
                return
            # Enrol only messages observed on this new path. Never scan historical
            # messages for missing receipts: old successful posts have no receipt.
            await self.db.ensure_message_discord_notification(ident)
            cached = self._sent.get(f"message:{ident}")
            if cached is None:
                channel_id = thread_id.partition(":")[2] if discord_thread else ""
                channel = self.bot.get_channel(int(channel_id)) if channel_id.isdigit() else None
                if discord_thread and channel_id.isdigit() and channel is None:
                    logger.warning(
                        "Discord channel %s not resolvable; falling back to project %s",
                        channel_id, msg.project_id,
                    )
                mentions = discord.AllowedMentions.none()
                if parked:
                    embed = discord.Embed(
                        title="⚠️ Message not delivered",
                        description=_literal(msg.body, 3800), colour=discord.Colour.orange(),
                    )
                    if channel is not None:
                        message = await self.bot._safe_api_call(
                            channel.send(
                                content="⚠️ A previous message was not delivered — see details.",
                                embed=embed, allowed_mentions=mentions,
                            ), critical=False, context="message.sent parked warning",
                        )
                    else:
                        message = await self.bot._send_message(
                            "⚠️ A previous message was not delivered — see details.",
                            project_id=msg.project_id, embed=embed, allowed_mentions=mentions,
                        )
                elif channel is not None:
                    message = await self.bot._send_long_message(
                        channel, msg.body, allowed_mentions=mentions, single_message=True,
                    )
                else:
                    message = await self.bot._send_message(
                        msg.body, project_id=msg.project_id,
                        allowed_mentions=mentions, single_message=True,
                    )
                cached = _sent_identity(message)
                if cached is None:
                    return
                self._sent[f"message:{ident}"] = cached
            await self.db.mark_message_discord_notified(ident, *cached)
            self._sent.pop(f"message:{ident}", None)

    async def retry_user_messages(self) -> None:
        for ident in await self.db.list_pending_message_discord_notifications(limit=100):
            try:
                await self.send_user_message({"message_id": ident})
            except Exception:
                logger.exception("Discord user message retry failed for %s", ident)


def _notifications(bot: Any) -> AgentQuestionNotifications:
    current = vars(bot).get("_agent_question_notifications")
    if not isinstance(current, AgentQuestionNotifications):
        current = AgentQuestionNotifications(bot)
        bot._agent_question_notifications = current
    return current


async def restore_agent_question_views(bot: Any) -> None:
    await _notifications(bot).restore_views()


async def retry_discord_user_messages(bot: Any) -> None:
    await _notifications(bot).retry_user_messages()
