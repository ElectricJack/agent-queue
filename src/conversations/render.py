"""Pure conversation briefs, dashboard pointers and fixed notice text."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote, urlsplit

from src.conversations.limits import MAX_INPUT_CHARS, MAX_REPLY_CHARS, WINDOW_SECONDS
from src.escalations.render import sanitise

_URL = re.compile(r"https?://[^\s<>()\[\]`]+", re.IGNORECASE)
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
        "conversation_closed": "This conversation is closed. Please mention Agent Q in a new message.",
        "delay": "The supervisor is delayed. Your message is still queued; it has not been answered.",
        "open_failed": "Unable to open a conversation thread. Your message remains visible in the dashboard.",
    }
    return notices[kind]


def conversation_pointer(base_url: str, conversation_id: str) -> str:
    return f"{base_url.rstrip('/')}/conversations/{quote(conversation_id, safe='')}"


def sanitise_reply(text: str, *, base_url: str) -> str:
    """Strip controls and foreign links, then apply the shared mention policy."""
    text = _ANSI.sub("", text)
    text = "".join(
        char
        for char in text
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    base = base_url.rstrip("/")
    allowed = urlsplit(base)

    def link(match: re.Match[str]) -> str:
        url = match.group()
        try:
            parsed = urlsplit(url)
        except ValueError:
            return "[link removed]"
        if (
            base
            and allowed.scheme in {"http", "https"}
            and allowed.netloc
            and parsed.scheme == allowed.scheme
            and parsed.netloc == allowed.netloc
            and (url == base or url.startswith((base + "/", base + "?", base + "#")))
        ):
            return url
        return "[link removed]"

    text = _URL.sub(link, text)
    # Mention neutralisation can add one character per @. Do not let the
    # escalation field limit truncate the full answer before we add a pointer.
    return sanitise(text, limit=2 * len(text) + 1)


def render_reply(text: str, *, dedup_key: str, base_url: str, conversation_id: str) -> str:
    """Render one reply with its reconciliation marker inside the Discord budget."""
    body = sanitise_reply(text, base_url=base_url)
    marker = f"(aq-conv:{dedup_key})"
    complete = f"{body} {marker}"
    if len(complete) <= MAX_REPLY_CHARS:
        return complete
    suffix = f" … {conversation_pointer(base_url, conversation_id)} {marker}"
    budget = MAX_REPLY_CHARS - len(suffix)
    if budget < 0:
        raise ValueError("conversation pointer and marker exceed the reply limit")
    # A partial allowed URL could point elsewhere. Drop the whole link when
    # the cut would bisect it, then append the complete trusted pointer.
    for match in _URL.finditer(body):
        if match.start() < budget < match.end():
            budget = match.start()
            break
    return body[:budget].rstrip() + suffix
