from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DaemonMetrics")


@_attrs_define
class DaemonMetrics:
    """
    Attributes:
        uptime_seconds (float | Unset):  Default: 0.0.
        restarts (float | Unset):  Default: 0.0.
    """

    uptime_seconds: float | Unset = 0.0
    restarts: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        uptime_seconds = self.uptime_seconds

        restarts = self.restarts

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if uptime_seconds is not UNSET:
            field_dict["uptime_seconds"] = uptime_seconds
        if restarts is not UNSET:
            field_dict["restarts"] = restarts

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        uptime_seconds = d.pop("uptime_seconds", UNSET)

        restarts = d.pop("restarts", UNSET)

        daemon_metrics = cls(
            uptime_seconds=uptime_seconds,
            restarts=restarts,
        )

        daemon_metrics.additional_properties = d
        return daemon_metrics

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
