from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationSupply")


@_attrs_define
class ProviderAllocationSupply:
    """Supply counters as the pool sizer reads them.

    ``ready`` is pool demand and is ``None`` for a task-lifecycle profile,
    which has live sessions but no pool.

        Attributes:
            ready (int | None | Unset):  Default: 0.
            idle (int | Unset):  Default: 0.
            busy (int | Unset):  Default: 0.
            starting (int | Unset):  Default: 0.
            draining (int | Unset):  Default: 0.
            unresponsive (int | Unset):  Default: 0.
    """

    ready: int | None | Unset = 0
    idle: int | Unset = 0
    busy: int | Unset = 0
    starting: int | Unset = 0
    draining: int | Unset = 0
    unresponsive: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ready: int | None | Unset
        if isinstance(self.ready, Unset):
            ready = UNSET
        else:
            ready = self.ready

        idle = self.idle

        busy = self.busy

        starting = self.starting

        draining = self.draining

        unresponsive = self.unresponsive

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if ready is not UNSET:
            field_dict["ready"] = ready
        if idle is not UNSET:
            field_dict["idle"] = idle
        if busy is not UNSET:
            field_dict["busy"] = busy
        if starting is not UNSET:
            field_dict["starting"] = starting
        if draining is not UNSET:
            field_dict["draining"] = draining
        if unresponsive is not UNSET:
            field_dict["unresponsive"] = unresponsive

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_ready(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        ready = _parse_ready(d.pop("ready", UNSET))

        idle = d.pop("idle", UNSET)

        busy = d.pop("busy", UNSET)

        starting = d.pop("starting", UNSET)

        draining = d.pop("draining", UNSET)

        unresponsive = d.pop("unresponsive", UNSET)

        provider_allocation_supply = cls(
            ready=ready,
            idle=idle,
            busy=busy,
            starting=starting,
            draining=draining,
            unresponsive=unresponsive,
        )

        provider_allocation_supply.additional_properties = d
        return provider_allocation_supply

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
