"""Typed API response models for digest preview, health and §4 authoring."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class DigestWindowBounds(BaseModel):
    since: float
    until: float
    catchup: bool = False


class DigestPreviewResponse(BaseModel):
    """A dry evaluation of the current window; nothing was sent."""

    success: bool = True
    destination: str
    config_generation: int
    enabled: bool
    window: DigestWindowBounds
    would_send: bool
    reason: str
    text: str = ""
    completed_count: int = 0
    active_count: int = 0
    idle_tasks: int = 0
    open_escalations: int = 0
    #: Present exactly when ``would_send`` is false: the §8 reason for silence.
    suppression_reason: str | None = None
    settings_errors: list[str] = []
    warnings: list[str] = []


class DigestScheduleSettings(BaseModel):
    enabled: bool
    interval_minutes: int
    project_ids: list[str] = []
    categories: list[str] = []
    catchup_hours: int
    #: Phase P3 (2026-10-03 §4). Present so the panel can show the cadence the
    #: supervisor actually writes to, and so "off" is visible rather than absent.
    supervisor_authored: bool = False
    cadence_minutes: int = 120
    author_fallback_minutes: int = 10


class DigestEscalationSettings(BaseModel):
    enabled: bool
    mention_user_ids: list[str] = []
    mention_role_ids: list[str] = []
    reminder_minutes: int
    supervisor_delivery_timeout_minutes: int


class DigestWindowRecord(BaseModel):
    id: str
    window_start: float
    window_end: float
    send_status: str
    suppression_reason: str | None = None
    is_catchup: bool = False
    config_generation: int
    attempt_count: int = 0


class DiscordCutoverStatus(BaseModel):
    status: str
    channel_id: str = ""
    migrated_questions: int = 0
    migrated_gates: int = 0
    accepted_answers_preserved: int = 0
    adopted_roots: int = 0
    retired_task_threads: int = 0
    inert_messages: int = 0
    conflicts: list[str] = []


class DiscordIntakeDiagnostics(BaseModel):
    """Inbound Discord messages the gateway ignored, counted by reason code.

    In-memory and sliding: it covers the last ``window_seconds`` and is empty
    after a restart.  ``available`` is false when no gateway is connected.
    """

    available: bool = False
    window_seconds: int = 3600
    total: int = 0
    ignored: dict[str, int] = {}


class DigestStatusResponse(BaseModel):
    success: bool = True
    destination: str
    config_generation: int
    channel_id: str = ""
    digest: DigestScheduleSettings
    escalation: DigestEscalationSettings
    next_evaluation_at: float
    last_window_end: float | None = None
    recent_windows: list[DigestWindowRecord] = []
    #: Digest windows still pending, sending, retrying or of unknown outcome.
    delivery_health: dict[str, int] = {}
    open_escalations: int = 0
    pending_escalation_deliveries: int = 0
    cutover: DiscordCutoverStatus | None = None
    intake: DiscordIntakeDiagnostics = DiscordIntakeDiagnostics()
    settings_errors: list[str] = []
    warnings: list[str] = []


class DigestFactsResponse(BaseModel):
    """One window's frozen evidence, as the author reads it."""

    success: bool = True
    request_id: str
    window_id: str
    #: ``reserved`` | ``requested`` | ``submitted`` | ``fallback`` | ``cancelled``.
    state: str
    deadline: float
    seconds_remaining: float = 0.0
    #: The bounded brief itself; counts exact, lists capped and counted.
    facts: dict[str, Any]
    facts_hash: str


class DigestPostResponse(BaseModel):
    """The rendered post the daemon will send for one window."""

    success: bool = True
    request_id: str
    window_id: str
    state: str
    version: int
    text: str = ""
    characters: int = 0


class DigestRequestResponse(BaseModel):
    """How many held windows this reconciliation handed to the supervisor."""

    success: bool = True
    requested: int = 0
    cancelled: int = 0


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "digest_preview": DigestPreviewResponse,
    "digest_status": DigestStatusResponse,
    "digest_facts": DigestFactsResponse,
    "digest_post": DigestPostResponse,
    "digest_request": DigestRequestResponse,
}
