"""Typed responses for explicit supervisor conversation replies."""

from pydantic import BaseModel


class SupervisorInboxReplyResponse(BaseModel):
    success: bool = True
    created: bool
    reply_message_id: str
    delivery_dedup_key: str
    discord_text_chars: int
    truncated: bool


RESPONSE_MODELS = {"supervisor_inbox_reply": SupervisorInboxReplyResponse}
