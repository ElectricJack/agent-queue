from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SearchFilesRequest")


@_attrs_define
class SearchFilesRequest:
    """
    Attributes:
        pattern (str): Search pattern (regex for grep, glob for find)
        path (str): Directory to search in (absolute or relative to workspaces root)
        mode (str | Unset): Search mode: 'grep' for content, 'find' for filenames Default: 'grep'.
    """

    pattern: str
    path: str
    mode: str | Unset = "grep"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        pattern = self.pattern

        path = self.path

        mode = self.mode

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "pattern": pattern,
                "path": path,
            }
        )
        if mode is not UNSET:
            field_dict["mode"] = mode

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        pattern = d.pop("pattern")

        path = d.pop("path")

        mode = d.pop("mode", UNSET)

        search_files_request = cls(
            pattern=pattern,
            path=path,
            mode=mode,
        )

        search_files_request.additional_properties = d
        return search_files_request

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
