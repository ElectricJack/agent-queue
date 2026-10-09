"""Typed dashboard/CLI policy profile preview, placement and receipt models."""

from pydantic import BaseModel

from src.policy_profiles.models import Bundle, DiffItem, ExportRequest, ImportRequest, Placeholder


class PolicyFilePreview(BaseModel):
    path: str
    content: str


class PolicyExportResponse(BaseModel):
    success: bool = True
    bundle: Bundle
    checksum: str
    files: list[PolicyFilePreview]
    written: list[str] = []
    archive: str


class PolicyDiffResponse(BaseModel):
    success: bool = True
    name: str
    project_id: str
    items: list[DiffItem]
    placeholders: list[Placeholder]
    values: dict[str, str]


class PolicyReviewReceipt(BaseModel):
    item_id: str
    review_id: str


class PolicyApplyResponse(BaseModel):
    success: bool = True
    applied: list[str]
    reviews: list[PolicyReviewReceipt] = []
    pending_configuration: list[str] = []
    activated: bool = False
    items: list[DiffItem]


RESPONSE_MODELS = {
    "policy_export": PolicyExportResponse,
    "policy_diff": PolicyDiffResponse,
    "policy_apply": PolicyApplyResponse,
}
REQUEST_MODELS = {
    "policy_export": ExportRequest,
    "policy_diff": ImportRequest,
    "policy_apply": ImportRequest,
}
