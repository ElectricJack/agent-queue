from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GlobFilesResponse")


@_attrs_define
class GlobFilesResponse:
    """
    Attributes:
        matches (list[str] | Unset):
        count (int | Unset):  Default: 0.
        truncated (bool | None | Unset):
        total (int | None | Unset):
    """

    matches: list[str] | Unset = UNSET
    count: int | Unset = 0
    truncated: bool | None | Unset = UNSET
    total: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        matches: list[str] | Unset = UNSET
        if not isinstance(self.matches, Unset):
            matches = self.matches

        count = self.count

        truncated: bool | None | Unset
        if isinstance(self.truncated, Unset):
            truncated = UNSET
        else:
            truncated = self.truncated

        total: int | None | Unset
        if isinstance(self.total, Unset):
            total = UNSET
        else:
            total = self.total

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if matches is not UNSET:
            field_dict["matches"] = matches
        if count is not UNSET:
            field_dict["count"] = count
        if truncated is not UNSET:
            field_dict["truncated"] = truncated
        if total is not UNSET:
            field_dict["total"] = total

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        matches = cast(list[str], d.pop("matches", UNSET))

        count = d.pop("count", UNSET)

        def _parse_truncated(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        truncated = _parse_truncated(d.pop("truncated", UNSET))

        def _parse_total(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total = _parse_total(d.pop("total", UNSET))

        glob_files_response = cls(
            matches=matches,
            count=count,
            truncated=truncated,
            total=total,
        )

        glob_files_response.additional_properties = d
        return glob_files_response

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
