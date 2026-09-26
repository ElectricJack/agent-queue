"""Decide whether an inbound transport message opens or continues a conversation.

The mirror of :mod:`src.escalations.intake` for Discord @mention conversations
(mention-routing spec §4.1): a pure function of what the gateway *observed* —
never a clock, never the database, never a field the author could set.  The
author is the connection's account as the gateway reports it; the text is only
ever the payload, so a body that claims to be somebody cannot become them.

Every gate returns its own stable code, in :data:`CONVERSATION_CODES` order,
and an ignore is silent: the router logs one INFO line with the code and ids.
Two rules are unconditional here rather than trusted to the router:

* a thread bound to an escalation belongs to escalation intake, even when a
  conversation row names the same thread (``escalation_thread``);
* a top-level message opens a conversation only when the gateway saw the bot
  *user* in ``message.mentions`` — a role, ``@everyone`` or a typed token in
  the text is not a mention.

Size, rate limits and closed conversations are ``supervisor_inbox_post``'s
refusals, not ours: the command re-validates anyway and owns the deduped
notices those three refusals send.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.conversations.preconditions import ConversationPreconditions
from src.escalations.render import _MENTION_TOKEN, _WHITESPACE

#: A top-level bot mention in the configured channel: open a conversation.
ACTION_OPEN = "open"
#: A message in a thread bound to a conversation; needs no mention.
ACTION_FOLLOW_UP = "follow_up"
#: Not a conversation message.  Silent; the router logs the code.
ACTION_IGNORE = "ignore"

#: Every ignore code, in gate order.  A rename is a contract change: operators
#: grep for these and the digest counts by them.  ``classify_error`` is not a
#: gate — it is the router's code when classifying raises.
CONVERSATION_CODES = (
    "preconditions_unmet",
    "own_message",
    "bot_author",
    "webhook_author",
    "dm",
    "edit",
    "foreign_guild",
    "foreign_channel",
    "author_not_allowlisted",
    "no_bot_mention",
    "escalation_thread",
    "unknown_thread",
    "empty_text",
    "classify_error",
)

#: C0 and C1 control characters (and DEL), except ``\t`` and ``\n``.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class ObservedMessage:
    """What a transport gateway observed, with nothing the author asserted.

    ``channel_id`` is the *parent* channel of ``thread_id``, else the channel
    itself.  ``mentions_bot`` is true only when the bot user is in the
    gateway's ``message.mentions``; role and ``@everyone`` mentions do not
    count.
    """

    transport: str
    external_message_id: str
    guild_id: str | None
    channel_id: str | None
    thread_id: str | None
    author_id: str
    text: str
    received_at: float
    author_is_bot: bool = False
    is_own_message: bool = False
    is_webhook: bool = False
    is_dm: bool = False
    is_edit: bool = False
    mentions_bot: bool = False


@dataclass(frozen=True)
class ConversationDecision:
    """What the router should do with one inbound message.

    ``code`` is ``"open"``, ``"follow_up"`` or a :data:`CONVERSATION_CODES`
    entry; unlike ``reason`` it never changes wording.  ``text`` is the
    normalised text of an open or follow-up and empty for an ignore.
    """

    action: str
    code: str
    reason: str
    conversation_id: str | None = None
    text: str = ""


def _ignore(code: str, reason: str) -> ConversationDecision:
    return ConversationDecision(action=ACTION_IGNORE, code=code, reason=reason)


def normalise_text(raw: str, *, bot_user_id: int | str | None) -> str:
    """The text a conversation stores: NFC, without the bot's own mention.

    Drops ``<@id>`` / ``<@!id>`` tokens naming the bot (other mention tokens
    stay; outbound rendering sanitises them), strips C0/C1 controls except
    ``\\n`` and ``\\t``, collapses runs of spaces and tabs, and strips.  The
    caller measures the result in code points.
    """
    text = unicodedata.normalize("NFC", raw or "")
    if bot_user_id is not None and str(bot_user_id).strip():
        own = {f"<@{bot_user_id}>", f"<@!{bot_user_id}>"}
        text = _MENTION_TOKEN.sub(lambda m: "" if m.group() in own else m.group(), text)
    text = _CONTROL.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def classify_conversation(
    message: ObservedMessage,
    *,
    preconditions: ConversationPreconditions,
    configured_guild_id: str,
    configured_channel_id: str,
    authorized_author_ids: Sequence[str],
    bot_user_id: int | str | None,
    escalation_bound: bool,
    conversation: Mapping[str, Any] | None,
) -> ConversationDecision:
    """Whether *message* opens or continues a supervisor conversation.

    ``escalation_bound`` says whether escalation intake owns the message's
    thread; ``conversation`` is the row
    :meth:`~src.database.queries.conversation_queries.ConversationQueriesMixin
    .find_conversation_by_thread` returned for it (its ``id``, ``transport``,
    ``channel_id`` and ``external_thread_id`` are read).  Both only matter
    for a thread message.
    """
    if not preconditions.ok:
        return _ignore(
            "preconditions_unmet", f"preconditions unmet: {','.join(preconditions.unmet)}"
        )
    # Loop guards first: nothing a bot or a webhook posts may become input.
    if message.is_own_message:
        return _ignore("own_message", "message was authored by this bot")
    if message.author_is_bot:
        return _ignore("bot_author", "message was authored by a bot")
    if message.is_webhook:
        return _ignore("webhook_author", "message was posted by a webhook")
    if message.is_dm:
        return _ignore("dm", "message is a direct message")
    # An edit would rewrite an instruction the supervisor may already act on.
    if message.is_edit:
        return _ignore("edit", "message is an edit")
    if not configured_guild_id or message.guild_id != str(configured_guild_id):
        return _ignore("foreign_guild", "message is not in the configured guild")
    if not configured_channel_id or message.channel_id != str(configured_channel_id):
        return _ignore("foreign_channel", "message is not in the configured channel")
    allowlist = {str(author).strip() for author in authorized_author_ids} - {""}
    if not message.author_id or str(message.author_id) not in allowlist:
        return _ignore("author_not_allowlisted", "author is not on the conversation allowlist")

    conversation_id: str | None = None
    if not message.thread_id:
        if not message.mentions_bot:
            return _ignore("no_bot_mention", "top-level message does not mention the bot")
    else:
        # Unconditional: an escalation thread never becomes chat, whatever
        # conversation row the caller also found for it.
        if escalation_bound:
            return _ignore("escalation_thread", "thread is bound to an escalation")
        if conversation is None:
            return _ignore("unknown_thread", "thread is not bound to a conversation")
        # Defence in depth: the row must agree with what the gateway observed,
        # so a lookup that grew looser cannot widen which thread is chat.
        if (
            str(conversation.get("transport") or "") != message.transport
            or str(conversation.get("channel_id") or "") != message.channel_id
            or str(conversation.get("external_thread_id") or "") != message.thread_id
        ):
            return _ignore("unknown_thread", "bound conversation disagrees with the thread")
        conversation_id = str(conversation["id"])

    text = normalise_text(message.text, bot_user_id=bot_user_id)
    if not text:
        return _ignore("empty_text", "message has no text")
    if conversation_id is None:
        return ConversationDecision(
            action=ACTION_OPEN,
            code="open",
            reason="bot mention in the configured channel",
            text=text,
        )
    return ConversationDecision(
        action=ACTION_FOLLOW_UP,
        code="follow_up",
        reason="message in a conversation thread",
        conversation_id=conversation_id,
        text=text,
    )


__all__ = [
    "ACTION_FOLLOW_UP",
    "ACTION_IGNORE",
    "ACTION_OPEN",
    "CONVERSATION_CODES",
    "ConversationDecision",
    "ObservedMessage",
    "classify_conversation",
    "normalise_text",
]
