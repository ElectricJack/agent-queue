from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EscalationReplyRequest")


@_attrs_define
class EscalationReplyRequest:
    """
    Attributes:
        escalation_id (str):
        text (str):
        external_message_id (str): Stable dashboard or adapter message identity for replay collapse.
        received_sequence (int | None | Unset):
    """

    escalation_id: str
    text: str
    external_message_id: str
    received_sequence: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        escalation_id = self.escalation_id

        text = self.text

        external_message_id = self.external_message_id

        received_sequence: int | None | Unset
        if isinstance(self.received_sequence, Unset):
            received_sequence = UNSET
        else:
            received_sequence = self.received_sequence

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "escalation_id": escalation_id,
                "text": text,
                "external_message_id": external_message_id,
            }
        )
        if received_sequence is not UNSET:
            field_dict["received_sequence"] = received_sequence

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        escalation_id = d.pop("escalation_id")

        text = d.pop("text")

        external_message_id = d.pop("external_message_id")

        def _parse_received_sequence(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        received_sequence = _parse_received_sequence(d.pop("received_sequence", UNSET))

        escalation_reply_request = cls(
            escalation_id=escalation_id,
            text=text,
            external_message_id=external_message_id,
            received_sequence=received_sequence,
        )

        escalation_reply_request.additional_properties = d
        return escalation_reply_request

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
