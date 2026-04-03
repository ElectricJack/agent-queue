from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GitCreatePrResponse")


@_attrs_define
class GitCreatePrResponse:
    """
    Attributes:
        project_id (str):
        pr_url (str | Unset):  Default: ''.
        branch (str | Unset):  Default: ''.
        base (str | Unset):  Default: ''.
    """

    project_id: str
    pr_url: str | Unset = ""
    branch: str | Unset = ""
    base: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        pr_url = self.pr_url

        branch = self.branch

        base = self.base

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if pr_url is not UNSET:
            field_dict["pr_url"] = pr_url
        if branch is not UNSET:
            field_dict["branch"] = branch
        if base is not UNSET:
            field_dict["base"] = base

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        pr_url = d.pop("pr_url", UNSET)

        branch = d.pop("branch", UNSET)

        base = d.pop("base", UNSET)

        git_create_pr_response = cls(
            project_id=project_id,
            pr_url=pr_url,
            branch=branch,
            base=base,
        )

        git_create_pr_response.additional_properties = d
        return git_create_pr_response

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
