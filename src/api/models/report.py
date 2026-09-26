"""Typed responses for supervisor report commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


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


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "morning_report_preview": MorningReportPreviewResponse,
    "report_request": ReportRequestResponse,
    "report_brief": ReportBriefResponse,
    "report_submit": ReportSubmitResponse,
}
