"""Response models for the document-review command group."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ReviewErrorResponse(BaseModel):
    """Stable refusal envelope shared by document-review endpoints."""

    model_config = {"extra": "allow"}
    success: bool = False
    error_code: str
    error: str


class ReviewRecord(BaseModel):
    model_config = {"extra": "allow"}

    id: str
    project_id: str
    title: str
    kind: str
    state: str
    current_revision: int
    author_task_id: str | None = None
    vault_path: str
    gate_id: str | None = None
    decider: str = "user"


class ReviewSubmitResponse(BaseModel):
    model_config = {"extra": "allow"}

    success: bool = True
    review_id: str
    revision: int
    vault_path: str


class ReviewShowResponse(BaseModel):
    model_config = {"extra": "allow"}

    success: bool = True
    review: ReviewRecord
    revision: dict[str, Any]
    revisions: list[dict[str, Any]] = []
    vault_state: str
    comments: list[dict[str, Any]] | None = None
    diff: list[dict[str, Any]] | None = None


class ReviewListResponse(BaseModel):
    success: bool = True
    reviews: list[ReviewRecord] = []


class ReviewWithdrawResponse(BaseModel):
    success: bool = True
    review_id: str
    flagged_task_ids: list[str] = []


class ReviewDecideResponse(BaseModel):
    success: bool = True
    review_id: str
    state: str
    unblocked_task_ids: list[str] = []


class ReviewCommentResponse(BaseModel):
    success: bool = True
    comment_id: str


class ReviewDelegateResponse(BaseModel):
    success: bool = True
    review_id: str
    decider: str


class ReviewImportEditsResponse(BaseModel):
    success: bool = True
    review_id: str
    revision: int


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "review_submit": ReviewSubmitResponse,
    "review_show": ReviewShowResponse,
    "review_list": ReviewListResponse,
    "review_withdraw": ReviewWithdrawResponse,
    "review_decide": ReviewDecideResponse,
    "review_comment": ReviewCommentResponse,
    "review_delegate": ReviewDelegateResponse,
    "review_import_edits": ReviewImportEditsResponse,
}
