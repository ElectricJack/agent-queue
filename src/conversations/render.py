"""Pure conversation briefs, dashboard pointers and fixed notice text."""

from __future__ import annotations

from src.conversations.limits import MAX_INPUT_CHARS, WINDOW_SECONDS


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
    return f"{base_url.rstrip('/')}/conversations/{conversation_id}"
