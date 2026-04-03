from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GitCreateBranchResponse")


@_attrs_define
class GitCreateBranchResponse:
    """
    Attributes:
        project_id (str):
        created_branch (str | Unset):  Default: ''.
    """

    project_id: str
    created_branch: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        created_branch = self.created_branch

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if created_branch is not UNSET:
            field_dict["created_branch"] = created_branch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        created_branch = d.pop("created_branch", UNSET)

        git_create_branch_response = cls(
            project_id=project_id,
            created_branch=created_branch,
        )

        git_create_branch_response.additional_properties = d
        return git_create_branch_response

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
