from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="GithubIssueFixApprovedRequest")


@_attrs_define
class GithubIssueFixApprovedRequest:
    """
    Attributes:
        project_id (str):
        review_id (str):
        revision (int):
    """

    project_id: str
    review_id: str
    revision: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        review_id = self.review_id

        revision = self.revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "review_id": review_id,
                "revision": revision,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        review_id = d.pop("review_id")

        revision = d.pop("revision")

        github_issue_fix_approved_request = cls(
            project_id=project_id,
            review_id=review_id,
            revision=revision,
        )

        github_issue_fix_approved_request.additional_properties = d
        return github_issue_fix_approved_request

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
