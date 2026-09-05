from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="BrowseProjectRootRequest")


@_attrs_define
class BrowseProjectRootRequest:
    """
    Attributes:
        root_id (str): Configured project root id
        relative_path (str | Unset): Root-relative directory to list (default: the root itself) Default: ''.
    """

    root_id: str
    relative_path: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        root_id = self.root_id

        relative_path = self.relative_path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "root_id": root_id,
            }
        )
        if relative_path is not UNSET:
            field_dict["relative_path"] = relative_path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        root_id = d.pop("root_id")

        relative_path = d.pop("relative_path", UNSET)

        browse_project_root_request = cls(
            root_id=root_id,
            relative_path=relative_path,
        )

        browse_project_root_request.additional_properties = d
        return browse_project_root_request

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
