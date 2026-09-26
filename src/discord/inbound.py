"""Route authenticated gateway messages: escalation ownership precedes chat."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from src.commands.principal import ExecutionPrincipal, principal_context
from src.conversations.intake import ACTION_IGNORE, ObservedMessage, classify_conversation
from src.conversations.preconditions import conversation_preconditions
from src.discord.escalation_intake import DiscordEscalationIntake, _raw_id
from src.discord.intake_diagnostics import IgnoreCounter
from src.escalations.intake import IGNORE_LOG_FORMAT

logger = logging.getLogger(__name__)


class DiscordInboundRouter:
    """No transport writes; all conversation state goes through the command boundary."""

    def __init__(
        self,
        *,
        bot: Any,
        handler: Any,
        config: Any,
        escalation_intake: DiscordEscalationIntake,
        diagnostics: IgnoreCounter,
        cutover_status: Callable[[], str | None],
        outbox_bound: Callable[[], bool],
    ) -> None:
        self._bot = bot
        self._handler = handler
        self._config = config
        self._escalation_intake = escalation_intake
        self._diagnostics = diagnostics
        self._cutover_status = cutover_status
        self._outbox_bound = outbox_bound

    def observe(self, message: Any, *, bot_user_id: int | None) -> ObservedMessage:
        """Capture gateway identity, mention membership and the message timestamp."""
        channel = message.channel
        parent_id = getattr(channel, "parent_id", None)
        author = message.author
        return ObservedMessage(
            transport="discord",
            external_message_id=str(message.id),
            guild_id=_raw_id(message.guild),
            channel_id=str(parent_id) if parent_id else _raw_id(channel),
            thread_id=_raw_id(channel) if parent_id else None,
            author_id=_raw_id(author) or "",
            text=message.content or "",
            received_at=message.created_at.timestamp(),
            author_is_bot=bool(getattr(author, "bot", False)),
            is_own_message=bot_user_id is not None and author.id == bot_user_id,
            is_webhook=getattr(message, "webhook_id", None) is not None,
            is_dm=message.guild is None,
            is_edit=False,
            mentions_bot=any(
                getattr(user, "id", None) == bot_user_id
                for user in (getattr(message, "mentions", None) or [])
            )
            if bot_user_id is not None
            else False,
        )

    def _ignore(self, code: str, message: Any) -> str:
        channel = getattr(message, "channel", None)
        logger.info(
            IGNORE_LOG_FORMAT,
            code,
            _raw_id(getattr(message, "guild", None)),
            getattr(channel, "parent_id", None) or _raw_id(channel),
            _raw_id(message),
            _raw_id(getattr(message, "author", None)),
        )
        self._diagnostics.record(code)
        return f"ignored:{code}"

    async def route(self, message: Any, *, bot_user_id: int | None, source: str = "gateway") -> str:
        """Escalations stop routing even on failure; conversations fail closed."""
        try:
            # Keep the existing boolean entry point and its exact ignore
            # diagnostics while this opt-in feature is off.
            if not self._config.discord.conversation.enabled:
                consumed = await self._escalation_intake.handle(message, bot_user_id=bot_user_id)
                return "escalation" if consumed else "ignored:preconditions_unmet"
            outcome = await self._escalation_intake.handle_detailed(
                message, bot_user_id=bot_user_id
            )
        except Exception:
            logger.warning("escalation intake failed before routing", exc_info=True)
            self._ignore("classify_error", message)
            return "escalation_failed"
        if outcome.consumed:
            return "escalation"
        if outcome.failed:
            # The adapter already logged and counted classify_error.
            return "escalation_failed"

        try:
            observed = self.observe(message, bot_user_id=bot_user_id)
            discord = self._config.discord
            preconditions = conversation_preconditions(
                self._config,
                cutover_status=self._cutover_status(),
                outbox_bound=self._outbox_bound(),
            )
            classification_args = {
                "preconditions": preconditions,
                "configured_guild_id": discord.guild_id,
                "configured_channel_id": discord.channel_id,
                "authorized_author_ids": tuple(discord.authorized_users),
                "bot_user_id": bot_user_id,
            }
            decision = classify_conversation(
                observed, **classification_args, escalation_bound=False, conversation=None
            )
            if decision.action == ACTION_IGNORE and decision.code != "unknown_thread":
                return self._ignore(decision.code, message)

            conversation = None
            if observed.thread_id:
                # thread_unbound is returned only after escalation intake's
                # durable lookup. Reuse that negative result to avoid a second
                # query; disabled or otherwise refusing intake still needs the
                # exclusive ownership check before looking up a conversation.
                checked_unbound = (
                    outcome.decision is not None and outcome.decision.code == "thread_unbound"
                )
                binding = (
                    None
                    if checked_unbound
                    else await self._handler.db.find_escalation_by_thread(
                        channel_id=observed.channel_id, thread_id=observed.thread_id
                    )
                )
                if not binding:
                    conversation = await self._handler.db.find_conversation_by_thread(
                        transport="discord",
                        channel_id=observed.channel_id,
                        external_thread_id=observed.thread_id,
                    )
                decision = classify_conversation(
                    observed,
                    **classification_args,
                    escalation_bound=bool(binding),
                    conversation=conversation,
                )
                if decision.action == ACTION_IGNORE:
                    return self._ignore(decision.code, message)

            envelope = {
                "transport": observed.transport,
                "guild_id": observed.guild_id,
                "channel_id": observed.channel_id,
                "external_message_id": observed.external_message_id,
                "external_root_message_id": (
                    conversation["external_root_message_id"]
                    if conversation
                    else observed.external_message_id
                ),
                "external_thread_id": observed.thread_id,
                "author_id": observed.author_id,
                "text": decision.text,
                "received_at": observed.received_at,
                "mentions_bot": observed.mentions_bot,
            }
        except Exception:
            logger.warning("conversation intake failed to classify a message", exc_info=True)
            return self._ignore("classify_error", message)

        try:
            with principal_context(ExecutionPrincipal.service("discord-gateway")):
                result = await self._handler.execute(
                    "supervisor_inbox_post",
                    {
                        "envelope": envelope,
                        "conversation_id": decision.conversation_id,
                        "source": source,
                    },
                )
            if result.get("success"):
                logger.info(
                    "discord conversation posted conversation=%s input=%s created=%s",
                    result.get("conversation_id"),
                    result.get("input_id"),
                    result.get("created"),
                )
                return "posted:created" if result.get("created") else "posted:replayed"
            code = result.get("error_code") or "post_failed"
        except Exception:
            logger.warning("conversation input could not be persisted", exc_info=True)
            code = "post_failed"
        logger.info(
            "discord conversation refused reason=%s guild=%s channel=%s message=%s author=%s",
            code,
            observed.guild_id,
            observed.channel_id,
            observed.external_message_id,
            observed.author_id,
        )
        return f"refused:{code}"


__all__ = ["DiscordInboundRouter"]
