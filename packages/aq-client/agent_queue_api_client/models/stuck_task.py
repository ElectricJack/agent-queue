from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="StuckTask")


@_attrs_define
class StuckTask:
    """
    Attributes:
        id (str):
        project_id (str):
        status (str):
        updated_at (float):
        seconds_in_state (float):
        assigned_agent (None | str | Unset):
    """

    id: str
    project_id: str
    status: str
    updated_at: float
    seconds_in_state: float
    assigned_agent: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        project_id = self.project_id

        status = self.status

        updated_at = self.updated_at

        seconds_in_state = self.seconds_in_state

        assigned_agent: None | str | Unset
        if isinstance(self.assigned_agent, Unset):
            assigned_agent = UNSET
        else:
            assigned_agent = self.assigned_agent

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "project_id": project_id,
                "status": status,
                "updated_at": updated_at,
                "seconds_in_state": seconds_in_state,
            }
        )
        if assigned_agent is not UNSET:
            field_dict["assigned_agent"] = assigned_agent

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id")

        status = d.pop("status")

        updated_at = d.pop("updated_at")

        seconds_in_state = d.pop("seconds_in_state")

        def _parse_assigned_agent(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        assigned_agent = _parse_assigned_agent(d.pop("assigned_agent", UNSET))

        stuck_task = cls(
            id=id,
            project_id=project_id,
            status=status,
            updated_at=updated_at,
            seconds_in_state=seconds_in_state,
            assigned_agent=assigned_agent,
        )

        stuck_task.additional_properties = d
        return stuck_task

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
