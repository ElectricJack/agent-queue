"""Decide whether an inbound transport message opens or continues a conversation.

The mirror of :mod:`src.escalations.intake` for Discord supervisor conversations
(mention-routing spec §4.1, extended by the 2026-10-03 chat-extension spec
§2.1–§2.2): a pure function of what the gateway *observed* — never a clock,
never the database, never a field the author could set.  The author is the
connection's account as the gateway reports it; the text is only ever the
payload, so a body that claims to be somebody cannot become them.

Every gate returns its own stable code, in :data:`CONVERSATION_CODES` order,
and an ignore is silent: the router logs one INFO line with the code and ids.
Two rules are unconditional here rather than trusted to the router:

* a thread bound to an escalation belongs to escalation intake, even when a
  conversation row names the same thread (``escalation_thread``);
* nobody but an allow-listed human is ever chat input, whatever the author,
  bot, webhook or loop flags say (``own_message``, ``bot_author``,
  ``webhook_author``, ``author_not_allowlisted``).

Everything else is the P2 phase flag of §7.1, decided by the caller and
restated here so the pure decision is testable on its own:

* ``require_mention=True`` (the default) is the installed mention routing: a
  top-level message needs the bot user in ``message.mentions`` and an unbound
  thread is nobody's business.  ``require_mention=False`` replaces it with
  §2.2: the top-level message joins the channel's one conversation, a mention
  changes nothing, and a thread Jack started binds a conversation of its own.
* ``allow_dm`` (§2.1) admits a direct message from an allow-listed user as a
  channel message whose thread is :func:`dm_thread_id`; it is off by default.

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

#: A thread-bound conversation, one per opened thread (the installed default).
KIND_THREAD = "thread"
#: The one durable conversation a channel carries (§2.2).
KIND_CHANNEL = "channel"

#: The synthetic guild a direct message is admitted under (§2.1).  A DM has no
#: guild, and inventing a snowflake would put a lie in a durable column.
DM_GUILD = "dm"
#: Prefix of the synthetic thread a direct message is admitted as (§2.1).
DM_THREAD_PREFIX = "dm:"

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

#: ``<kind>:<id>`` naming what a thread hangs off (§2.2, §2.3).  The id is an
#: aq id or a window id, never prose: the envelope carries it so the
#: supervisor knows which review or digest the thread is about.
_TAG = re.compile(r"^(?:review|digest|report):[A-Za-z0-9._:-]{1,64}$")

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

    For an open, ``conversation_id`` is ``None`` and the destination is
    described rather than named: ``bind_thread`` binds the new conversation to
    the observed thread, ``channel`` makes it the channel's one conversation
    (§2.2).  ``tag`` is what the thread hangs off, when durable state names it.
    """

    action: str
    code: str
    reason: str
    conversation_id: str | None = None
    text: str = ""
    bind_thread: bool = False
    channel: bool = False
    tag: str | None = None


def dm_thread_id(channel_id: str) -> str:
    """The synthetic thread a direct message is admitted as (§2.1)."""
    return f"{DM_THREAD_PREFIX}{channel_id}"


def is_dm_thread(thread_id: str | None) -> bool:
    """Whether *thread_id* is the synthetic thread of :func:`dm_thread_id`."""
    return bool(thread_id) and str(thread_id).startswith(DM_THREAD_PREFIX)


