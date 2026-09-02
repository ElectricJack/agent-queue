from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ThroughputMetrics")


@_attrs_define
class ThroughputMetrics:
    """
    Attributes:
        completions_per_hour (float | Unset):  Default: 0.0.
        prs_per_hour (float | Unset):  Default: 0.0.
    """

    completions_per_hour: float | Unset = 0.0
    prs_per_hour: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        completions_per_hour = self.completions_per_hour

        prs_per_hour = self.prs_per_hour

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if completions_per_hour is not UNSET:
            field_dict["completions_per_hour"] = completions_per_hour
        if prs_per_hour is not UNSET:
            field_dict["prs_per_hour"] = prs_per_hour

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        completions_per_hour = d.pop("completions_per_hour", UNSET)

        prs_per_hour = d.pop("prs_per_hour", UNSET)

        throughput_metrics = cls(
            completions_per_hour=completions_per_hour,
            prs_per_hour=prs_per_hour,
        )

        throughput_metrics.additional_properties = d
        return throughput_metrics

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
