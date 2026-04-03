from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GitDiffResponse")


@_attrs_define
class GitDiffResponse:
    """
    Attributes:
        project_id (str):
        base_branch (str | Unset):  Default: ''.
        diff (str | Unset):  Default: ''.
    """

    project_id: str
    base_branch: str | Unset = ""
    diff: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        base_branch = self.base_branch

        diff = self.diff

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if base_branch is not UNSET:
            field_dict["base_branch"] = base_branch
        if diff is not UNSET:
            field_dict["diff"] = diff

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        base_branch = d.pop("base_branch", UNSET)

        diff = d.pop("diff", UNSET)

        git_diff_response = cls(
            project_id=project_id,
            base_branch=base_branch,
            diff=diff,
        )

        git_diff_response.additional_properties = d
        return git_diff_response

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
