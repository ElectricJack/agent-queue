from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationProfileState")


@_attrs_define
class ProviderAllocationProfileState:
    """The fields an allocation compares on one profile.

    Attributes:
        lifecycle (str):
        enabled (bool | Unset):  Default: True.
        min_active (int | None | Unset):
        max_active (int | None | Unset):
        min_per_project (int | None | Unset):
    """

    lifecycle: str
    enabled: bool | Unset = True
    min_active: int | None | Unset = UNSET
    max_active: int | None | Unset = UNSET
    min_per_project: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        lifecycle = self.lifecycle

        enabled = self.enabled

        min_active: int | None | Unset
        if isinstance(self.min_active, Unset):
            min_active = UNSET
        else:
            min_active = self.min_active

        max_active: int | None | Unset
        if isinstance(self.max_active, Unset):
            max_active = UNSET
        else:
            max_active = self.max_active

        min_per_project: int | None | Unset
        if isinstance(self.min_per_project, Unset):
            min_per_project = UNSET
        else:
            min_per_project = self.min_per_project

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "lifecycle": lifecycle,
            }
        )
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if min_active is not UNSET:
            field_dict["min_active"] = min_active
        if max_active is not UNSET:
            field_dict["max_active"] = max_active
        if min_per_project is not UNSET:
            field_dict["min_per_project"] = min_per_project

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        lifecycle = d.pop("lifecycle")

        enabled = d.pop("enabled", UNSET)

        def _parse_min_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        min_active = _parse_min_active(d.pop("min_active", UNSET))

        def _parse_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_active = _parse_max_active(d.pop("max_active", UNSET))

        def _parse_min_per_project(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        min_per_project = _parse_min_per_project(d.pop("min_per_project", UNSET))

        provider_allocation_profile_state = cls(
            lifecycle=lifecycle,
            enabled=enabled,
            min_active=min_active,
            max_active=max_active,
            min_per_project=min_per_project,
        )

        provider_allocation_profile_state.additional_properties = d
        return provider_allocation_profile_state

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
