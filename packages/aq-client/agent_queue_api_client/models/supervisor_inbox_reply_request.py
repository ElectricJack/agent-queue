from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="SupervisorInboxReplyRequest")


@_attrs_define
class SupervisorInboxReplyRequest:
    """
    Attributes:
        conversation_id (str):
        input_id (str):
        text (str):
        idempotency_key (str):
    """

    conversation_id: str
    input_id: str
    text: str
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        conversation_id = self.conversation_id

        input_id = self.input_id

        text = self.text

        idempotency_key = self.idempotency_key

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "conversation_id": conversation_id,
                "input_id": input_id,
                "text": text,
                "idempotency_key": idempotency_key,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        conversation_id = d.pop("conversation_id")

        input_id = d.pop("input_id")

        text = d.pop("text")

        idempotency_key = d.pop("idempotency_key")

        supervisor_inbox_reply_request = cls(
            conversation_id=conversation_id,
            input_id=input_id,
            text=text,
            idempotency_key=idempotency_key,
        )

        return supervisor_inbox_reply_request
