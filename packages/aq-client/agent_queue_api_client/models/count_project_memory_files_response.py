from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CountProjectMemoryFilesResponse")


@_attrs_define
class CountProjectMemoryFilesResponse:
    """
    Attributes:
        project_id (str):
        path (str):
        count (int | Unset):  Default: 0.
        total (int | Unset):  Default: 0.
        missing (bool | None | Unset):
        newer_than (None | str | Unset):
    """

    project_id: str
    path: str
    count: int | Unset = 0
    total: int | Unset = 0
    missing: bool | None | Unset = UNSET
    newer_than: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        path = self.path

        count = self.count

        total = self.total

        missing: bool | None | Unset
        if isinstance(self.missing, Unset):
            missing = UNSET
        else:
            missing = self.missing

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
        if count is not UNSET:
            field_dict["count"] = count
        if total is not UNSET:
            field_dict["total"] = total
        if missing is not UNSET:
            field_dict["missing"] = missing
        if newer_than is not UNSET:
            field_dict["newer_than"] = newer_than

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        path = d.pop("path")

        count = d.pop("count", UNSET)

        total = d.pop("total", UNSET)

        def _parse_missing(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        missing = _parse_missing(d.pop("missing", UNSET))

        def _parse_newer_than(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        newer_than = _parse_newer_than(d.pop("newer_than", UNSET))

        count_project_memory_files_response = cls(
            project_id=project_id,
            path=path,
            count=count,
            total=total,
            missing=missing,
            newer_than=newer_than,
        )

        count_project_memory_files_response.additional_properties = d
        return count_project_memory_files_response

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
