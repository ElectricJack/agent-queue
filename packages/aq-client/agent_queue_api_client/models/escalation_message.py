from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EscalationMessage")


@_attrs_define
class EscalationMessage:
    """
    Attributes:
        id (str):
        escalation_id (str):
        direction (str):
        transport (str):
        verified_actor (str):
        text (str):
        received_at (float):
        created_at (float):
        external_message_id (None | str | Unset):
        received_sequence (int | None | Unset):
        supervisor_message_id (None | str | Unset):
    """

    id: str
    escalation_id: str
    direction: str
    transport: str
    verified_actor: str
    text: str
    received_at: float
    created_at: float
    external_message_id: None | str | Unset = UNSET
    received_sequence: int | None | Unset = UNSET
    supervisor_message_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        escalation_id = self.escalation_id

        direction = self.direction

        transport = self.transport

        verified_actor = self.verified_actor

        text = self.text

        received_at = self.received_at

        created_at = self.created_at

        external_message_id: None | str | Unset
        if isinstance(self.external_message_id, Unset):
            external_message_id = UNSET
        else:
            external_message_id = self.external_message_id

        received_sequence: int | None | Unset
        if isinstance(self.received_sequence, Unset):
            received_sequence = UNSET
        else:
            received_sequence = self.received_sequence

        supervisor_message_id: None | str | Unset
        if isinstance(self.supervisor_message_id, Unset):
            supervisor_message_id = UNSET
        else:
            supervisor_message_id = self.supervisor_message_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "escalation_id": escalation_id,
                "direction": direction,
                "transport": transport,
                "verified_actor": verified_actor,
                "text": text,
                "received_at": received_at,
                "created_at": created_at,
            }
        )
        if external_message_id is not UNSET:
            field_dict["external_message_id"] = external_message_id
        if received_sequence is not UNSET:
            field_dict["received_sequence"] = received_sequence
        if supervisor_message_id is not UNSET:
            field_dict["supervisor_message_id"] = supervisor_message_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        escalation_id = d.pop("escalation_id")

        direction = d.pop("direction")

        transport = d.pop("transport")

        verified_actor = d.pop("verified_actor")

        text = d.pop("text")

        received_at = d.pop("received_at")

        created_at = d.pop("created_at")

        def _parse_external_message_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        external_message_id = _parse_external_message_id(d.pop("external_message_id", UNSET))

        def _parse_received_sequence(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        received_sequence = _parse_received_sequence(d.pop("received_sequence", UNSET))

        def _parse_supervisor_message_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        supervisor_message_id = _parse_supervisor_message_id(d.pop("supervisor_message_id", UNSET))

        escalation_message = cls(
            id=id,
            escalation_id=escalation_id,
            direction=direction,
            transport=transport,
            verified_actor=verified_actor,
            text=text,
            received_at=received_at,
            created_at=created_at,
            external_message_id=external_message_id,
            received_sequence=received_sequence,
            supervisor_message_id=supervisor_message_id,
        )

        escalation_message.additional_properties = d
        return escalation_message

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
