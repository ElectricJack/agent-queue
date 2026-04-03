from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReadFileRequest")


@_attrs_define
class ReadFileRequest:
    """
    Attributes:
        path (str): File path (absolute or relative to workspaces root)
        max_lines (int | Unset): Max lines to return (default 2000) Default: 2000.
        offset (int | Unset): Line number to start reading from (1-based, default 1) Default: 1.
        limit (int | None | Unset): Number of lines to read. If set, overrides max_lines.
    """

    path: str
    max_lines: int | Unset = 2000
    offset: int | Unset = 1
    limit: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        path = self.path

        max_lines = self.max_lines

        offset = self.offset

        limit: int | None | Unset
        if isinstance(self.limit, Unset):
            limit = UNSET
        else:
            limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "path": path,
            }
        )
        if max_lines is not UNSET:
            field_dict["max_lines"] = max_lines
        if offset is not UNSET:
            field_dict["offset"] = offset
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        path = d.pop("path")

        max_lines = d.pop("max_lines", UNSET)

        offset = d.pop("offset", UNSET)

        def _parse_limit(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        limit = _parse_limit(d.pop("limit", UNSET))

        read_file_request = cls(
            path=path,
            max_lines=max_lines,
            offset=offset,
            limit=limit,
        )

        read_file_request.additional_properties = d
        return read_file_request

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
