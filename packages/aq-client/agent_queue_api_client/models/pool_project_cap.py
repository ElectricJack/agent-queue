from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PoolProjectCap")


@_attrs_define
class PoolProjectCap:
    """A project's runtime ceiling for a pool whose bounds are global.

    Attributes:
        project_id (str):
        max_concurrent_agents (int | None | Unset):
        effective_max_active (int | None | Unset):
    """

    project_id: str
    max_concurrent_agents: int | None | Unset = UNSET
    effective_max_active: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        max_concurrent_agents: int | None | Unset
        if isinstance(self.max_concurrent_agents, Unset):
            max_concurrent_agents = UNSET
        else:
            max_concurrent_agents = self.max_concurrent_agents

        effective_max_active: int | None | Unset
        if isinstance(self.effective_max_active, Unset):
            effective_max_active = UNSET
        else:
            effective_max_active = self.effective_max_active

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if max_concurrent_agents is not UNSET:
            field_dict["max_concurrent_agents"] = max_concurrent_agents
        if effective_max_active is not UNSET:
            field_dict["effective_max_active"] = effective_max_active

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_max_concurrent_agents(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_concurrent_agents = _parse_max_concurrent_agents(d.pop("max_concurrent_agents", UNSET))

        def _parse_effective_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        effective_max_active = _parse_effective_max_active(d.pop("effective_max_active", UNSET))

        pool_project_cap = cls(
            project_id=project_id,
            max_concurrent_agents=max_concurrent_agents,
            effective_max_active=effective_max_active,
        )

        pool_project_cap.additional_properties = d
        return pool_project_cap

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
