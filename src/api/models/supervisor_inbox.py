"""Typed global supervisor conversation reads and explicit replies."""

from typing import Any, Literal

from pydantic import BaseModel

from src.commands.contracts.supervisor_inbox import (
    SupervisorInboxHistoryArgs,
    SupervisorInboxReplyArgs,
    SupervisorInboxStatusArgs,
)


class SupervisorInboxReplyRequest(SupervisorInboxReplyArgs):
    pass


class SupervisorInboxStatusRequest(SupervisorInboxStatusArgs):
    pass


class SupervisorInboxHistoryRequest(SupervisorInboxHistoryArgs):
    pass


class SupervisorInboxReplyResponse(BaseModel):
    success: bool = True
    created: bool
    reply_message_id: str
    delivery_dedup_key: str
    discord_text_chars: int
    truncated: bool


class ConversationPreconditions(BaseModel):
    ok: bool
    unmet: list[str]


class ConversationDiagnostics(BaseModel):
    message_content_intent: bool | None
    permissions: dict[str, bool] | None
    outbox_bound: bool


class ConversationLimits(BaseModel):
    max_input_chars: int
    author_window_limit: int
    channel_window_limit: int
    window_seconds: int
    max_reply_chars: int


class ConversationCounts(BaseModel):
    by_state: dict[str, int]
    inputs_pending_supervisor: int


class ConversationBackfill(BaseModel):
    cursors: list[dict[str, Any]]
    gaps: list[dict[str, Any]]


class ConversationIntake(BaseModel):
    available: bool
    window_seconds: int
    total: int
    ignored: dict[str, int]


class SupervisorInboxStatusResponse(BaseModel):
    success: bool = True
    enabled: bool
    preconditions: ConversationPreconditions
    diagnostics: ConversationDiagnostics
    limits: ConversationLimits
    counts: ConversationCounts
    backfill: ConversationBackfill
    intake: ConversationIntake


class ConversationInputRecord(BaseModel):
    model_config = {"extra": "allow"}

    id: str
    conversation_id: str
    verified_actor: str
    text: str | None
    text_expired: bool
    state: str
    received_at: float
    reply_message_id: str | None
    reply_body: str | None
    reply_created_at: float | None


class ConversationHistoryRecord(BaseModel):
    model_config = {"extra": "allow"}

    id: str
    transport: str
    guild_id: str
    channel_id: str
    external_root_message_id: str
    external_thread_id: str | None
    thread_id: str
    created_by: str
    audience: list[str]
    state: Literal["opening", "open", "closed", "delivery_blocked"]
    created_at: float
    updated_at: float
    closed_at: float | None
    inputs: list[ConversationInputRecord]
    next_before: float | None


class SupervisorInboxHistoryResponse(BaseModel):
    success: bool = True
    conversations: list[ConversationHistoryRecord]
    next_before: float | None


class SupervisorInboxErrorResponse(BaseModel):
    model_config = {"extra": "allow"}

    success: bool = False
    error_code: str
    error: str


RESPONSE_MODELS = {
    "supervisor_inbox_reply": SupervisorInboxReplyResponse,
    "supervisor_inbox_status": SupervisorInboxStatusResponse,
    "supervisor_inbox_history": SupervisorInboxHistoryResponse,
}

# Use explicit models so state choices and pagination bounds survive codegen.
REQUEST_MODELS = {
    "supervisor_inbox_reply": SupervisorInboxReplyRequest,
    "supervisor_inbox_status": SupervisorInboxStatusRequest,
    "supervisor_inbox_history": SupervisorInboxHistoryRequest,
}
