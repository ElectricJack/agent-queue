from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_hidden import ProviderAllocationHidden
    from ..models.provider_allocation_intent import ProviderAllocationIntent
    from ..models.provider_allocation_project_supply import ProviderAllocationProjectSupply
    from ..models.provider_allocation_session import ProviderAllocationSession
    from ..models.provider_allocation_supply import ProviderAllocationSupply


T = TypeVar("T", bound="ProviderAllocationProfile")


@_attrs_define
class ProviderAllocationProfile:
    """One ordinary worker profile: its bounds, supply, sessions and pins.

    Attributes:
        profile_id (str):
        harness (str):
        lifecycle (str):
        supply (ProviderAllocationSupply): Supply counters as the pool sizer reads them.

            ``ready`` is pool demand and is ``None`` for a task-lifecycle profile,
            which has live sessions but no pool.
        pinned (ProviderAllocationIntent): READY/ASSIGNED/IN_PROGRESS tasks carrying one explicit provider intent.

            ``count`` and ``by_status`` are fleet-wide; ``task_ids`` holds only the
            ones inside the caller's view.
        preferred (ProviderAllocationIntent): READY/ASSIGNED/IN_PROGRESS tasks carrying one explicit provider intent.

            ``count`` and ``by_status`` are fleet-wide; ``task_ids`` holds only the
            ones inside the caller's view.
        hidden (ProviderAllocationHidden): What the caller's view left out of one profile.
        name (str | Unset):  Default: ''.
        enabled (bool | Unset):  Default: True.
        intelligence_class (str | Unset):  Default: ''.
        min_active (int | None | Unset):
        max_active (int | None | Unset):
        min_per_project (int | None | Unset):
        projects (list[ProviderAllocationProjectSupply] | Unset):
        sessions (list[ProviderAllocationSession] | Unset):
    """

    profile_id: str
    harness: str
    lifecycle: str
    supply: ProviderAllocationSupply
    pinned: ProviderAllocationIntent
    preferred: ProviderAllocationIntent
    hidden: ProviderAllocationHidden
    name: str | Unset = ""
    enabled: bool | Unset = True
    intelligence_class: str | Unset = ""
    min_active: int | None | Unset = UNSET
    max_active: int | None | Unset = UNSET
    min_per_project: int | None | Unset = UNSET
    projects: list[ProviderAllocationProjectSupply] | Unset = UNSET
    sessions: list[ProviderAllocationSession] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile_id = self.profile_id

        harness = self.harness

        lifecycle = self.lifecycle

        supply = self.supply.to_dict()

        pinned = self.pinned.to_dict()

        preferred = self.preferred.to_dict()

        hidden = self.hidden.to_dict()

        name = self.name

        enabled = self.enabled

        intelligence_class = self.intelligence_class

        min_active: int | None | Unset
        if isinstance(self.min_active, Unset):
            min_active = UNSET
        else:
            min_active = self.min_active

        max_active: int | None | Unset
        if isinstance(self.max_active, Unset):
            max_active = UNSET
        else:
            max_active = self.max_active

        min_per_project: int | None | Unset
        if isinstance(self.min_per_project, Unset):
            min_per_project = UNSET
        else:
            min_per_project = self.min_per_project

        projects: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.projects, Unset):
            projects = []
            for projects_item_data in self.projects:
                projects_item = projects_item_data.to_dict()
                projects.append(projects_item)

        sessions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.sessions, Unset):
            sessions = []
            for sessions_item_data in self.sessions:
                sessions_item = sessions_item_data.to_dict()
                sessions.append(sessions_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile_id": profile_id,
                "harness": harness,
                "lifecycle": lifecycle,
                "supply": supply,
                "pinned": pinned,
                "preferred": preferred,
                "hidden": hidden,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if min_active is not UNSET:
            field_dict["min_active"] = min_active
        if max_active is not UNSET:
            field_dict["max_active"] = max_active
        if min_per_project is not UNSET:
            field_dict["min_per_project"] = min_per_project
        if projects is not UNSET:
            field_dict["projects"] = projects
        if sessions is not UNSET:
            field_dict["sessions"] = sessions

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_hidden import ProviderAllocationHidden
        from ..models.provider_allocation_intent import ProviderAllocationIntent
        from ..models.provider_allocation_project_supply import ProviderAllocationProjectSupply
        from ..models.provider_allocation_session import ProviderAllocationSession
        from ..models.provider_allocation_supply import ProviderAllocationSupply

        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        harness = d.pop("harness")

        lifecycle = d.pop("lifecycle")

        supply = ProviderAllocationSupply.from_dict(d.pop("supply"))

        pinned = ProviderAllocationIntent.from_dict(d.pop("pinned"))

        preferred = ProviderAllocationIntent.from_dict(d.pop("preferred"))

        hidden = ProviderAllocationHidden.from_dict(d.pop("hidden"))

        name = d.pop("name", UNSET)

        enabled = d.pop("enabled", UNSET)

        intelligence_class = d.pop("intelligence_class", UNSET)

        def _parse_min_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        min_active = _parse_min_active(d.pop("min_active", UNSET))

        def _parse_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_active = _parse_max_active(d.pop("max_active", UNSET))

        def _parse_min_per_project(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        min_per_project = _parse_min_per_project(d.pop("min_per_project", UNSET))

        _projects = d.pop("projects", UNSET)
        projects: list[ProviderAllocationProjectSupply] | Unset = UNSET
        if _projects is not UNSET:
            projects = []
            for projects_item_data in _projects:
                projects_item = ProviderAllocationProjectSupply.from_dict(projects_item_data)

                projects.append(projects_item)

        _sessions = d.pop("sessions", UNSET)
        sessions: list[ProviderAllocationSession] | Unset = UNSET
        if _sessions is not UNSET:
            sessions = []
            for sessions_item_data in _sessions:
                sessions_item = ProviderAllocationSession.from_dict(sessions_item_data)

                sessions.append(sessions_item)

        provider_allocation_profile = cls(
            profile_id=profile_id,
            harness=harness,
            lifecycle=lifecycle,
            supply=supply,
            pinned=pinned,
            preferred=preferred,
            hidden=hidden,
            name=name,
            enabled=enabled,
            intelligence_class=intelligence_class,
            min_active=min_active,
            max_active=max_active,
            min_per_project=min_per_project,
            projects=projects,
            sessions=sessions,
        )

        provider_allocation_profile.additional_properties = d
        return provider_allocation_profile

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
