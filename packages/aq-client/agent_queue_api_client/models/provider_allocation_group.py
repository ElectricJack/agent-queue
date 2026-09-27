from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_ceiling import ProviderAllocationCeiling
    from ..models.provider_allocation_event import ProviderAllocationEvent
    from ..models.provider_allocation_manual_agent import ProviderAllocationManualAgent
    from ..models.provider_allocation_profile import ProviderAllocationProfile
    from ..models.provider_allocation_supply import ProviderAllocationSupply


T = TypeVar("T", bound="ProviderAllocationGroup")


@_attrs_define
class ProviderAllocationGroup:
    """One provider: its ordinary worker profiles and what runs on them.

    Attributes:
        provider (str):
        supply (ProviderAllocationSupply): Supply counters as the pool sizer reads them.

            ``ready`` is pool demand and is ``None`` for a task-lifecycle profile,
            which has live sessions but no pool.
        ceiling (ProviderAllocationCeiling): The provider-wide configured ceiling over its enabled pool profiles.

            ``max_active`` is ``None`` (and ``unbounded`` true) when any of them is
            unbounded.  Bounds stay per profile; this total is for reading only.
        vendor (str | Unset):  Default: ''.
        state (str | Unset):  Default: 'available'.
        harnesses (list[str] | Unset):
        profiles (list[ProviderAllocationProfile] | Unset):
        manual_agents (list[ProviderAllocationManualAgent] | Unset):
        pinned_tasks (int | Unset):  Default: 0.
        preferred_tasks (int | Unset):  Default: 0.
        last_allocation (None | ProviderAllocationEvent | Unset):
    """

    provider: str
    supply: ProviderAllocationSupply
    ceiling: ProviderAllocationCeiling
    vendor: str | Unset = ""
    state: str | Unset = "available"
    harnesses: list[str] | Unset = UNSET
    profiles: list[ProviderAllocationProfile] | Unset = UNSET
    manual_agents: list[ProviderAllocationManualAgent] | Unset = UNSET
    pinned_tasks: int | Unset = 0
    preferred_tasks: int | Unset = 0
    last_allocation: None | ProviderAllocationEvent | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.provider_allocation_event import ProviderAllocationEvent

        provider = self.provider

        supply = self.supply.to_dict()

        ceiling = self.ceiling.to_dict()

        vendor = self.vendor

        state = self.state

        harnesses: list[str] | Unset = UNSET
        if not isinstance(self.harnesses, Unset):
            harnesses = self.harnesses

        profiles: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.profiles, Unset):
            profiles = []
            for profiles_item_data in self.profiles:
                profiles_item = profiles_item_data.to_dict()
                profiles.append(profiles_item)

        manual_agents: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.manual_agents, Unset):
            manual_agents = []
            for manual_agents_item_data in self.manual_agents:
                manual_agents_item = manual_agents_item_data.to_dict()
                manual_agents.append(manual_agents_item)

        pinned_tasks = self.pinned_tasks

        preferred_tasks = self.preferred_tasks

        last_allocation: dict[str, Any] | None | Unset
        if isinstance(self.last_allocation, Unset):
            last_allocation = UNSET
        elif isinstance(self.last_allocation, ProviderAllocationEvent):
            last_allocation = self.last_allocation.to_dict()
        else:
            last_allocation = self.last_allocation

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "supply": supply,
                "ceiling": ceiling,
            }
        )
        if vendor is not UNSET:
            field_dict["vendor"] = vendor
        if state is not UNSET:
            field_dict["state"] = state
        if harnesses is not UNSET:
            field_dict["harnesses"] = harnesses
        if profiles is not UNSET:
            field_dict["profiles"] = profiles
        if manual_agents is not UNSET:
            field_dict["manual_agents"] = manual_agents
        if pinned_tasks is not UNSET:
            field_dict["pinned_tasks"] = pinned_tasks
        if preferred_tasks is not UNSET:
            field_dict["preferred_tasks"] = preferred_tasks
        if last_allocation is not UNSET:
            field_dict["last_allocation"] = last_allocation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_ceiling import ProviderAllocationCeiling
        from ..models.provider_allocation_event import ProviderAllocationEvent
        from ..models.provider_allocation_manual_agent import ProviderAllocationManualAgent
        from ..models.provider_allocation_profile import ProviderAllocationProfile
        from ..models.provider_allocation_supply import ProviderAllocationSupply

        d = dict(src_dict)
        provider = d.pop("provider")

        supply = ProviderAllocationSupply.from_dict(d.pop("supply"))

        ceiling = ProviderAllocationCeiling.from_dict(d.pop("ceiling"))

        vendor = d.pop("vendor", UNSET)

        state = d.pop("state", UNSET)

        harnesses = cast(list[str], d.pop("harnesses", UNSET))

        _profiles = d.pop("profiles", UNSET)
        profiles: list[ProviderAllocationProfile] | Unset = UNSET
        if _profiles is not UNSET:
            profiles = []
            for profiles_item_data in _profiles:
                profiles_item = ProviderAllocationProfile.from_dict(profiles_item_data)

                profiles.append(profiles_item)

        _manual_agents = d.pop("manual_agents", UNSET)
        manual_agents: list[ProviderAllocationManualAgent] | Unset = UNSET
        if _manual_agents is not UNSET:
            manual_agents = []
            for manual_agents_item_data in _manual_agents:
                manual_agents_item = ProviderAllocationManualAgent.from_dict(manual_agents_item_data)

                manual_agents.append(manual_agents_item)

        pinned_tasks = d.pop("pinned_tasks", UNSET)

        preferred_tasks = d.pop("preferred_tasks", UNSET)

        def _parse_last_allocation(data: object) -> None | ProviderAllocationEvent | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                last_allocation_type_0 = ProviderAllocationEvent.from_dict(data)

                return last_allocation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAllocationEvent | Unset, data)

        last_allocation = _parse_last_allocation(d.pop("last_allocation", UNSET))

        provider_allocation_group = cls(
            provider=provider,
            supply=supply,
            ceiling=ceiling,
            vendor=vendor,
            state=state,
            harnesses=harnesses,
            profiles=profiles,
            manual_agents=manual_agents,
            pinned_tasks=pinned_tasks,
            preferred_tasks=preferred_tasks,
            last_allocation=last_allocation,
        )

        provider_allocation_group.additional_properties = d
        return provider_allocation_group

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
