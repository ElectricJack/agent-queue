from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="ConversationInputRecord")


@_attrs_define
class ConversationInputRecord:
    """
    Attributes:
        id (str):
        conversation_id (str):
        verified_actor (str):
        text (None | str):
        text_expired (bool):
        state (str):
        received_at (float):
        reply_message_id (None | str):
        reply_body (None | str):
        reply_created_at (float | None):
    """

    id: str
    conversation_id: str
    verified_actor: str
    text: None | str
    text_expired: bool
    state: str
    received_at: float
    reply_message_id: None | str
    reply_body: None | str
    reply_created_at: float | None
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        conversation_id = self.conversation_id

        verified_actor = self.verified_actor

        text: None | str
        text = self.text

        text_expired = self.text_expired

        state = self.state

        received_at = self.received_at

        reply_message_id: None | str
        reply_message_id = self.reply_message_id

        reply_body: None | str
        reply_body = self.reply_body

        reply_created_at: float | None
        reply_created_at = self.reply_created_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "conversation_id": conversation_id,
                "verified_actor": verified_actor,
                "text": text,
                "text_expired": text_expired,
                "state": state,
                "received_at": received_at,
                "reply_message_id": reply_message_id,
                "reply_body": reply_body,
                "reply_created_at": reply_created_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        conversation_id = d.pop("conversation_id")

        verified_actor = d.pop("verified_actor")

        def _parse_text(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        text = _parse_text(d.pop("text"))

        text_expired = d.pop("text_expired")

        state = d.pop("state")

        received_at = d.pop("received_at")

        def _parse_reply_message_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        reply_message_id = _parse_reply_message_id(d.pop("reply_message_id"))

        def _parse_reply_body(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        reply_body = _parse_reply_body(d.pop("reply_body"))

        def _parse_reply_created_at(data: object) -> float | None:
            if data is None:
                return data
            return cast(float | None, data)

        reply_created_at = _parse_reply_created_at(d.pop("reply_created_at"))

        conversation_input_record = cls(
            id=id,
            conversation_id=conversation_id,
            verified_actor=verified_actor,
            text=text,
            text_expired=text_expired,
            state=state,
            received_at=received_at,
            reply_message_id=reply_message_id,
            reply_body=reply_body,
            reply_created_at=reply_created_at,
        )

        conversation_input_record.additional_properties = d
        return conversation_input_record

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
