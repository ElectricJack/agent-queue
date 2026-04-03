from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="NoteSummary")


@_attrs_define
class NoteSummary:
    """
    Attributes:
        name (str | Unset):  Default: ''.
        title (str | Unset):  Default: ''.
        size_bytes (int | Unset):  Default: 0.
        modified (None | str | Unset):
        path (None | str | Unset):
    """

    name: str | Unset = ""
    title: str | Unset = ""
    size_bytes: int | Unset = 0
    modified: None | str | Unset = UNSET
    path: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        title = self.title

        size_bytes = self.size_bytes

        modified: None | str | Unset
        if isinstance(self.modified, Unset):
            modified = UNSET
        else:
            modified = self.modified

        path: None | str | Unset
        if isinstance(self.path, Unset):
            path = UNSET
        else:
            path = self.path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if name is not UNSET:
            field_dict["name"] = name
        if title is not UNSET:
            field_dict["title"] = title
        if size_bytes is not UNSET:
            field_dict["size_bytes"] = size_bytes
        if modified is not UNSET:
            field_dict["modified"] = modified
        if path is not UNSET:
            field_dict["path"] = path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name", UNSET)

        title = d.pop("title", UNSET)

        size_bytes = d.pop("size_bytes", UNSET)

        def _parse_modified(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        modified = _parse_modified(d.pop("modified", UNSET))

        def _parse_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        path = _parse_path(d.pop("path", UNSET))

        note_summary = cls(
            name=name,
            title=title,
            size_bytes=size_bytes,
            modified=modified,
            path=path,
        )

        note_summary.additional_properties = d
        return note_summary

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
