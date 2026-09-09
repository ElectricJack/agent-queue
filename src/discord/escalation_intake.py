"""Turn a Discord thread message into a verified escalation reply (§5, §7).

This is the only inbound Discord path that may touch an escalation, and it is
deliberately narrow.  It does not resolve gates, reopen tasks, append task
descriptions or type into a worker's terminal: it persists the human's words
through :func:`escalation_reply` and lets the owning supervisor decide, which
is the product contract's "every reply goes through the owning supervisor".

Identity is never taken from the message body.  The author is the account the
gateway authenticated for the connection, checked against the configured
``discord.authorized_users`` allowlist here, and only then handed to the core
as an ``ExecutionPrincipal.service("discord:<id>")`` — the shape
``_verified_reply_identity`` turns into ``human:discord:<id>``.  A caller that
put ``actor``, ``human`` or ``verified_actor`` in the request body would be
refused by ``_reject_authority_args``; this path never sends them.

Correlation is durable: the thread ID is matched against the confirmed receipt
on the incident's root delivery, never against a task/thread naming heuristic,
so it survives a restart and a reconnect (§7).
"""

from __future__ import annotations

import logging
from typing import Any

from src.commands.principal import ExecutionPrincipal, principal_context
from src.escalations.intake import (
    ACTION_ACCEPT,
    ACTION_CLOSED,
    InboundMessage,
    IntakeDecision,
    classify_inbound,
)

logger = logging.getLogger(__name__)


class DiscordEscalationIntake:
    """Correlate inbound Discord thread messages with open incidents."""

    def __init__(self, handler: Any, config: Any, *, reconcile: Any = None) -> None:
        self._handler = handler
        self._config = config
        self._reconcile = reconcile

    @property
    def _settings(self) -> Any:
        return self._config.discord.escalation

    def observe(self, message: Any, *, bot_user_id: int | None) -> InboundMessage:
        """The gateway's view of *message*, with nothing the author asserted."""
        channel = message.channel
        parent_id = getattr(channel, "parent_id", None)
        thread_id = str(channel.id) if parent_id else None
        author = message.author
        return InboundMessage(
            transport="discord",
            external_message_id=str(message.id),
            channel_id=(str(parent_id) if parent_id else str(getattr(channel, "id", "") or "")),
            thread_id=thread_id,
            author_id=str(getattr(author, "id", "") or ""),
            text=message.content or "",
            author_is_bot=bool(getattr(author, "bot", False)),
            is_own_message=bool(
                bot_user_id is not None and getattr(author, "id", None) == bot_user_id
            ),
        )

    async def classify(self, observed: InboundMessage) -> IntakeDecision:
        """Decide what to do, doing the durable lookup only when it can matter.

        The cheap gates run first against ``binding=None``: an unbound thread
        is the *last* refusal :func:`classify_inbound` reaches, so any other
        reason means channel chatter that should never cost a query.
        """
        discord_config = self._config.discord
        first = classify_inbound(
            observed,
            configured_channel_id=discord_config.channel_id or None,
            authorized_author_ids=tuple(discord_config.authorized_users),
            binding=None,
            enabled=bool(self._settings.enabled),
        )
        if first.reason != "thread is not bound to an escalation":
            return first
        binding = await self._handler.db.find_escalation_by_thread(
            channel_id=observed.channel_id or "",
            thread_id=observed.thread_id or "",
        )
        return classify_inbound(
            observed,
            configured_channel_id=discord_config.channel_id or None,
            authorized_author_ids=tuple(discord_config.authorized_users),
            binding=binding,
            enabled=bool(self._settings.enabled),
        )

    async def handle(self, message: Any, *, bot_user_id: int | None) -> bool:
        """Consume *message* as an escalation reply; ``False`` if it is not one.

        Returning ``False`` leaves the bot's ordinary routing untouched.
        Returning ``True`` means the message belonged to an incident and must
        not also be forwarded as general supervisor chat — that would deliver
        the same words twice and is what "no general chatbot" rules out.
        """
        try:
            observed = self.observe(message, bot_user_id=bot_user_id)
            decision = await self.classify(observed)
        except Exception:
            logger.warning("escalation intake failed to classify a message", exc_info=True)
            return False
        if decision.action not in (ACTION_ACCEPT, ACTION_CLOSED):
            return False

        principal = ExecutionPrincipal.service(f"discord:{observed.author_id}")
        try:
            with principal_context(principal):
                result = await self._handler.execute(
                    "escalation_reply",
                    {
                        "escalation_id": decision.escalation_id,
                        "text": observed.text.strip(),
                        "external_message_id": observed.external_message_id,
                    },
                )
        except Exception:
            logger.warning(
                "escalation reply for %s could not be persisted",
                decision.escalation_id,
                exc_info=True,
            )
            # Consumed either way: retrying it as generic supervisor chat would
            # smuggle the same words in through a path with no provenance.
            return True
        if isinstance(result, dict) and result.get("error"):
            logger.warning(
                "escalation reply for %s refused: %s", decision.escalation_id, result["error"]
            )
            return True

        # The acknowledgement (or, for a closed incident, the closed-state
        # guidance) is a planned delivery, not a direct send: reconciling now
        # only makes it prompt.  A failure here is invisible to the human and
        # the orchestrator's own tick will pick the delivery up regardless.
        if self._reconcile is not None and isinstance(result, dict) and result.get("created"):
            try:
                await self._reconcile(decision.escalation_id)
            except Exception:
                logger.debug("post-reply reconcile failed; the cycle will retry", exc_info=True)
        return True


__all__ = ["DiscordEscalationIntake"]
