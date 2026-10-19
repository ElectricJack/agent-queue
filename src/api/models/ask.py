"""Versioned source decisions and their durable application result."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.commands.contracts.models import CommandArgs
from src.conversations.envelope import ConversationEnvelope


class AskVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    decision_generation: int
    source_fingerprint: str
    feedback_version: int


class AskSourceRecord(AskVersion):
    model_config = {"extra": "allow"}
    id: str
    project_id: str
    source_kind: str
    source_key: str
    severity: str
    next_actor: str
    display_version: int
    created_at: float
    updated_at: float
    answer_pending: bool = False
    held_while_paused: bool = False
    context_state: str
    context: dict[str, Any]
    application_state: str
    decision_state: str


class AskPreview(BaseModel):
    model_config = {"extra": "allow"}
    id: str
    packet: dict[str, Any]
    packet_sha256: str
    expires_at: float


class AskPrepareResponse(BaseModel):
    success: bool = True
    preview: AskPreview
    expected: AskVersion


class AskDecisionResponse(BaseModel):
    model_config = {"extra": "allow"}
    success: bool
    decision_id: str
    application_state: str
    created: bool | None = None
    receipt: dict[str, Any] | None = None


RESPONSE_MODELS = {
    "ask_prepare_decision": AskPrepareResponse,
    "ask_decide": AskDecisionResponse,
    "ask_retry_operation": AskDecisionResponse,
}


class AskListArgs(CommandArgs):
    source_kind: str | None = None
    project_id: str | None = None
    view: Literal["needs_you", "waiting", "history"] = "needs_you"
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None


class AskShowArgs(CommandArgs):
    alias_kind: Literal["review", "escalation"] | None = None
    ask_id: str = Field(min_length=1)
    input_id: str | None = None


class AskHistoryArgs(CommandArgs):
    ask_id: str = Field(min_length=1)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None


class AskReplyArgs(CommandArgs):
    ask_id: str = Field(min_length=1)
    intent: Literal["discuss"] = "discuss"
    text: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected: AskVersion
    post_decision: bool = False


class AskPrepareArgs(CommandArgs):
    ask_id: str = Field(min_length=1)
    expected: AskVersion
    option_key: str = Field(min_length=1)
    overall_note: str = ""
    dispositions: list[dict[str, Any]] = Field(default_factory=list)
    responder_class: str | None = None


class AskDecideArgs(AskPrepareArgs):
    preview_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class AskApplyInputArgs(CommandArgs):
    ask_id: str = Field(min_length=1)
    input_id: str = Field(min_length=1)
    expected: AskVersion
    option_key: str = Field(min_length=1)
    interpretation: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class AskDiscussionReplyArgs(CommandArgs):
    ask_id: str = Field(min_length=1)
    input_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=15000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class AskUpdateContextArgs(CommandArgs):
    ask_id: str = Field(min_length=1)
    expected: AskVersion


class AskReconcileArgs(CommandArgs):
    project_id: str | None = None
    apply: bool = False
    limit: int = Field(default=100, ge=1, le=100)
    case_id: str | None = None
    disposition: Literal["mapped_work", "reviewed_no_action", "retain_unknown"] | None = None
    evidence: str | None = Field(default=None, min_length=1, max_length=4000)


class AskRetryOperationArgs(CommandArgs):
    decision_id: str = Field(min_length=1)


class AskCutoverArgs(CommandArgs):
    project_id: str
    mode: Literal["legacy", "shadow", "dashboard", "transferring", "channel", "delivery_disabled"]
    expected_generation: int = Field(ge=0)
    inventory_sha256: str | None = None
    evidence: str | None = Field(default=None, min_length=1, max_length=4000)
    legacy_controls_disabled: bool = False
    aliases_redirected: bool = False


class AskCutoverStatusArgs(CommandArgs):
    project_id: str = Field(min_length=1)


class AskCutoverDestination(BaseModel):
    destination: str
    mode: str
    generation: int
    migrated: bool
    first_switch_utc: str | None
    compatibility_deadline: float | None
    compatibility_deadline_utc: str | None


class AskCutoverStatusResult(BaseModel):
    project_id: str
    configured_destination: str
    destinations: list[AskCutoverDestination]


class AskCutoverStatusResponse(BaseModel):
    success: bool = True
    result: AskCutoverStatusResult


class AskRepairDeliveryArgs(CommandArgs):
    delivery_id: str
    expected_generation: int = Field(ge=0)
    disposition: Literal["confirmed_sent", "confirmed_absent", "cancel"]
    evidence: str = Field(min_length=1, max_length=4000)


class AskEnvelope(ConversationEnvelope):
    reference_message_id: str | None = None

    @field_validator("reference_message_id")
    @classmethod
    def reference_snowflake(cls, value):
        return cls.snowflake(value)


class AskIntakeArgs(CommandArgs):
    envelope: AskEnvelope
    source: Literal["gateway", "backfill"] = "gateway"


class AskButtonArgs(CommandArgs):
    interaction_id: str
    custom_id: str
    guild_id: str
    channel_id: str
    message_id: str
    author_id: str


class AskListResponse(BaseModel):
    view: str = "needs_you"
    identity_hash: str
    success: bool = True
    items: list[AskSourceRecord]
    count: int
    snapshot_at: float
    next_cursor: str | None = None
    source_health: str


class AskShowResponse(BaseModel):
    success: bool = True
    ask: AskSourceRecord
    expected: AskVersion
    document: dict[str, Any] | None = None
    inputs: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    history: list[dict[str, Any]] = Field(default_factory=list)
    obligations: list[dict[str, Any]] = Field(default_factory=list)
    operations: list[dict[str, Any]] = Field(default_factory=list)
    decision: dict[str, Any] | None = None
    source_health: str = "healthy"


class AskHistoryResponse(BaseModel):
    success: bool = True
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class AskInputResponse(BaseModel):
    success: bool = True
    created: bool
    input_id: str
    receipt: dict[str, Any]


class AskDiscussionResponse(BaseModel):
    success: bool = True
    created: bool
    ask_id: str
    input_id: str
    entry_id: str
    version: dict[str, Any]
    delivery_id: str | None = None


class AskControlResponse(BaseModel):
    success: bool
    result: dict[str, Any]


class AskErrorResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    success: bool = False
    error: str
    error_code: str
    fields: dict[str, Any] = Field(default_factory=dict)


ASK_ARGS = {
    "ask_list": AskListArgs,
    "ask_show": AskShowArgs,
    "ask_history": AskHistoryArgs,
    "ask_reply": AskReplyArgs,
    "ask_prepare_decision": AskPrepareArgs,
    "ask_decide": AskDecideArgs,
    "ask_apply_input": AskApplyInputArgs,
    "ask_discussion_reply": AskDiscussionReplyArgs,
    "ask_update_context": AskUpdateContextArgs,
    "ask_reconcile": AskReconcileArgs,
    "ask_cutover": AskCutoverArgs,
    "ask_cutover_status": AskCutoverStatusArgs,
    "ask_retry_operation": AskRetryOperationArgs,
    "ask_repair_delivery": AskRepairDeliveryArgs,
    "ask_intake": AskIntakeArgs,
    "ask_button": AskButtonArgs,
}
RESPONSE_MODELS.update(
    {
        "ask_list": AskListResponse,
        "ask_show": AskShowResponse,
        "ask_history": AskHistoryResponse,
        "ask_reply": AskInputResponse,
        "ask_intake": AskInputResponse,
        "ask_apply_input": AskDecisionResponse,
        "ask_discussion_reply": AskDiscussionResponse,
        "ask_update_context": AskControlResponse,
        "ask_reconcile": AskControlResponse,
        "ask_cutover": AskControlResponse,
        "ask_cutover_status": AskCutoverStatusResponse,
        "ask_repair_delivery": AskControlResponse,
        "ask_button": AskInputResponse,
    }
)
