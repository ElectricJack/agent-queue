from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.histogram import Histogram
    from ..models.operation_perf_outcomes import OperationPerfOutcomes


T = TypeVar("T", bound="OperationPerf")


@_attrs_define
class OperationPerf:
    """Bounded operation timing; coarser tiers average concurrency gauges.

    Attributes:
        latency (Histogram | None | Unset):
        outcomes (OperationPerfOutcomes | Unset):
        in_flight (float | Unset):  Default: 0.0.
        peak_in_flight (float | Unset):  Default: 0.0.
    """

    latency: Histogram | None | Unset = UNSET
    outcomes: OperationPerfOutcomes | Unset = UNSET
    in_flight: float | Unset = 0.0
    peak_in_flight: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.histogram import Histogram

        latency: dict[str, Any] | None | Unset
        if isinstance(self.latency, Unset):
            latency = UNSET
        elif isinstance(self.latency, Histogram):
            latency = self.latency.to_dict()
        else:
            latency = self.latency

        outcomes: dict[str, Any] | Unset = UNSET
        if not isinstance(self.outcomes, Unset):
            outcomes = self.outcomes.to_dict()

        in_flight = self.in_flight

        peak_in_flight = self.peak_in_flight

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if latency is not UNSET:
            field_dict["latency"] = latency
        if outcomes is not UNSET:
            field_dict["outcomes"] = outcomes
        if in_flight is not UNSET:
            field_dict["in_flight"] = in_flight
        if peak_in_flight is not UNSET:
            field_dict["peak_in_flight"] = peak_in_flight

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.histogram import Histogram
        from ..models.operation_perf_outcomes import OperationPerfOutcomes

        d = dict(src_dict)

        def _parse_latency(data: object) -> Histogram | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                latency_type_0 = Histogram.from_dict(data)

                return latency_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(Histogram | None | Unset, data)

        latency = _parse_latency(d.pop("latency", UNSET))

        _outcomes = d.pop("outcomes", UNSET)
        outcomes: OperationPerfOutcomes | Unset
        if isinstance(_outcomes, Unset):
            outcomes = UNSET
        else:
            outcomes = OperationPerfOutcomes.from_dict(_outcomes)

        in_flight = d.pop("in_flight", UNSET)

        peak_in_flight = d.pop("peak_in_flight", UNSET)

        operation_perf = cls(
            latency=latency,
            outcomes=outcomes,
            in_flight=in_flight,
            peak_in_flight=peak_in_flight,
        )

        operation_perf.additional_properties = d
        return operation_perf

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
