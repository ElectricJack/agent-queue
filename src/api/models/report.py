"""Typed responses for supervisor report commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ReportReconcileResponse(BaseModel):
    success: bool = True
    requested: int


class ReportRequestResponse(BaseModel):
    success: bool = True
    request_id: str
    state: str
    message_id: str
    deadline: float


class MorningReportPreviewResponse(BaseModel):
    success: bool = True
    brief: dict[str, Any]
    brief_hash: str
    would_suppress: bool
    reason: str


class ReportBriefResponse(BaseModel):
    success: bool = True
    request_id: str
    state: str
    deadline: float
    version: int
    brief_hash: str
    brief: dict[str, Any]
    facts: list[dict[str, Any]]
    active: list[dict[str, Any]]
    total_facts: int
    total_active: int


class ReportSubmitResponse(BaseModel):
    success: bool = True
    request_id: str
    window_id: str
    state: str
    version: int


class ReportItem(BaseModel):
    refs: list[str]
    text: str
    task_id: str | None = None
    late: bool = False
    shipment: str | None = None
    prior_verification: str = ""
    verification_label: str = "agent-reported"


class ManualCheck(BaseModel):
    action: str
    surface: str
    expected_result: str
    reason: str
    refs: list[str]
    prior_verification: str
    confidence: str


class ReportProject(BaseModel):
    id: str
    name: str = ""
    landed: list[ReportItem]
    pending: list[ReportItem]
    failures: list[ReportItem]
    manual_checks: list[ManualCheck]


class ReportContent(BaseModel):
    version: int
    summary: str
    projects: list[ReportProject]
    coverage: dict[str, Any]
    global_facts: list[ReportItem] = []
    omitted: dict[str, int] = {}


class MorningReportRecord(BaseModel):
    id: str
    state: str
    reason: str | None
    local_date: str
    timezone: str
    planned_at: float
    window_start: float
    window_end: float
    brief_hash: str | None
    created_at: float
    finalized_at: float | None
    author_deadline: float
    report: ReportContent | None
    is_fallback: bool


class ReportGetResponse(BaseModel):
    success: bool = True
    report: MorningReportRecord


class ReportListResponse(BaseModel):
    success: bool = True
    reports: list[MorningReportRecord]


class MorningReportTickResponse(BaseModel):
    success: bool = True
    report_id: str | None
    state: str
    reason: str | None
    next_due_at: float | None
    cancelled: int


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "morning_report_tick": MorningReportTickResponse,
    "report_get": ReportGetResponse,
    "report_list": ReportListResponse,
    "morning_report_preview": MorningReportPreviewResponse,
    "report_request": ReportRequestResponse,
    "report_reconcile": ReportReconcileResponse,
    "report_brief": ReportBriefResponse,
    "report_submit": ReportSubmitResponse,
}
