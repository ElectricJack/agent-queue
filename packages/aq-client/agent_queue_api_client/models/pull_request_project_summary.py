from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="PullRequestProjectSummary")


@_attrs_define
class PullRequestProjectSummary:
    """
    Attributes:
        project_id (str):
        project_name (str):
        repository (str):
        pulls_url (str):
        open_pr_count (int):
    """

    project_id: str
    project_name: str
    repository: str
    pulls_url: str
    open_pr_count: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        project_name = self.project_name

        repository = self.repository

        pulls_url = self.pulls_url

        open_pr_count = self.open_pr_count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "project_name": project_name,
                "repository": repository,
                "pulls_url": pulls_url,
                "open_pr_count": open_pr_count,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        project_name = d.pop("project_name")

        repository = d.pop("repository")

        pulls_url = d.pop("pulls_url")

        open_pr_count = d.pop("open_pr_count")

        pull_request_project_summary = cls(
            project_id=project_id,
            project_name=project_name,
            repository=repository,
            pulls_url=pulls_url,
            open_pr_count=open_pr_count,
        )

        pull_request_project_summary.additional_properties = d
        return pull_request_project_summary

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
