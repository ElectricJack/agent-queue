"""Pure conversation briefs, dashboard pointers and fixed notice text."""

from __future__ import annotations

import re
import unicodedata

from src.conversations.limits import MAX_INPUT_CHARS, MAX_REPLY_CHARS, WINDOW_SECONDS
from src.dashboard_paths import conversation_path
from src.discord.links import (
    LINK_PATTERN,
    format_links,
    links_in,
    reserve_links,
    text_prefix,
    text_units,
)
from src.discord.render_budget import link_line
from src.escalations.render import sanitise

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def render_brief(
    *, conversation_id: str, input_id: str, verified_actor: str, text: str, follow_up: bool
) -> str:
    kind = "follow-up" if follow_up else "new input"
    return (
        f"Discord conversation {conversation_id} — {kind} {input_id} from {verified_actor}:\n\n"
        f"{text}\n\n"
        f"Answer with: aq supervisor-inbox reply --conversation-id {conversation_id} "
        f"--input-id {input_id} --idempotency-key {input_id} --text '...' (or --file). "
        "Operational or bulk changes are proposed for dashboard action; conversation text "
        "never resolves a gate or approves work."
    )


def notice_text(kind: str, **facts) -> str:
    """Only bounded, fixed explanations may be sent for an intake refusal."""
    notices = {
        "oversize": f"Message too long. Please send at most {MAX_INPUT_CHARS:,} characters.",
        "rate_limited": f"Too many messages. Please try again after {WINDOW_SECONDS // 60} minutes.",
        "conversation_closed": "This conversation is closed. Send a new message to start another one.",
        "delay": "The supervisor is delayed. Your message is still queued; it has not been answered.",
        "supervisor_missing": (
            "No supervisor is running, so nothing is watching this channel. "
            "Your message is queued."
        ),
        "open_failed": "Unable to open a conversation thread. Your message remains visible in the dashboard.",
    }
    return notices[kind]


#: The offline status line is one line and one count, edited in place (§2.4).
#: It never grows with the queue: the count is what changed, not the history.
STATUS_LINE_MAX_CHARS = 120
STATUS_OFFLINE = "offline"
STATUS_BACK = "back"


def status_line_text(state: str, *, queued: int) -> str:
    """The one status line a conversation shows while a supervisor is away."""
    if state == STATUS_BACK:
        return "▶ Supervisor is back; answering now."
    count = max(1, int(queued or 0))
    plural = "message" if count == 1 else "messages"
    return (f"⏸ Supervisor is offline. {count} {plural} queued; I'll answer when it starts.")[
        :STATUS_LINE_MAX_CHARS
    ]


def conversation_pointer(base_url: str, conversation_id: str) -> str:
    return link_line(base_url, conversation_path(conversation_id))


def sanitise_reply(text: str, *, base_url: str) -> str:
    """Strip controls and neutralise mentions while preserving authored links."""
    text = _ANSI.sub("", text)
    text = "".join(
        char
        for char in text
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    # Mention neutralisation can add one character per @. Do not let the
    # escalation field limit truncate the full answer before we add a pointer.
    return format_links(text, lambda part: sanitise(part, limit=2 * len(part) + 1)).strip()


def conversation_marker(dedup_key: str) -> str:
    """The invisible reconciliation marker carried by every conversation reply."""
    from src.delivery.message import invisible

    return invisible(f"aq-conv:{dedup_key}")


def render_reply(text: str, *, dedup_key: str, base_url: str, conversation_id: str) -> str:
    """Render one reply with its reconciliation marker inside the Discord budget."""
    body = sanitise_reply(text, base_url=base_url)
    marker = conversation_marker(dedup_key)
    complete = f"{body}{marker}"
    if text_units(complete) <= MAX_REPLY_CHARS:
        return complete
    suffix = f" … {conversation_pointer(base_url, conversation_id)}{marker}"
    authored_links = links_in(body)
    if authored_links:
        retained = reserve_links(authored_links, room=MAX_REPLY_CHARS - text_units(suffix) - 1)
        suffix = "\n" + "\n".join(retained) + suffix
        body = LINK_PATTERN.sub("", body)
    budget = MAX_REPLY_CHARS - text_units(suffix)
    if budget < 0:
        raise ValueError("conversation pointer and marker exceed the reply limit")
    return text_prefix(body, budget).rstrip() + suffix
