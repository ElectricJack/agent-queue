from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PoolGauges")


@_attrs_define
class PoolGauges:
    """Local engine pool readings from metrics_pool_gauges, without a query.

    Attributes:
        checked_out (float | None | Unset):
        overflow (float | None | Unset):
        size (float | None | Unset):
    """

    checked_out: float | None | Unset = UNSET
    overflow: float | None | Unset = UNSET
    size: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        checked_out: float | None | Unset
        if isinstance(self.checked_out, Unset):
            checked_out = UNSET
        else:
            checked_out = self.checked_out

        overflow: float | None | Unset
        if isinstance(self.overflow, Unset):
            overflow = UNSET
        else:
            overflow = self.overflow

        size: float | None | Unset
        if isinstance(self.size, Unset):
            size = UNSET
        else:
            size = self.size

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if checked_out is not UNSET:
            field_dict["checked_out"] = checked_out
        if overflow is not UNSET:
            field_dict["overflow"] = overflow
        if size is not UNSET:
            field_dict["size"] = size

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_checked_out(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        checked_out = _parse_checked_out(d.pop("checked_out", UNSET))

        def _parse_overflow(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        overflow = _parse_overflow(d.pop("overflow", UNSET))

        def _parse_size(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        size = _parse_size(d.pop("size", UNSET))

        pool_gauges = cls(
            checked_out=checked_out,
            overflow=overflow,
            size=size,
        )

        pool_gauges.additional_properties = d
        return pool_gauges

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
