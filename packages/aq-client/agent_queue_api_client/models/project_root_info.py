from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProjectRootInfo")


@_attrs_define
class ProjectRootInfo:
    """
    Attributes:
        id (str):
        label (str):
        path (str):
        readable (bool | Unset):  Default: True.
        writable (bool | Unset):  Default: True.
    """

    id: str
    label: str
    path: str
    readable: bool | Unset = True
    writable: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        label = self.label

        path = self.path

        readable = self.readable

        writable = self.writable

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "label": label,
                "path": path,
            }
        )
        if readable is not UNSET:
            field_dict["readable"] = readable
        if writable is not UNSET:
            field_dict["writable"] = writable

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        label = d.pop("label")

        path = d.pop("path")

        readable = d.pop("readable", UNSET)

        writable = d.pop("writable", UNSET)

        project_root_info = cls(
            id=id,
            label=label,
            path=path,
            readable=readable,
            writable=writable,
        )

        project_root_info.additional_properties = d
        return project_root_info

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
