from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationCeiling")


@_attrs_define
class ProviderAllocationCeiling:
    """The provider-wide configured ceiling over its enabled pool profiles.

    ``max_active`` is ``None`` (and ``unbounded`` true) when any of them is
    unbounded.  Bounds stay per profile; this total is for reading only.

        Attributes:
            min_active (int | Unset):  Default: 0.
            max_active (int | None | Unset):  Default: 0.
            unbounded (bool | Unset):  Default: False.
            pool_profiles (int | Unset):  Default: 0.
    """

    min_active: int | Unset = 0
    max_active: int | None | Unset = 0
    unbounded: bool | Unset = False
    pool_profiles: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        min_active = self.min_active

        max_active: int | None | Unset
        if isinstance(self.max_active, Unset):
            max_active = UNSET
        else:
            max_active = self.max_active

        unbounded = self.unbounded

        pool_profiles = self.pool_profiles

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if min_active is not UNSET:
            field_dict["min_active"] = min_active
        if max_active is not UNSET:
            field_dict["max_active"] = max_active
        if unbounded is not UNSET:
            field_dict["unbounded"] = unbounded
        if pool_profiles is not UNSET:
            field_dict["pool_profiles"] = pool_profiles

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        min_active = d.pop("min_active", UNSET)

        def _parse_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_active = _parse_max_active(d.pop("max_active", UNSET))

        unbounded = d.pop("unbounded", UNSET)

        pool_profiles = d.pop("pool_profiles", UNSET)

        provider_allocation_ceiling = cls(
            min_active=min_active,
            max_active=max_active,
            unbounded=unbounded,
            pool_profiles=pool_profiles,
        )

        provider_allocation_ceiling.additional_properties = d
        return provider_allocation_ceiling

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
