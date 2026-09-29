from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationProject")


@_attrs_define
class ProviderAllocationProject:
    """A project's routing preference and effective limit.

    Attributes:
        project_id (str):
        name (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        preferred_provider (None | str | Unset):
        assignment_playbook_id (None | str | Unset):
        max_concurrent_agents (int | None | Unset):
    """

    project_id: str
    name: str | Unset = ""
    status: str | Unset = ""
    preferred_provider: None | str | Unset = UNSET
    assignment_playbook_id: None | str | Unset = UNSET
    max_concurrent_agents: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        name = self.name

        status = self.status

        preferred_provider: None | str | Unset
        if isinstance(self.preferred_provider, Unset):
            preferred_provider = UNSET
        else:
            preferred_provider = self.preferred_provider

        assignment_playbook_id: None | str | Unset
        if isinstance(self.assignment_playbook_id, Unset):
            assignment_playbook_id = UNSET
        else:
            assignment_playbook_id = self.assignment_playbook_id

        max_concurrent_agents: int | None | Unset
        if isinstance(self.max_concurrent_agents, Unset):
            max_concurrent_agents = UNSET
        else:
            max_concurrent_agents = self.max_concurrent_agents

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if status is not UNSET:
            field_dict["status"] = status
        if preferred_provider is not UNSET:
            field_dict["preferred_provider"] = preferred_provider
        if assignment_playbook_id is not UNSET:
            field_dict["assignment_playbook_id"] = assignment_playbook_id
        if max_concurrent_agents is not UNSET:
            field_dict["max_concurrent_agents"] = max_concurrent_agents

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        name = d.pop("name", UNSET)

        status = d.pop("status", UNSET)

        def _parse_preferred_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        preferred_provider = _parse_preferred_provider(d.pop("preferred_provider", UNSET))

        def _parse_assignment_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        assignment_playbook_id = _parse_assignment_playbook_id(d.pop("assignment_playbook_id", UNSET))

        def _parse_max_concurrent_agents(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_concurrent_agents = _parse_max_concurrent_agents(d.pop("max_concurrent_agents", UNSET))

        provider_allocation_project = cls(
            project_id=project_id,
            name=name,
            status=status,
            preferred_provider=preferred_provider,
            assignment_playbook_id=assignment_playbook_id,
            max_concurrent_agents=max_concurrent_agents,
        )

        provider_allocation_project.additional_properties = d
        return provider_allocation_project

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
