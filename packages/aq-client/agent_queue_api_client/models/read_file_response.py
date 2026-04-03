from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReadFileResponse")


@_attrs_define
class ReadFileResponse:
    """
    Attributes:
        content (str | Unset):  Default: ''.
        path (str | Unset):  Default: ''.
        offset (int | None | Unset):
        truncated (bool | None | Unset):
        total_lines (int | None | Unset):
        lines_returned (int | None | Unset):
    """

    content: str | Unset = ""
    path: str | Unset = ""
    offset: int | None | Unset = UNSET
    truncated: bool | None | Unset = UNSET
    total_lines: int | None | Unset = UNSET
    lines_returned: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        content = self.content

        path = self.path

        offset: int | None | Unset
        if isinstance(self.offset, Unset):
            offset = UNSET
        else:
            offset = self.offset

        truncated: bool | None | Unset
        if isinstance(self.truncated, Unset):
            truncated = UNSET
        else:
            truncated = self.truncated

        total_lines: int | None | Unset
        if isinstance(self.total_lines, Unset):
            total_lines = UNSET
        else:
            total_lines = self.total_lines

        lines_returned: int | None | Unset
        if isinstance(self.lines_returned, Unset):
            lines_returned = UNSET
        else:
            lines_returned = self.lines_returned

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if content is not UNSET:
            field_dict["content"] = content
        if path is not UNSET:
            field_dict["path"] = path
        if offset is not UNSET:
            field_dict["offset"] = offset
        if truncated is not UNSET:
            field_dict["truncated"] = truncated
        if total_lines is not UNSET:
            field_dict["total_lines"] = total_lines
        if lines_returned is not UNSET:
            field_dict["lines_returned"] = lines_returned

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        content = d.pop("content", UNSET)

        path = d.pop("path", UNSET)

        def _parse_offset(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        offset = _parse_offset(d.pop("offset", UNSET))

        def _parse_truncated(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        truncated = _parse_truncated(d.pop("truncated", UNSET))

        def _parse_total_lines(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total_lines = _parse_total_lines(d.pop("total_lines", UNSET))

        def _parse_lines_returned(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        lines_returned = _parse_lines_returned(d.pop("lines_returned", UNSET))

        read_file_response = cls(
            content=content,
            path=path,
            offset=offset,
            truncated=truncated,
            total_lines=total_lines,
            lines_returned=lines_returned,
        )

        read_file_response.additional_properties = d
        return read_file_response

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
