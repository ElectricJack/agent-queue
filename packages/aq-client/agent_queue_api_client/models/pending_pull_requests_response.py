from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pending_pull_request import PendingPullRequest


T = TypeVar("T", bound="PendingPullRequestsResponse")


@_attrs_define
class PendingPullRequestsResponse:
    """
    Attributes:
        pull_requests (list[PendingPullRequest]):
        snapshot_at (float | None | Unset):
        snapshot_age_seconds (float | None | Unset):
        can_approve (bool | Unset):  Default: False.
    """

    pull_requests: list[PendingPullRequest]
    snapshot_at: float | None | Unset = UNSET
    snapshot_age_seconds: float | None | Unset = UNSET
    can_approve: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        pull_requests = []
        for pull_requests_item_data in self.pull_requests:
            pull_requests_item = pull_requests_item_data.to_dict()
            pull_requests.append(pull_requests_item)

        snapshot_at: float | None | Unset
        if isinstance(self.snapshot_at, Unset):
            snapshot_at = UNSET
        else:
            snapshot_at = self.snapshot_at

        snapshot_age_seconds: float | None | Unset
        if isinstance(self.snapshot_age_seconds, Unset):
            snapshot_age_seconds = UNSET
        else:
            snapshot_age_seconds = self.snapshot_age_seconds

        can_approve = self.can_approve

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "pull_requests": pull_requests,
            }
        )
        if snapshot_at is not UNSET:
            field_dict["snapshot_at"] = snapshot_at
        if snapshot_age_seconds is not UNSET:
            field_dict["snapshot_age_seconds"] = snapshot_age_seconds
        if can_approve is not UNSET:
            field_dict["can_approve"] = can_approve

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pending_pull_request import PendingPullRequest

        d = dict(src_dict)
        pull_requests = []
        _pull_requests = d.pop("pull_requests")
        for pull_requests_item_data in _pull_requests:
            pull_requests_item = PendingPullRequest.from_dict(pull_requests_item_data)

            pull_requests.append(pull_requests_item)

        def _parse_snapshot_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        snapshot_at = _parse_snapshot_at(d.pop("snapshot_at", UNSET))

        def _parse_snapshot_age_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        snapshot_age_seconds = _parse_snapshot_age_seconds(d.pop("snapshot_age_seconds", UNSET))

        can_approve = d.pop("can_approve", UNSET)

        pending_pull_requests_response = cls(
            pull_requests=pull_requests,
            snapshot_at=snapshot_at,
            snapshot_age_seconds=snapshot_age_seconds,
            can_approve=can_approve,
        )

        pending_pull_requests_response.additional_properties = d
        return pending_pull_requests_response

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
