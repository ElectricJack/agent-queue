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

Every message this adapter does not consume is ignored silently in the channel
and loudly in the log: exactly one INFO line carrying the stable reason code
and the gateway ids, never the text, so "my message vanished" is answerable by
grepping for ``discord intake ignored``.  The same code also goes to the
optional ``on_ignore`` hook, which the bot points at its
:class:`~src.discord.intake_diagnostics.IgnoreCounter` so ``digest_status`` can
report the last hour's ignores by code.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from src.commands.principal import ExecutionPrincipal, principal_context
from src.escalations.intake import (
    ACTION_ACCEPT,
    ACTION_CLOSED,
    IGNORE_LOG_FORMAT,
    InboundMessage,
    IntakeDecision,
    classify_inbound,
)

logger = logging.getLogger(__name__)

#: The ignore code for a message the adapter could not observe or classify.
#: Not in ``REASON_CODES``: the pure classifier never returns it.
CLASSIFY_ERROR_CODE = "classify_error"


def _raw_id(obj: Any) -> str | None:
    value = getattr(obj, "id", None)
    return str(value) if value not in (None, "") else None


class DiscordEscalationIntake:
    """Correlate inbound Discord thread messages with open incidents."""

    def __init__(
        self,
        handler: Any,
        config: Any,
        *,
        reconcile: Any = None,
        on_ignore: Callable[[str], None] | None = None,
    ) -> None:
        self._handler = handler
        self._config = config
        self._reconcile = reconcile
        self._on_ignore = on_ignore

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
            guild_id=_raw_id(getattr(message, "guild", None)),
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
        if first.code != "thread_unbound":
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
        observed: InboundMessage | None = None
        try:
            observed = self.observe(message, bot_user_id=bot_user_id)
            decision = await self.classify(observed)
        except Exception:
            logger.warning("escalation intake failed to classify a message", exc_info=True)
            self._log_ignore(observed, CLASSIFY_ERROR_CODE, message)
            return False
        if decision.action not in (ACTION_ACCEPT, ACTION_CLOSED):
            self._log_ignore(observed, decision.code, message)
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

    def _log_ignore(self, observed: InboundMessage | None, code: str, message: Any) -> None:
        """The one INFO line for an ignored message: code and ids, never content."""
        if observed is not None:
            ids = (
                observed.guild_id,
                observed.channel_id,
                observed.external_message_id,
                observed.author_id,
            )
        else:
            # observe() itself failed: report whatever ids the raw message has.
            channel = getattr(message, "channel", None)
            ids = (
                _raw_id(getattr(message, "guild", None)),
                getattr(channel, "parent_id", None) or _raw_id(channel),
                _raw_id(message),
                _raw_id(getattr(message, "author", None)),
            )
        logger.info(IGNORE_LOG_FORMAT, code, *(value or None for value in ids))
        if self._on_ignore is not None:
            try:
                self._on_ignore(code)
            except Exception:
                # Diagnostics must never turn an ignore into a gateway error.
                logger.debug("intake ignore hook failed", exc_info=True)


__all__ = ["CLASSIFY_ERROR_CODE", "DiscordEscalationIntake"]
