from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationHidden")


@_attrs_define
class ProviderAllocationHidden:
    """What the caller's view left out of one profile.

    Attributes:
        projects (int | Unset):  Default: 0.
        sessions (int | Unset):  Default: 0.
        tasks (int | Unset):  Default: 0.
    """

    projects: int | Unset = 0
    sessions: int | Unset = 0
    tasks: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        projects = self.projects

        sessions = self.sessions

        tasks = self.tasks

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if projects is not UNSET:
            field_dict["projects"] = projects
        if sessions is not UNSET:
            field_dict["sessions"] = sessions
        if tasks is not UNSET:
            field_dict["tasks"] = tasks

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        projects = d.pop("projects", UNSET)

        sessions = d.pop("sessions", UNSET)

        tasks = d.pop("tasks", UNSET)

        provider_allocation_hidden = cls(
            projects=projects,
            sessions=sessions,
            tasks=tasks,
        )

        provider_allocation_hidden.additional_properties = d
        return provider_allocation_hidden

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
