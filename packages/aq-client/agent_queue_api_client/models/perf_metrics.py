from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.api_perf import ApiPerf
    from ..models.db_perf import DbPerf
    from ..models.host_perf import HostPerf
    from ..models.loop_perf import LoopPerf
    from ..models.perf_sampler_cost import PerfSamplerCost
    from ..models.relay_perf import RelayPerf


T = TypeVar("T", bound="PerfMetrics")


@_attrs_define
class PerfMetrics:
    """MetricsSampler's daemon and dashboard-server performance blocks.

    Attributes:
        enabled (bool | Unset):  Default: False.
        loop (LoopPerf | Unset): Event-loop drift observed by the registry's LoopLagProbe.
        api (ApiPerf | Unset): Bounded route-template tables and totals from PerfRegistry.snapshot.
        db (DbPerf | Unset): Pool-wait and query observations from the engine's perf observers.
        host (HostPerf | Unset): Slow-tier HostSampler snapshot, including its cost and staleness.
        relay (RelayPerf | Unset): Dashboard-server relay deltas; unavailable until the relay is polled.
        sampler (PerfSamplerCost | Unset): Instrumentation-only cost measured by MetricsSampler.
    """

    enabled: bool | Unset = False
    loop: LoopPerf | Unset = UNSET
    api: ApiPerf | Unset = UNSET
    db: DbPerf | Unset = UNSET
    host: HostPerf | Unset = UNSET
    relay: RelayPerf | Unset = UNSET
    sampler: PerfSamplerCost | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        enabled = self.enabled

        loop: dict[str, Any] | Unset = UNSET
        if not isinstance(self.loop, Unset):
            loop = self.loop.to_dict()

        api: dict[str, Any] | Unset = UNSET
        if not isinstance(self.api, Unset):
            api = self.api.to_dict()

        db: dict[str, Any] | Unset = UNSET
        if not isinstance(self.db, Unset):
            db = self.db.to_dict()

        host: dict[str, Any] | Unset = UNSET
        if not isinstance(self.host, Unset):
            host = self.host.to_dict()

        relay: dict[str, Any] | Unset = UNSET
        if not isinstance(self.relay, Unset):
            relay = self.relay.to_dict()

        sampler: dict[str, Any] | Unset = UNSET
        if not isinstance(self.sampler, Unset):
            sampler = self.sampler.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if loop is not UNSET:
            field_dict["loop"] = loop
        if api is not UNSET:
            field_dict["api"] = api
        if db is not UNSET:
            field_dict["db"] = db
        if host is not UNSET:
            field_dict["host"] = host
        if relay is not UNSET:
            field_dict["relay"] = relay
        if sampler is not UNSET:
            field_dict["sampler"] = sampler

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.api_perf import ApiPerf
        from ..models.db_perf import DbPerf
        from ..models.host_perf import HostPerf
        from ..models.loop_perf import LoopPerf
        from ..models.perf_sampler_cost import PerfSamplerCost
        from ..models.relay_perf import RelayPerf

        d = dict(src_dict)
        enabled = d.pop("enabled", UNSET)

        _loop = d.pop("loop", UNSET)
        loop: LoopPerf | Unset
        if isinstance(_loop, Unset):
            loop = UNSET
        else:
            loop = LoopPerf.from_dict(_loop)

        _api = d.pop("api", UNSET)
        api: ApiPerf | Unset
        if isinstance(_api, Unset):
            api = UNSET
        else:
            api = ApiPerf.from_dict(_api)

        _db = d.pop("db", UNSET)
        db: DbPerf | Unset
        if isinstance(_db, Unset):
            db = UNSET
        else:
            db = DbPerf.from_dict(_db)

        _host = d.pop("host", UNSET)
        host: HostPerf | Unset
        if isinstance(_host, Unset):
            host = UNSET
        else:
            host = HostPerf.from_dict(_host)

        _relay = d.pop("relay", UNSET)
        relay: RelayPerf | Unset
        if isinstance(_relay, Unset):
            relay = UNSET
        else:
            relay = RelayPerf.from_dict(_relay)

        _sampler = d.pop("sampler", UNSET)
        sampler: PerfSamplerCost | Unset
        if isinstance(_sampler, Unset):
            sampler = UNSET
        else:
            sampler = PerfSamplerCost.from_dict(_sampler)

        perf_metrics = cls(
            enabled=enabled,
            loop=loop,
            api=api,
            db=db,
            host=host,
            relay=relay,
            sampler=sampler,
        )

        perf_metrics.additional_properties = d
        return perf_metrics

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
