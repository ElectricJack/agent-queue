from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PerfSamplerCost")


@_attrs_define
class PerfSamplerCost:
    """Instrumentation-only cost measured by MetricsSampler.

    Attributes:
        perf_ms (float | None | Unset):
    """

    perf_ms: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        perf_ms: float | None | Unset
        if isinstance(self.perf_ms, Unset):
            perf_ms = UNSET
        else:
            perf_ms = self.perf_ms

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if perf_ms is not UNSET:
            field_dict["perf_ms"] = perf_ms

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_perf_ms(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        perf_ms = _parse_perf_ms(d.pop("perf_ms", UNSET))

        perf_sampler_cost = cls(
            perf_ms=perf_ms,
        )

        perf_sampler_cost.additional_properties = d
        return perf_sampler_cost

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
