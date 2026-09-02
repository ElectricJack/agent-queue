from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MachineMetrics")


@_attrs_define
class MachineMetrics:
    """Nulls mean the platform does not expose the value, not zero.

    Attributes:
        load1 (float | None | Unset):
        load5 (float | None | Unset):
        load15 (float | None | Unset):
        cpu_count (float | None | Unset):
        mem_total_mb (float | None | Unset):
        mem_free_mb (float | None | Unset):
        mem_available_mb (float | None | Unset):
    """

    load1: float | None | Unset = UNSET
    load5: float | None | Unset = UNSET
    load15: float | None | Unset = UNSET
    cpu_count: float | None | Unset = UNSET
    mem_total_mb: float | None | Unset = UNSET
    mem_free_mb: float | None | Unset = UNSET
    mem_available_mb: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        load1: float | None | Unset
        if isinstance(self.load1, Unset):
            load1 = UNSET
        else:
            load1 = self.load1

        load5: float | None | Unset
        if isinstance(self.load5, Unset):
            load5 = UNSET
        else:
            load5 = self.load5

        load15: float | None | Unset
        if isinstance(self.load15, Unset):
            load15 = UNSET
        else:
            load15 = self.load15

        cpu_count: float | None | Unset
        if isinstance(self.cpu_count, Unset):
            cpu_count = UNSET
        else:
            cpu_count = self.cpu_count

        mem_total_mb: float | None | Unset
        if isinstance(self.mem_total_mb, Unset):
            mem_total_mb = UNSET
        else:
            mem_total_mb = self.mem_total_mb

        mem_free_mb: float | None | Unset
        if isinstance(self.mem_free_mb, Unset):
            mem_free_mb = UNSET
        else:
            mem_free_mb = self.mem_free_mb

        mem_available_mb: float | None | Unset
        if isinstance(self.mem_available_mb, Unset):
            mem_available_mb = UNSET
        else:
            mem_available_mb = self.mem_available_mb

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if load1 is not UNSET:
            field_dict["load1"] = load1
        if load5 is not UNSET:
            field_dict["load5"] = load5
        if load15 is not UNSET:
            field_dict["load15"] = load15
        if cpu_count is not UNSET:
            field_dict["cpu_count"] = cpu_count
        if mem_total_mb is not UNSET:
            field_dict["mem_total_mb"] = mem_total_mb
        if mem_free_mb is not UNSET:
            field_dict["mem_free_mb"] = mem_free_mb
        if mem_available_mb is not UNSET:
            field_dict["mem_available_mb"] = mem_available_mb

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_load1(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        load1 = _parse_load1(d.pop("load1", UNSET))

        def _parse_load5(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        load5 = _parse_load5(d.pop("load5", UNSET))

        def _parse_load15(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        load15 = _parse_load15(d.pop("load15", UNSET))

        def _parse_cpu_count(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        cpu_count = _parse_cpu_count(d.pop("cpu_count", UNSET))

        def _parse_mem_total_mb(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        mem_total_mb = _parse_mem_total_mb(d.pop("mem_total_mb", UNSET))

        def _parse_mem_free_mb(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        mem_free_mb = _parse_mem_free_mb(d.pop("mem_free_mb", UNSET))

        def _parse_mem_available_mb(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        mem_available_mb = _parse_mem_available_mb(d.pop("mem_available_mb", UNSET))

        machine_metrics = cls(
            load1=load1,
            load5=load5,
            load15=load15,
            cpu_count=cpu_count,
            mem_total_mb=mem_total_mb,
            mem_free_mb=mem_free_mb,
            mem_available_mb=mem_available_mb,
        )

        machine_metrics.additional_properties = d
        return machine_metrics

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
