from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EditFileRequest")


@_attrs_define
class EditFileRequest:
    """
    Attributes:
        path (str): File path (absolute or relative to workspaces root)
        old_string (str): Exact text to find and replace
        new_string (str): Replacement text
        replace_all (bool | Unset): Replace all occurrences (default false — requires unique match) Default: False.
    """

    path: str
    old_string: str
    new_string: str
    replace_all: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        path = self.path

        old_string = self.old_string

        new_string = self.new_string

        replace_all = self.replace_all

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "path": path,
                "old_string": old_string,
                "new_string": new_string,
            }
        )
        if replace_all is not UNSET:
            field_dict["replace_all"] = replace_all

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        path = d.pop("path")

        old_string = d.pop("old_string")

        new_string = d.pop("new_string")

        replace_all = d.pop("replace_all", UNSET)

        edit_file_request = cls(
            path=path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
        )

        edit_file_request.additional_properties = d
        return edit_file_request

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
