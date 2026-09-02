from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="StallMetrics")


@_attrs_define
class StallMetrics:
    """Stall-ladder activity in the trailing hour.

    Sourced from bus events the reconciler does not persist, so both counters
    restart with the daemon — read them next to ``daemon.uptime_seconds``.

        Attributes:
            nudges_per_hour (float | Unset):  Default: 0.0.
            kills_per_hour (float | Unset):  Default: 0.0.
    """

    nudges_per_hour: float | Unset = 0.0
    kills_per_hour: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        nudges_per_hour = self.nudges_per_hour

        kills_per_hour = self.kills_per_hour

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if nudges_per_hour is not UNSET:
            field_dict["nudges_per_hour"] = nudges_per_hour
        if kills_per_hour is not UNSET:
            field_dict["kills_per_hour"] = kills_per_hour

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        nudges_per_hour = d.pop("nudges_per_hour", UNSET)

        kills_per_hour = d.pop("kills_per_hour", UNSET)

        stall_metrics = cls(
            nudges_per_hour=nudges_per_hour,
            kills_per_hour=kills_per_hour,
        )

        stall_metrics.additional_properties = d
        return stall_metrics

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
