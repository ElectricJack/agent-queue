from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeliveryLatencySegment")


@_attrs_define
class DeliveryLatencySegment:
    """Median of one segment over the cohort's samples; segments overlap.

    Attributes:
        segment (str):
        median_seconds (float | None | Unset):
        generations (int | Unset):  Default: 0.
    """

    segment: str
    median_seconds: float | None | Unset = UNSET
    generations: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        segment = self.segment

        median_seconds: float | None | Unset
        if isinstance(self.median_seconds, Unset):
            median_seconds = UNSET
        else:
            median_seconds = self.median_seconds

        generations = self.generations

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "segment": segment,
            }
        )
        if median_seconds is not UNSET:
            field_dict["median_seconds"] = median_seconds
        if generations is not UNSET:
            field_dict["generations"] = generations

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        segment = d.pop("segment")

        def _parse_median_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        median_seconds = _parse_median_seconds(d.pop("median_seconds", UNSET))

        generations = d.pop("generations", UNSET)

        delivery_latency_segment = cls(
            segment=segment,
            median_seconds=median_seconds,
            generations=generations,
        )

        delivery_latency_segment.additional_properties = d
        return delivery_latency_segment

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
