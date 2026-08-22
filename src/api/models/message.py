"""Response models for message commands (supervisor-agent §6.1).

Note: ``_cmd_message_*`` returns bare dicts (no top-level ``success`` key);
the API layer converts ``{"error": ...}`` responses to HTTP 422.  The models
below reflect the success-branch dict shape and mark unusual/optional fields
so the generated TS client sees stable types.  ``message_send`` remains in
API_EXCLUDED and is *not* modeled here — the chat page uses the dedicated
``POST /api/sessions/{name}/message`` endpoint.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MessageModel(BaseModel):
    """Rendered message dict (see ``src/commands/message_commands.py::message_to_dict``)."""

    model_config = {"extra": "allow", "populate_by_name": True}
    id: str
    project_id: str | None = None
    from_kind: str
    from_id: str
    from_: str | None = Field(
        default=None,
        alias="from",
        serialization_alias="from",
    )  # rendered "kind:id"; keyed as "from" in JSON
    to_kind: str
    to_id: str
    to: str | None = None
    thread_id: str | None = None
    subject: str | None = None
    body: str
    priority: int = 100
    created_at: float | None = None
    delivered_at: float | None = None
    read_at: float | None = None
    read: bool = False
    delivered: bool = False
    archive_after_inject: bool = False
    archived_at: float | None = None
    reply_to_id: str | None = None
    via: str | None = None
    body_kind: str | None = None
    pane_open: dict | None = None


class MessageReplyResponse(BaseModel):
    model_config = {"extra": "allow"}
    message_id: str
    reply_id: str
    reply: MessageModel


class MessageInboxResponse(BaseModel):
    model_config = {"extra": "allow"}
    to_kind: str
    to_id: str
    count: int = 0
    injected: int | None = None
    archived: int | None = None
    messages: list[MessageModel] = []


class MessageListResponse(BaseModel):
    count: int = 0
    messages: list[MessageModel] = []


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    # message_send is API_EXCLUDED — see module docstring.
    "message_reply": MessageReplyResponse,
    "message_inbox": MessageInboxResponse,
    "message_list": MessageListResponse,
}
