from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CountProjectMemoryFilesRequest")


@_attrs_define
class CountProjectMemoryFilesRequest:
    """
    Attributes:
        project_id (str): Project ID. Must contain only alphanumerics, hyphens, and underscores — no path separators or
            traversal segments.
        path (str): Relative subdirectory under the project's memory directory (e.g. ``insights`` or ``knowledge``).
        newer_than (None | str | Unset): Optional ISO 8601 timestamp. If provided, only files whose mtime is strictly
            newer than this time are counted. Omit or pass null to count all files.
    """

    project_id: str
    path: str
    newer_than: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        path = self.path

        newer_than: None | str | Unset
        if isinstance(self.newer_than, Unset):
            newer_than = UNSET
        else:
            newer_than = self.newer_than

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "path": path,
            }
        )
        if newer_than is not UNSET:
            field_dict["newer_than"] = newer_than

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        path = d.pop("path")

        def _parse_newer_than(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        newer_than = _parse_newer_than(d.pop("newer_than", UNSET))

        count_project_memory_files_request = cls(
            project_id=project_id,
            path=path,
            newer_than=newer_than,
        )

        count_project_memory_files_request.additional_properties = d
        return count_project_memory_files_request

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
