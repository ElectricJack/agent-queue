from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AppendNoteResponse")


@_attrs_define
class AppendNoteResponse:
    """
    Attributes:
        path (str | Unset):  Default: ''.
        title (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        size_bytes (int | Unset):  Default: 0.
    """

    path: str | Unset = ""
    title: str | Unset = ""
    status: str | Unset = ""
    size_bytes: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        path = self.path

        title = self.title

        status = self.status

        size_bytes = self.size_bytes

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if path is not UNSET:
            field_dict["path"] = path
        if title is not UNSET:
            field_dict["title"] = title
        if status is not UNSET:
            field_dict["status"] = status
        if size_bytes is not UNSET:
            field_dict["size_bytes"] = size_bytes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        path = d.pop("path", UNSET)

        title = d.pop("title", UNSET)

        status = d.pop("status", UNSET)

        size_bytes = d.pop("size_bytes", UNSET)

        append_note_response = cls(
            path=path,
            title=title,
            status=status,
            size_bytes=size_bytes,
        )

        append_note_response.additional_properties = d
        return append_note_response

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
