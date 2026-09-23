from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.pending_pull_request import PendingPullRequest


T = TypeVar("T", bound="PendingPullRequestsResponse")


@_attrs_define
class PendingPullRequestsResponse:
    """
    Attributes:
        pull_requests (list[PendingPullRequest]):
    """

    pull_requests: list[PendingPullRequest]
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        pull_requests = []
        for pull_requests_item_data in self.pull_requests:
            pull_requests_item = pull_requests_item_data.to_dict()
            pull_requests.append(pull_requests_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "pull_requests": pull_requests,
            }
        )

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

        pending_pull_requests_response = cls(
            pull_requests=pull_requests,
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
