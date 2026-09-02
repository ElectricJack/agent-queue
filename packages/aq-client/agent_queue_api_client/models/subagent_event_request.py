from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SubagentEventRequest")


@_attrs_define
class SubagentEventRequest:
    """
    Attributes:
        event (str): Native lifecycle event reported by the harness hook.
        subagent_id (str): Harness-provided identifier for the child agent.
        agent_type (None | str | Unset): Optional harness sub-agent type.
        turn_id (None | str | Unset): Optional harness turn identifier.
        session_id (None | str | Unset): Session to attribute the event to for local replay only; a bearer token always
            supplies its own session identity.
    """

    event: str
    subagent_id: str
    agent_type: None | str | Unset = UNSET
    turn_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        event = self.event

        subagent_id = self.subagent_id

        agent_type: None | str | Unset
        if isinstance(self.agent_type, Unset):
            agent_type = UNSET
        else:
            agent_type = self.agent_type

        turn_id: None | str | Unset
        if isinstance(self.turn_id, Unset):
            turn_id = UNSET
        else:
            turn_id = self.turn_id

        session_id: None | str | Unset
        if isinstance(self.session_id, Unset):
            session_id = UNSET
        else:
            session_id = self.session_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "event": event,
                "subagent_id": subagent_id,
            }
        )
        if agent_type is not UNSET:
            field_dict["agent_type"] = agent_type
        if turn_id is not UNSET:
            field_dict["turn_id"] = turn_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        event = d.pop("event")

        subagent_id = d.pop("subagent_id")

        def _parse_agent_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        agent_type = _parse_agent_type(d.pop("agent_type", UNSET))

        def _parse_turn_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        turn_id = _parse_turn_id(d.pop("turn_id", UNSET))

        def _parse_session_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_id = _parse_session_id(d.pop("session_id", UNSET))

        subagent_event_request = cls(
            event=event,
            subagent_id=subagent_id,
            agent_type=agent_type,
            turn_id=turn_id,
            session_id=session_id,
        )

        subagent_event_request.additional_properties = d
        return subagent_event_request

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
