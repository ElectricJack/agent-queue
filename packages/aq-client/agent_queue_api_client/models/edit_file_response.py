from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EditFileResponse")


@_attrs_define
class EditFileResponse:
    """
    Attributes:
        path (str | Unset):  Default: ''.
        replacements (int | Unset):  Default: 0.
    """

    path: str | Unset = ""
    replacements: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        path = self.path

        replacements = self.replacements

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if path is not UNSET:
            field_dict["path"] = path
        if replacements is not UNSET:
            field_dict["replacements"] = replacements

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        path = d.pop("path", UNSET)

        replacements = d.pop("replacements", UNSET)

        edit_file_response = cls(
            path=path,
            replacements=replacements,
        )

        edit_file_response.additional_properties = d
        return edit_file_response

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
