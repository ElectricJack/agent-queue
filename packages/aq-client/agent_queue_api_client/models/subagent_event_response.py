from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SubagentEventResponse")


@_attrs_define
class SubagentEventResponse:
    """Receipt for one ``SubagentStart`` / ``SubagentStop`` hook delivery.

    Attributes:
        session_id (str):
        event (str):
        subagent_id (str):
        success (bool | Unset):  Default: True.
        recorded (bool | Unset):  Default: True.
        active_subagent_count (int | Unset):  Default: 0.
        subagents_spawned_total (int | Unset):  Default: 0.
    """

    session_id: str
    event: str
    subagent_id: str
    success: bool | Unset = True
    recorded: bool | Unset = True
    active_subagent_count: int | Unset = 0
    subagents_spawned_total: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        session_id = self.session_id

        event = self.event

        subagent_id = self.subagent_id

        success = self.success

        recorded = self.recorded

        active_subagent_count = self.active_subagent_count

        subagents_spawned_total = self.subagents_spawned_total

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "session_id": session_id,
                "event": event,
                "subagent_id": subagent_id,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if recorded is not UNSET:
            field_dict["recorded"] = recorded
        if active_subagent_count is not UNSET:
            field_dict["active_subagent_count"] = active_subagent_count
        if subagents_spawned_total is not UNSET:
            field_dict["subagents_spawned_total"] = subagents_spawned_total

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        session_id = d.pop("session_id")

        event = d.pop("event")

        subagent_id = d.pop("subagent_id")

        success = d.pop("success", UNSET)

        recorded = d.pop("recorded", UNSET)

        active_subagent_count = d.pop("active_subagent_count", UNSET)

        subagents_spawned_total = d.pop("subagents_spawned_total", UNSET)

        subagent_event_response = cls(
            session_id=session_id,
            event=event,
            subagent_id=subagent_id,
            success=success,
            recorded=recorded,
            active_subagent_count=active_subagent_count,
            subagents_spawned_total=subagents_spawned_total,
        )

        subagent_event_response.additional_properties = d
        return subagent_event_response

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
