from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="BindProjectRepositoryRequest")


@_attrs_define
class BindProjectRepositoryRequest:
    """
    Attributes:
        project_id (str): Existing project ID
        repo_url (str): GitHub repository URL to authorize
        expected_repo_url (str): Exact current stored URL; empty for first binding
        reason (str): Nonempty operator audit reason
    """

    project_id: str
    repo_url: str
    expected_repo_url: str
    reason: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        repo_url = self.repo_url

        expected_repo_url = self.expected_repo_url

        reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "repo_url": repo_url,
                "expected_repo_url": expected_repo_url,
                "reason": reason,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        repo_url = d.pop("repo_url")

        expected_repo_url = d.pop("expected_repo_url")

        reason = d.pop("reason")

        bind_project_repository_request = cls(
            project_id=project_id,
            repo_url=repo_url,
            expected_repo_url=expected_repo_url,
            reason=reason,
        )

        bind_project_repository_request.additional_properties = d
        return bind_project_repository_request

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
