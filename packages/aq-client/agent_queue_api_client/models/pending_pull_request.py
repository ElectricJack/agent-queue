from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.pending_pull_request_ci_status import PendingPullRequestCiStatus
from ..models.pending_pull_request_review_decision import PendingPullRequestReviewDecision
from ..types import UNSET, Unset

T = TypeVar("T", bound="PendingPullRequest")


@_attrs_define
class PendingPullRequest:
    """
    Attributes:
        title (str):
        url (str):
        repository (str):
        project_id (str):
        project_name (str):
        task_id (str):
        head_sha (str):
        state (Literal['open'] | Unset):  Default: 'open'.
        opened_at (float | None | Unset):
        review_decision (PendingPullRequestReviewDecision | Unset):  Default: PendingPullRequestReviewDecision.PENDING.
        ci_status (PendingPullRequestCiStatus | Unset):  Default: PendingPullRequestCiStatus.PENDING.
        train_state (None | str | Unset):
    """

    title: str
    url: str
    repository: str
    project_id: str
    project_name: str
    task_id: str
    head_sha: str
    state: Literal["open"] | Unset = "open"
    opened_at: float | None | Unset = UNSET
    review_decision: PendingPullRequestReviewDecision | Unset = PendingPullRequestReviewDecision.PENDING
    ci_status: PendingPullRequestCiStatus | Unset = PendingPullRequestCiStatus.PENDING
    train_state: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        title = self.title

        url = self.url

        repository = self.repository

        project_id = self.project_id

        project_name = self.project_name

        task_id = self.task_id

        head_sha = self.head_sha

        state = self.state

        opened_at: float | None | Unset
        if isinstance(self.opened_at, Unset):
            opened_at = UNSET
        else:
            opened_at = self.opened_at

        review_decision: str | Unset = UNSET
        if not isinstance(self.review_decision, Unset):
            review_decision = self.review_decision.value

        ci_status: str | Unset = UNSET
        if not isinstance(self.ci_status, Unset):
            ci_status = self.ci_status.value

        train_state: None | str | Unset
        if isinstance(self.train_state, Unset):
            train_state = UNSET
        else:
            train_state = self.train_state

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "title": title,
                "url": url,
                "repository": repository,
                "project_id": project_id,
                "project_name": project_name,
                "task_id": task_id,
                "head_sha": head_sha,
            }
        )
        if state is not UNSET:
            field_dict["state"] = state
        if opened_at is not UNSET:
            field_dict["opened_at"] = opened_at
        if review_decision is not UNSET:
            field_dict["review_decision"] = review_decision
        if ci_status is not UNSET:
            field_dict["ci_status"] = ci_status
        if train_state is not UNSET:
            field_dict["train_state"] = train_state

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        title = d.pop("title")

        url = d.pop("url")

        repository = d.pop("repository")

        project_id = d.pop("project_id")

        project_name = d.pop("project_name")

        task_id = d.pop("task_id")

        head_sha = d.pop("head_sha")

        state = cast(Literal["open"] | Unset, d.pop("state", UNSET))
        if state != "open" and not isinstance(state, Unset):
            raise ValueError(f"state must match const 'open', got '{state}'")

        def _parse_opened_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        opened_at = _parse_opened_at(d.pop("opened_at", UNSET))

        _review_decision = d.pop("review_decision", UNSET)
        review_decision: PendingPullRequestReviewDecision | Unset
        if isinstance(_review_decision, Unset):
            review_decision = UNSET
        else:
            review_decision = PendingPullRequestReviewDecision(_review_decision)

        _ci_status = d.pop("ci_status", UNSET)
        ci_status: PendingPullRequestCiStatus | Unset
        if isinstance(_ci_status, Unset):
            ci_status = UNSET
        else:
            ci_status = PendingPullRequestCiStatus(_ci_status)

        def _parse_train_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        train_state = _parse_train_state(d.pop("train_state", UNSET))

        pending_pull_request = cls(
            title=title,
            url=url,
            repository=repository,
            project_id=project_id,
            project_name=project_name,
            task_id=task_id,
            head_sha=head_sha,
            state=state,
            opened_at=opened_at,
            review_decision=review_decision,
            ci_status=ci_status,
            train_state=train_state,
        )

        pending_pull_request.additional_properties = d
        return pending_pull_request

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
