from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SamplerMetrics")


@_attrs_define
class SamplerMetrics:
    """The sampler's own per-tick cost, so its overhead is observable.

    Attributes:
        collect_ms (float | Unset):  Default: 0.0.
    """

    collect_ms: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        collect_ms = self.collect_ms

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if collect_ms is not UNSET:
            field_dict["collect_ms"] = collect_ms

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        collect_ms = d.pop("collect_ms", UNSET)

        sampler_metrics = cls(
            collect_ms=collect_ms,
        )

        sampler_metrics.additional_properties = d
        return sampler_metrics

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
