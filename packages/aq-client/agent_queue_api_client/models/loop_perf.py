from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.histogram import Histogram


T = TypeVar("T", bound="LoopPerf")


@_attrs_define
class LoopPerf:
    """Event-loop drift observed by the registry's LoopLagProbe.

    Attributes:
        drift (Histogram | None | Unset):
        probe_interval_ms (float | None | Unset):
    """

    drift: Histogram | None | Unset = UNSET
    probe_interval_ms: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.histogram import Histogram

        drift: dict[str, Any] | None | Unset
        if isinstance(self.drift, Unset):
            drift = UNSET
        elif isinstance(self.drift, Histogram):
            drift = self.drift.to_dict()
        else:
            drift = self.drift

        probe_interval_ms: float | None | Unset
        if isinstance(self.probe_interval_ms, Unset):
            probe_interval_ms = UNSET
        else:
            probe_interval_ms = self.probe_interval_ms

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if drift is not UNSET:
            field_dict["drift"] = drift
        if probe_interval_ms is not UNSET:
            field_dict["probe_interval_ms"] = probe_interval_ms

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.histogram import Histogram

        d = dict(src_dict)

        def _parse_drift(data: object) -> Histogram | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                drift_type_0 = Histogram.from_dict(data)

                return drift_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(Histogram | None | Unset, data)

        drift = _parse_drift(d.pop("drift", UNSET))

        def _parse_probe_interval_ms(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        probe_interval_ms = _parse_probe_interval_ms(d.pop("probe_interval_ms", UNSET))

        loop_perf = cls(
            drift=drift,
            probe_interval_ms=probe_interval_ms,
        )

        loop_perf.additional_properties = d
        return loop_perf

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
