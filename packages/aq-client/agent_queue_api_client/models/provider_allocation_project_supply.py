from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationProjectSupply")


@_attrs_define
class ProviderAllocationProjectSupply:
    """One project's share of a profile's supply.

    Attributes:
        ready (int | None | Unset):  Default: 0.
        idle (int | Unset):  Default: 0.
        busy (int | Unset):  Default: 0.
        starting (int | Unset):  Default: 0.
        draining (int | Unset):  Default: 0.
        unresponsive (int | Unset):  Default: 0.
        project_id (None | str | Unset):
    """

    ready: int | None | Unset = 0
    idle: int | Unset = 0
    busy: int | Unset = 0
    starting: int | Unset = 0
    draining: int | Unset = 0
    unresponsive: int | Unset = 0
    project_id: None | str | Unset = UNSET
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

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

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
        if project_id is not UNSET:
            field_dict["project_id"] = project_id

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

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        provider_allocation_project_supply = cls(
            ready=ready,
            idle=idle,
            busy=busy,
            starting=starting,
            draining=draining,
            unresponsive=unresponsive,
            project_id=project_id,
        )

        provider_allocation_project_supply.additional_properties = d
        return provider_allocation_project_supply

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
