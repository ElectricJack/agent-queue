from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.histogram import Histogram
    from ..models.route_perf_status import RoutePerfStatus


T = TypeVar("T", bound="RoutePerf")


@_attrs_define
class RoutePerf:
    """Latency and status-class counters from the route middleware.

    Attributes:
        latency (Histogram | None | Unset):
        status (RoutePerfStatus | Unset):
    """

    latency: Histogram | None | Unset = UNSET
    status: RoutePerfStatus | Unset = UNSET
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

        status: dict[str, Any] | Unset = UNSET
        if not isinstance(self.status, Unset):
            status = self.status.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if latency is not UNSET:
            field_dict["latency"] = latency
        if status is not UNSET:
            field_dict["status"] = status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.histogram import Histogram
        from ..models.route_perf_status import RoutePerfStatus

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

        _status = d.pop("status", UNSET)
        status: RoutePerfStatus | Unset
        if isinstance(_status, Unset):
            status = UNSET
        else:
            status = RoutePerfStatus.from_dict(_status)

        route_perf = cls(
            latency=latency,
            status=status,
        )

        route_perf.additional_properties = d
        return route_perf

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