def normalise_tag(tag: str | None) -> str | None:
    """A ``<kind>:<id>`` thread tag, or ``None`` when it is not one."""
    if not isinstance(tag, str) or not _TAG.match(tag):
        return None
    return tag


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
    channel_conversation: Mapping[str, Any] | None = None,
    require_mention: bool = True,
    allow_dm: bool = False,
    thread_tag: str | None = None,
) -> ConversationDecision:
    """Whether *message* opens or continues a supervisor conversation.

    ``escalation_bound`` says whether escalation intake owns the message's
    thread; ``conversation`` is the row
    :meth:`~src.database.queries.conversation_queries.ConversationQueriesMixin
    .find_conversation_by_thread` returned for it (its ``id``, ``transport``,
    ``channel_id`` and ``external_thread_id`` are read), and
    ``channel_conversation`` is the row that channel-level route returned.
    ``conversation`` only matters for a thread message and
    ``channel_conversation`` only for a top-level one.

    ``thread_tag`` is what durable state says the thread hangs off; an
    unrecognised value is dropped rather than passed on.
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
    if message.is_dm and not allow_dm:
        return _ignore("dm", "message is a direct message")
    # An edit would rewrite an instruction the supervisor may already act on.
    if message.is_edit:
        return _ignore("edit", "message is an edit")
    if message.is_dm:
        # A direct message has no guild and its channel is the author's own
        # private one, so neither configured destination can match it. The
        # allow-list below is the only identity gate it can pass, plus the
        # channel it was observed in having to exist at all.
        if not message.channel_id:
            return _ignore("foreign_channel", "direct message names no channel")
    else:
        if not configured_guild_id or message.guild_id != str(configured_guild_id):
            return _ignore("foreign_guild", "message is not in the configured guild")
        if not configured_channel_id or message.channel_id != str(configured_channel_id):
            return _ignore("foreign_channel", "message is not in the configured channel")
    allowlist = {str(author).strip() for author in authorized_author_ids} - {""}
    if not message.author_id or str(message.author_id) not in allowlist:
        return _ignore("author_not_allowlisted", "author is not on the conversation allowlist")

    tag = normalise_tag(thread_tag)
    conversation_id: str | None = None
    bind_thread = False
    channel = False
    # The sentinel thread only means "direct message" when the gateway said the
    # message is one; a guild channel cannot borrow it.
    direct = message.is_dm and is_dm_thread(message.thread_id)
    if message.thread_id and not direct:
        # Unconditional: an escalation thread never becomes chat, whatever
        # conversation row the caller also found for it.
        if escalation_bound:
            return _ignore("escalation_thread", "thread is bound to an escalation")
        if conversation is None:
            if require_mention:
                return _ignore("unknown_thread", "thread is not bound to a conversation")
            # §2.2: a thread Jack started on any other post binds a new
            # conversation of its own rather than being dropped.
            bind_thread = True
        elif (
            str(conversation.get("transport") or "") != message.transport
            or str(conversation.get("channel_id") or "") != message.channel_id
            or str(conversation.get("external_thread_id") or "") != message.thread_id
        ):
            # Defence in depth: the row must agree with what the gateway
            # observed, so a lookup that grew looser cannot widen which thread
            # is chat.
            return _ignore("unknown_thread", "bound conversation disagrees with the thread")
        else:
            conversation_id = str(conversation["id"])
    else:
        # Top level in the configured channel, or the private channel a direct
        # message lives in. Both are one durable conversation per channel.
        if require_mention and not direct and not message.mentions_bot:
            return _ignore("no_bot_mention", "top-level message does not mention the bot")
        if channel_conversation is not None and not _agrees_with_channel(
            channel_conversation, message
        ):
            return _ignore("unknown_thread", "channel conversation disagrees with the channel")
        if channel_conversation is not None:
            conversation_id = str(channel_conversation["id"])
        elif not require_mention or direct:
            # §2.2: without a mention requirement every top-level message
            # belongs to the channel's one conversation. With one required,
            # the mention is what makes it a conversation at all.
            channel = True

    text = normalise_text(message.text, bot_user_id=bot_user_id)
    if not text:
        return _ignore("empty_text", "message has no text")
    if conversation_id is None:
        return ConversationDecision(
            action=ACTION_OPEN,
            code="open",
            reason=_open_reason(bind_thread=bind_thread, channel=channel),
            text=text,
            bind_thread=bind_thread,
            channel=channel,
            tag=tag,
        )
    return ConversationDecision(
        action=ACTION_FOLLOW_UP,
        code="follow_up",
        reason="message in a bound conversation",
        conversation_id=conversation_id,
        text=text,
        tag=tag,
    )


def _agrees_with_channel(row: Mapping[str, Any], message: ObservedMessage) -> bool:
    """Whether *row* really is the one conversation this channel carries."""
    return (
        str(row.get("transport") or "") == message.transport
        and str(row.get("channel_id") or "") == str(message.channel_id or "")
        and str(row.get("kind") or KIND_CHANNEL) == KIND_CHANNEL
    )


def _open_reason(*, bind_thread: bool, channel: bool) -> str:
    if bind_thread:
        return "thread the operator started in the configured channel"
    if channel:
        return "message in the channel conversation"
    return "bot mention in the configured channel"


__all__ = [
    "ACTION_FOLLOW_UP",
    "ACTION_IGNORE",
    "ACTION_OPEN",
    "CONVERSATION_CODES",
    "DM_GUILD",
    "DM_THREAD_PREFIX",
    "KIND_CHANNEL",
    "KIND_THREAD",
    "ConversationDecision",
    "ObservedMessage",
    "classify_conversation",
    "dm_thread_id",
    "is_dm_thread",
    "normalise_tag",
    "normalise_text",
]
