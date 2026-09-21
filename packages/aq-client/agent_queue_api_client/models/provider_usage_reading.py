from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderUsageReading")


@_attrs_define
class ProviderUsageReading:
    """The newest fresh account-wide usage window (the fullest one).

    Attributes:
        window (str):
        used_percent (float):
        observed_at (float):
        scope (str | Unset):  Default: ''.
        resets_at (float | None | Unset):
    """

    window: str
    used_percent: float
    observed_at: float
    scope: str | Unset = ""
    resets_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        window = self.window

        used_percent = self.used_percent

        observed_at = self.observed_at

        scope = self.scope

        resets_at: float | None | Unset
        if isinstance(self.resets_at, Unset):
            resets_at = UNSET
        else:
            resets_at = self.resets_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "window": window,
                "used_percent": used_percent,
                "observed_at": observed_at,
            }
        )
        if scope is not UNSET:
            field_dict["scope"] = scope
        if resets_at is not UNSET:
            field_dict["resets_at"] = resets_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        window = d.pop("window")

        used_percent = d.pop("used_percent")

        observed_at = d.pop("observed_at")

        scope = d.pop("scope", UNSET)

        def _parse_resets_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        resets_at = _parse_resets_at(d.pop("resets_at", UNSET))

        provider_usage_reading = cls(
            window=window,
            used_percent=used_percent,
            observed_at=observed_at,
            scope=scope,
            resets_at=resets_at,
        )

        provider_usage_reading.additional_properties = d
        return provider_usage_reading

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
