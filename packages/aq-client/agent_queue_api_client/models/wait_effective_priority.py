from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitEffectivePriority")


@_attrs_define
class WaitEffectivePriority:
    """
    Attributes:
        value (int | None | Unset):
        base (int | None | Unset):
        donor (None | str | Unset):
        donor_path (list[str] | Unset):
        graph_version (int | None | Unset):
        computed_at (float | None | Unset):
    """

    value: int | None | Unset = UNSET
    base: int | None | Unset = UNSET
    donor: None | str | Unset = UNSET
    donor_path: list[str] | Unset = UNSET
    graph_version: int | None | Unset = UNSET
    computed_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value: int | None | Unset
        if isinstance(self.value, Unset):
            value = UNSET
        else:
            value = self.value

        base: int | None | Unset
        if isinstance(self.base, Unset):
            base = UNSET
        else:
            base = self.base

        donor: None | str | Unset
        if isinstance(self.donor, Unset):
            donor = UNSET
        else:
            donor = self.donor

        donor_path: list[str] | Unset = UNSET
        if not isinstance(self.donor_path, Unset):
            donor_path = self.donor_path

        graph_version: int | None | Unset
        if isinstance(self.graph_version, Unset):
            graph_version = UNSET
        else:
            graph_version = self.graph_version

        computed_at: float | None | Unset
        if isinstance(self.computed_at, Unset):
            computed_at = UNSET
        else:
            computed_at = self.computed_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if value is not UNSET:
            field_dict["value"] = value
        if base is not UNSET:
            field_dict["base"] = base
        if donor is not UNSET:
            field_dict["donor"] = donor
        if donor_path is not UNSET:
            field_dict["donor_path"] = donor_path
        if graph_version is not UNSET:
            field_dict["graph_version"] = graph_version
        if computed_at is not UNSET:
            field_dict["computed_at"] = computed_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_value(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        value = _parse_value(d.pop("value", UNSET))

        def _parse_base(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        base = _parse_base(d.pop("base", UNSET))

        def _parse_donor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        donor = _parse_donor(d.pop("donor", UNSET))

        donor_path = cast(list[str], d.pop("donor_path", UNSET))

        def _parse_graph_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        graph_version = _parse_graph_version(d.pop("graph_version", UNSET))

        def _parse_computed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        computed_at = _parse_computed_at(d.pop("computed_at", UNSET))

        wait_effective_priority = cls(
            value=value,
            base=base,
            donor=donor,
            donor_path=donor_path,
            graph_version=graph_version,
            computed_at=computed_at,
        )

        wait_effective_priority.additional_properties = d
        return wait_effective_priority

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
