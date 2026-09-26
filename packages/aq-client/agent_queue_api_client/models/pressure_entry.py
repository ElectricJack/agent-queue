from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PressureEntry")


@_attrs_define
class PressureEntry:
    """One kernel PSI resource reading from the host reader.

    Attributes:
        some_avg10 (float | None | Unset):
        full_avg10 (float | None | Unset):
    """

    some_avg10: float | None | Unset = UNSET
    full_avg10: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        some_avg10: float | None | Unset
        if isinstance(self.some_avg10, Unset):
            some_avg10 = UNSET
        else:
            some_avg10 = self.some_avg10

        full_avg10: float | None | Unset
        if isinstance(self.full_avg10, Unset):
            full_avg10 = UNSET
        else:
            full_avg10 = self.full_avg10

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if some_avg10 is not UNSET:
            field_dict["some_avg10"] = some_avg10
        if full_avg10 is not UNSET:
            field_dict["full_avg10"] = full_avg10

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_some_avg10(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        some_avg10 = _parse_some_avg10(d.pop("some_avg10", UNSET))

        def _parse_full_avg10(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        full_avg10 = _parse_full_avg10(d.pop("full_avg10", UNSET))

        pressure_entry = cls(
            some_avg10=some_avg10,
            full_avg10=full_avg10,
        )

        pressure_entry.additional_properties = d
        return pressure_entry

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
