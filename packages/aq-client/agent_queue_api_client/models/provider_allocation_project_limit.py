from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationProjectLimit")


@_attrs_define
class ProviderAllocationProjectLimit:
    """One project's effective max for one changed pool profile (``aq pool scale``).

    Attributes:
        project_id (str):
        profile_id (str):
        lifecycle_before (str):
        lifecycle_after (str):
        max_concurrent_agents (int | None | Unset):
        effective_max_before (int | None | Unset):
        effective_max_after (int | None | Unset):
    """

    project_id: str
    profile_id: str
    lifecycle_before: str
    lifecycle_after: str
    max_concurrent_agents: int | None | Unset = UNSET
    effective_max_before: int | None | Unset = UNSET
    effective_max_after: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        profile_id = self.profile_id

        lifecycle_before = self.lifecycle_before

        lifecycle_after = self.lifecycle_after

        max_concurrent_agents: int | None | Unset
        if isinstance(self.max_concurrent_agents, Unset):
            max_concurrent_agents = UNSET
        else:
            max_concurrent_agents = self.max_concurrent_agents

        effective_max_before: int | None | Unset
        if isinstance(self.effective_max_before, Unset):
            effective_max_before = UNSET
        else:
            effective_max_before = self.effective_max_before

        effective_max_after: int | None | Unset
        if isinstance(self.effective_max_after, Unset):
            effective_max_after = UNSET
        else:
            effective_max_after = self.effective_max_after

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "profile_id": profile_id,
                "lifecycle_before": lifecycle_before,
                "lifecycle_after": lifecycle_after,
            }
        )
        if max_concurrent_agents is not UNSET:
            field_dict["max_concurrent_agents"] = max_concurrent_agents
        if effective_max_before is not UNSET:
            field_dict["effective_max_before"] = effective_max_before
        if effective_max_after is not UNSET:
            field_dict["effective_max_after"] = effective_max_after

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        profile_id = d.pop("profile_id")

        lifecycle_before = d.pop("lifecycle_before")

        lifecycle_after = d.pop("lifecycle_after")

        def _parse_max_concurrent_agents(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_concurrent_agents = _parse_max_concurrent_agents(d.pop("max_concurrent_agents", UNSET))

        def _parse_effective_max_before(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        effective_max_before = _parse_effective_max_before(d.pop("effective_max_before", UNSET))

        def _parse_effective_max_after(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        effective_max_after = _parse_effective_max_after(d.pop("effective_max_after", UNSET))

        provider_allocation_project_limit = cls(
            project_id=project_id,
            profile_id=profile_id,
            lifecycle_before=lifecycle_before,
            lifecycle_after=lifecycle_after,
            max_concurrent_agents=max_concurrent_agents,
            effective_max_before=effective_max_before,
            effective_max_after=effective_max_after,
        )

        provider_allocation_project_limit.additional_properties = d
        return provider_allocation_project_limit

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
