"""Decide whether an inbound transport message is a reply to an incident.

This is the mirror of :mod:`src.escalations.plan`: where the planner decides
what the incident owes the outside world, the intake decides what the outside
world is allowed to say back.  It is a pure function of the message's *server
observed* facts and the durable thread binding, which is what makes §5's
"adapter identity must be established by trusted server context" checkable
without a gateway.

Nothing here reads a clock, touches the database or trusts a field the author
could set.  In particular the author ID is the one the gateway reports for the
connection, never a body field: :func:`classify_inbound` is given the observed
value and the configured allowlist and does no more than compare them.

The refusals are deliberately silent — §7's channel is shared with humans, and
answering every unrelated line with "that is not an escalation" would turn the
one configured channel into a chatbot, which is exactly what the product
contract forbids.  ``IntakeDecision.reason`` is the human-readable why, and
``IntakeDecision.code`` is its stable short form: the one the adapter's single
INFO line per ignored message carries, and the one an operator greps for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.escalations.facts import TERMINAL_STATES

#: The message is a verified human reply: persist it and notify the supervisor.
ACTION_ACCEPT = "accept"
#: The message correlates to an incident that is already closed.  It is still
#: persisted as history (``accept_escalation_reply`` retains it without queuing
#: supervisor work), and the thread gets closed-state guidance rather than an
#: acknowledgement that implies somebody is still reviewing.
ACTION_CLOSED = "closed"
#: Not ours.  The adapter falls through to whatever it would otherwise do.
ACTION_IGNORE = "ignore"

#: Longest reply the core will persist; mirrors ``accept_escalation_reply``.
MAX_REPLY_CHARS = 16000


@dataclass(frozen=True)
class InboundMessage:
    """What a transport gateway observed, with nothing the author asserted.

    ``author_id`` is the connection's authenticated account as the gateway
    reports it.  ``channel_id`` is the *parent* channel of ``thread_id``, so a
    thread that has been moved out of the configured channel stops correlating
    on its own.
    """

    transport: str
    external_message_id: str
    channel_id: str | None
    thread_id: str | None
    author_id: str
    text: str
    author_is_bot: bool = False
    is_own_message: bool = False
    received_sequence: int | None = None
    guild_id: str | None = None


@dataclass(frozen=True)
class IntakeDecision:
    """What the adapter should do with one inbound message.

    ``code`` is ``"accepted"``, ``"closed"`` or the :data:`REASON_CODES` entry
    for an ignore; unlike ``reason`` it never changes wording.
    """

    action: str
    reason: str
    escalation_id: str | None = None
    project_id: str | None = None
    state: str | None = None
    code: str = ""

    @property
    def correlated(self) -> bool:
        return self.action in (ACTION_ACCEPT, ACTION_CLOSED)


#: Every refusal :func:`classify_inbound` can return, reason -> stable code,
#: in gate order.  One entry per ``_ignore(...)`` call; a new gate without a
#: code fails loudly rather than logging an unnamed refusal.
REASON_CODES: dict[str, str] = {
    "escalation intake is disabled": "disabled",
    "message was authored by this bot": "own_message",
    "message was authored by a bot": "bot_author",
    "message is not in a thread": "not_in_thread",
    "no configured channel": "no_channel",
    "thread is not in the configured channel": "foreign_channel",
    "author is not on the escalation reply allowlist": "author_not_allowlisted",
    "thread is not bound to an escalation": "thread_unbound",
    "bound channel disagrees with the observed channel": "binding_channel_mismatch",
    "bound thread disagrees with the observed thread": "binding_thread_mismatch",
    "reply has no text": "empty_text",
    f"reply exceeds {MAX_REPLY_CHARS} characters": "oversize",
    "message has no transport identity": "no_message_id",
}

#: The one INFO line per ignored message: ids and the code, never content.
IGNORE_LOG_FORMAT = "discord intake ignored reason=%s guild=%s channel=%s message=%s author=%s"


def _ignore(reason: str) -> IntakeDecision:
    return IntakeDecision(action=ACTION_IGNORE, reason=reason, code=REASON_CODES[reason])


def classify_inbound(
    message: InboundMessage,
    *,
    configured_channel_id: str | None,
    authorized_author_ids: Sequence[str],
    binding: Mapping[str, Any] | None,
    enabled: bool = True,
) -> IntakeDecision:
    """Whether *message* is an authorized human reply to a known incident.

    ``binding`` is the row :meth:`~src.database.queries.escalation_queries
    .EscalationQueriesMixin.find_escalation_by_thread` returned for the
    message's ``(channel_id, thread_id)``; ``None`` means the thread is not
    one of ours.  Every gate below must pass, and each returns its own reason
    so an operator can tell "unknown thread" from "not on the allowlist".
    """
    if not enabled:
        return _ignore("escalation intake is disabled")
    # Loop guards first: a bot's own post in its own thread must never become
    # a reply, or the acknowledgement of a reply acknowledges itself forever.
    if message.is_own_message:
        return _ignore("message was authored by this bot")
    if message.author_is_bot:
        return _ignore("message was authored by a bot")
    if not message.thread_id:
        # Channel-level chatter, a DM or a mention outside a thread.  §7 puts
        # follow-ups in the incident's thread and nowhere else.
        return _ignore("message is not in a thread")
    if not configured_channel_id:
        return _ignore("no configured channel")
    if not message.channel_id or message.channel_id != configured_channel_id:
        return _ignore("thread is not in the configured channel")
    if not message.author_id or message.author_id not in set(authorized_author_ids):
        return _ignore("author is not on the escalation reply allowlist")
    if binding is None:
        return _ignore("thread is not bound to an escalation")
    # Defence in depth: the binding must agree with what the gateway observed.
    # A query that ever grew looser must not silently widen who can reply.
    if str(binding.get("delivery_channel_id") or "") != message.channel_id:
        return _ignore("bound channel disagrees with the observed channel")
    if str(binding.get("delivery_thread_id") or "") != message.thread_id:
        return _ignore("bound thread disagrees with the observed thread")
    text = (message.text or "").strip()
    if not text:
        return _ignore("reply has no text")
    if len(text) > MAX_REPLY_CHARS:
        return _ignore(f"reply exceeds {MAX_REPLY_CHARS} characters")
    if not message.external_message_id:
        return _ignore("message has no transport identity")

    escalation_id = str(binding["id"])
    project_id = str(binding["project_id"])
    state = str(binding.get("state") or "")
    if state in TERMINAL_STATES:
        return IntakeDecision(
            action=ACTION_CLOSED,
            reason=f"escalation is {state}",
            escalation_id=escalation_id,
            project_id=project_id,
            state=state,
            code="closed",
        )
    return IntakeDecision(
        action=ACTION_ACCEPT,
        reason="verified human reply",
        escalation_id=escalation_id,
        project_id=project_id,
        state=state,
        code="accepted",
    )


__all__ = [
    "ACTION_ACCEPT",
    "ACTION_CLOSED",
    "ACTION_IGNORE",
    "IGNORE_LOG_FORMAT",
    "MAX_REPLY_CHARS",
    "REASON_CODES",
    "InboundMessage",
    "IntakeDecision",
    "classify_inbound",
]
