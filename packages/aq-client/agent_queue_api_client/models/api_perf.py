from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.api_perf_errors import ApiPerfErrors
    from ..models.api_perf_routes import ApiPerfRoutes
    from ..models.api_perf_streams import ApiPerfStreams
    from ..models.histogram import Histogram


T = TypeVar("T", bound="ApiPerf")


@_attrs_define
class ApiPerf:
    """Bounded route-template tables and totals from PerfRegistry.snapshot.

    Attributes:
        all_ (Histogram | None | Unset):
        routes (ApiPerfRoutes | Unset):
        streams (ApiPerfStreams | Unset):
        errors (ApiPerfErrors | Unset):
    """

    all_: Histogram | None | Unset = UNSET
    routes: ApiPerfRoutes | Unset = UNSET
    streams: ApiPerfStreams | Unset = UNSET
    errors: ApiPerfErrors | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.histogram import Histogram

        all_: dict[str, Any] | None | Unset
        if isinstance(self.all_, Unset):
            all_ = UNSET
        elif isinstance(self.all_, Histogram):
            all_ = self.all_.to_dict()
        else:
            all_ = self.all_

        routes: dict[str, Any] | Unset = UNSET
        if not isinstance(self.routes, Unset):
            routes = self.routes.to_dict()

        streams: dict[str, Any] | Unset = UNSET
        if not isinstance(self.streams, Unset):
            streams = self.streams.to_dict()

        errors: dict[str, Any] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = self.errors.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if all_ is not UNSET:
            field_dict["all"] = all_
        if routes is not UNSET:
            field_dict["routes"] = routes
        if streams is not UNSET:
            field_dict["streams"] = streams
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.api_perf_errors import ApiPerfErrors
        from ..models.api_perf_routes import ApiPerfRoutes
        from ..models.api_perf_streams import ApiPerfStreams
        from ..models.histogram import Histogram

        d = dict(src_dict)

        def _parse_all_(data: object) -> Histogram | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                all_type_0 = Histogram.from_dict(data)

                return all_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(Histogram | None | Unset, data)

        all_ = _parse_all_(d.pop("all", UNSET))

        _routes = d.pop("routes", UNSET)
        routes: ApiPerfRoutes | Unset
        if isinstance(_routes, Unset):
            routes = UNSET
        else:
            routes = ApiPerfRoutes.from_dict(_routes)

        _streams = d.pop("streams", UNSET)
        streams: ApiPerfStreams | Unset
        if isinstance(_streams, Unset):
            streams = UNSET
        else:
            streams = ApiPerfStreams.from_dict(_streams)

        _errors = d.pop("errors", UNSET)
        errors: ApiPerfErrors | Unset
        if isinstance(_errors, Unset):
            errors = UNSET
        else:
            errors = ApiPerfErrors.from_dict(_errors)

        api_perf = cls(
            all_=all_,
            routes=routes,
            streams=streams,
            errors=errors,
        )

        api_perf.additional_properties = d
        return api_perf

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
