"""Typed responses for explicit smart test selection."""

from typing import Any

from pydantic import BaseModel


class TestSelectResponse(BaseModel):
    success: bool = True
    selection_id: str
    mode: str
    recorded: bool
    full_required: bool
    full_suite_authorized: bool
    final_modules: list[str]
    ordered: list[str]
    fallback_modules: list[str]
    mandatory_modules: list[str]
    static_modules: list[str]
    jev_modules: list[str] | None
    jev_status: str
    fallback_reason: str | None
    jev_used_for_omission: bool
    reasons: dict[str, list[str]]
    argv: list[list[str]]
    pending_obligations: list[dict[str, Any]]
    record: dict[str, Any]


class TestSelectionRecheckResponse(BaseModel):
    success: bool = True
    stale: bool
    fingerprint: str
    recorded_fingerprint: str


class TestSelectionObserveResponse(BaseModel):
    success: bool = True
    observation_id: str


class TestSelectionShowResponse(BaseModel):
    success: bool = True
    selection: dict[str, Any]
    observations: list[dict[str, Any]]


class TestSelectionListResponse(BaseModel):
    success: bool = True
    selections: list[dict[str, Any]]


class TestSelectionPolicyResponse(BaseModel):
    success: bool = True
    config: dict[str, Any]
    promotion: dict[str, Any] | None
    latest_digests: dict[str, str] | None


class TestSelectionPromoteResponse(BaseModel):
    success: bool = True
    promotion: dict[str, Any]


class TestSelectionRevokeResponse(BaseModel):
    success: bool = True
    revoked: bool


RESPONSE_MODELS = {
    "test_select": TestSelectResponse,
    "test_selection_recheck": TestSelectionRecheckResponse,
    "test_selection_observe": TestSelectionObserveResponse,
    "test_selection_show": TestSelectionShowResponse,
    "test_selection_list": TestSelectionListResponse,
    "test_selection_policy_show": TestSelectionPolicyResponse,
    "test_selection_promote": TestSelectionPromoteResponse,
    "test_selection_revoke": TestSelectionRevokeResponse,
}
