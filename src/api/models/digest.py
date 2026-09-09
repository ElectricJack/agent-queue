"""Typed API response models for hourly-digest preview and schedule health."""

from __future__ import annotations

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
    settings_errors: list[str] = []
    warnings: list[str] = []


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "digest_preview": DigestPreviewResponse,
    "digest_status": DigestStatusResponse,
}
