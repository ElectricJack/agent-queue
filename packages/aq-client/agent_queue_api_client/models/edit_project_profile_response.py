from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EditProjectProfileResponse")


@_attrs_define
class EditProjectProfileResponse:
    """
    Attributes:
        project_id (str):
        status (str | Unset):  Default: 'profile_updated'.
        path (str | Unset):  Default: ''.
    """

    project_id: str
    status: str | Unset = "profile_updated"
    path: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        status = self.status

        path = self.path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if path is not UNSET:
            field_dict["path"] = path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        status = d.pop("status", UNSET)

        path = d.pop("path", UNSET)

        edit_project_profile_response = cls(
            project_id=project_id,
            status=status,
            path=path,
        )

        edit_project_profile_response.additional_properties = d
        return edit_project_profile_response

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
