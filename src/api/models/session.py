"""Response models for session commands (session-runtime spec §3, §5)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SessionSummary(BaseModel):
    """One row of ``session_list`` output.

    ``idle_seconds`` and ``stalled`` are derived per-row in
    ``_cmd_session_list``; every other field mirrors ``sessions`` table
    columns via ``SessionCommandsMixin._session_dict``.
    """

    id: str
    name: str
    task_id: str | None = None
    agent_id: str | None = None
    model: str | None = None
    intelligence_class: str | None = None
    ended_at: float | None = None
    end_reason: str | None = None
    project_id: str | None = None
    profile_id: str | None = None
    harness: str | None = None
    provider: str | None = None
    lifecycle: str | None = None
    state: str | None = None
    work_dir: str | None = None
    started_at: float | None = None
    last_activity: float | None = None
    restarts: int = 0
    quarantined_at: float | None = None
    sleep_reason: str | None = None
    # ``sessions.epoch`` is a Text column (AQ_DAEMON_EPOCH — a hex string
    # such as "5b8c0ab48772"), not an integer.  Declaring it as ``int``
    # made ``POST /api/system/session-list`` 500 with ResponseValidationError
    # on any live daemon row.  Match the schema in src/database/tables.py.
    epoch: str | None = None
    idle_seconds: float = 0.0
    stalled: bool = False


class ListSessionsResponse(BaseModel):
    success: bool = True
    sessions: list[SessionSummary] = []
    count: int = 0


class ShowSessionResponse(BaseModel):
    success: bool = True
    session: SessionSummary


class SessionPeekResponse(BaseModel):
    success: bool = True
    session_id: str
    output: str = ""
    note: str | None = None


class SessionAttachResponse(BaseModel):
    success: bool = True
    session_id: str
    attach_command: str


class SessionNudgeResponse(BaseModel):
    success: bool
    session_id: str
    delivered: bool = False
    error: str | None = None


class SessionInputResponse(BaseModel):
    success: bool = True
    session_id: str
    accepted: bool = True


class TranscriptEntryModel(BaseModel):
    uuid: str
    parent_uuid: str | None = None
    type: str
    text: str = ""
    model: str | None = None
    usage: dict[str, Any] | None = None
    ts: float = 0.0


class SessionLogsResponse(BaseModel):
    """Union: transcript entries OR peek-fallback string output.

    ``source`` discriminates; extra keys allowed so a peek-fallback row
    that echoes ``note`` from ``_cmd_session_peek`` still validates.
    """

    model_config = {"extra": "allow"}
    success: bool = True
    session_id: str
    source: str = "transcript"
    entries: list[TranscriptEntryModel] | None = None
    output: str | None = None


class SessionKillResponse(BaseModel):
    model_config = {"extra": "allow"}
    success: bool = True
    session_id: str


class SessionDesiredStateResponse(BaseModel):
    """``session_sleep`` / ``session_wake`` — intent, not observation.

    Both fields are returned so a caller can see the gap it just opened:
    ``desired_state`` is what was written, ``state`` is what the runtime
    still shows until the reconciler converges.
    """

    model_config = {"extra": "allow"}
    success: bool = True
    session_id: str
    desired_state: str
    state: str | None = None


class SessionTokenResponse(BaseModel):
    """``session_token`` — a freshly minted bearer token for one session.

    Dev/e2e facility (see ``SessionCommandsMixin._cmd_session_token``).
    ``task_id`` is populated only for a task session; a pool worker's
    scope pins no task.
    """

    model_config = {"extra": "allow"}
    success: bool = True
    session_id: str
    project_id: str | None = None
    task_id: str | None = None
    token: str


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "session_list": ListSessionsResponse,
    "session_show": ShowSessionResponse,
    "session_peek": SessionPeekResponse,
    "session_attach": SessionAttachResponse,
    "session_nudge": SessionNudgeResponse,
    "session_input": SessionInputResponse,
    "session_logs": SessionLogsResponse,
    "session_kill": SessionKillResponse,
    "session_sleep": SessionDesiredStateResponse,
    "session_wake": SessionDesiredStateResponse,
    "session_token": SessionTokenResponse,
}
