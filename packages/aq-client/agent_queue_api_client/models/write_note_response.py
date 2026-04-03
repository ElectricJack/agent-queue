from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WriteNoteResponse")


@_attrs_define
class WriteNoteResponse:
    """
    Attributes:
        path (str | Unset):  Default: ''.
        title (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
    """

    path: str | Unset = ""
    title: str | Unset = ""
    status: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        path = self.path

        title = self.title

        status = self.status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if path is not UNSET:
            field_dict["path"] = path
        if title is not UNSET:
            field_dict["title"] = title
        if status is not UNSET:
            field_dict["status"] = status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        path = d.pop("path", UNSET)

        title = d.pop("title", UNSET)

        status = d.pop("status", UNSET)

        write_note_response = cls(
            path=path,
            title=title,
            status=status,
        )

        write_note_response.additional_properties = d
        return write_note_response

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
