from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.db_perf_counters import DbPerfCounters
    from ..models.histogram import Histogram
    from ..models.pool_gauges import PoolGauges


T = TypeVar("T", bound="DbPerf")


@_attrs_define
class DbPerf:
    """Pool-wait and query observations from the engine's perf observers.

    Attributes:
        pool_wait (Histogram | None | Unset):
        query (Histogram | None | Unset):
        counters (DbPerfCounters | Unset):
        pool (PoolGauges | Unset): Local engine pool readings from metrics_pool_gauges, without a query.
    """

    pool_wait: Histogram | None | Unset = UNSET
    query: Histogram | None | Unset = UNSET
    counters: DbPerfCounters | Unset = UNSET
    pool: PoolGauges | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.histogram import Histogram

        pool_wait: dict[str, Any] | None | Unset
        if isinstance(self.pool_wait, Unset):
            pool_wait = UNSET
        elif isinstance(self.pool_wait, Histogram):
            pool_wait = self.pool_wait.to_dict()
        else:
            pool_wait = self.pool_wait

        query: dict[str, Any] | None | Unset
        if isinstance(self.query, Unset):
            query = UNSET
        elif isinstance(self.query, Histogram):
            query = self.query.to_dict()
        else:
            query = self.query

        counters: dict[str, Any] | Unset = UNSET
        if not isinstance(self.counters, Unset):
            counters = self.counters.to_dict()

        pool: dict[str, Any] | Unset = UNSET
        if not isinstance(self.pool, Unset):
            pool = self.pool.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if pool_wait is not UNSET:
            field_dict["pool_wait"] = pool_wait
        if query is not UNSET:
            field_dict["query"] = query
        if counters is not UNSET:
            field_dict["counters"] = counters
        if pool is not UNSET:
            field_dict["pool"] = pool

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.db_perf_counters import DbPerfCounters
        from ..models.histogram import Histogram
        from ..models.pool_gauges import PoolGauges

        d = dict(src_dict)

        def _parse_pool_wait(data: object) -> Histogram | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                pool_wait_type_0 = Histogram.from_dict(data)

                return pool_wait_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(Histogram | None | Unset, data)

        pool_wait = _parse_pool_wait(d.pop("pool_wait", UNSET))

        def _parse_query(data: object) -> Histogram | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                query_type_0 = Histogram.from_dict(data)

                return query_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(Histogram | None | Unset, data)

        query = _parse_query(d.pop("query", UNSET))

        _counters = d.pop("counters", UNSET)
        counters: DbPerfCounters | Unset
        if isinstance(_counters, Unset):
            counters = UNSET
        else:
            counters = DbPerfCounters.from_dict(_counters)

        _pool = d.pop("pool", UNSET)
        pool: PoolGauges | Unset
        if isinstance(_pool, Unset):
            pool = UNSET
        else:
            pool = PoolGauges.from_dict(_pool)

        db_perf = cls(
            pool_wait=pool_wait,
            query=query,
            counters=counters,
            pool=pool,
        )

        db_perf.additional_properties = d
        return db_perf

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
