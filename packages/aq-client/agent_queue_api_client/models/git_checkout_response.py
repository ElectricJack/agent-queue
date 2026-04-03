from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GitCheckoutResponse")


@_attrs_define
class GitCheckoutResponse:
    """
    Attributes:
        project_id (str):
        old_branch (str | Unset):  Default: ''.
        new_branch (str | Unset):  Default: ''.
        message (str | Unset):  Default: ''.
    """

    project_id: str
    old_branch: str | Unset = ""
    new_branch: str | Unset = ""
    message: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        old_branch = self.old_branch

        new_branch = self.new_branch

        message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if old_branch is not UNSET:
            field_dict["old_branch"] = old_branch
        if new_branch is not UNSET:
            field_dict["new_branch"] = new_branch
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        old_branch = d.pop("old_branch", UNSET)

        new_branch = d.pop("new_branch", UNSET)

        message = d.pop("message", UNSET)

        git_checkout_response = cls(
            project_id=project_id,
            old_branch=old_branch,
            new_branch=new_branch,
            message=message,
        )

        git_checkout_response.additional_properties = d
        return git_checkout_response

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
