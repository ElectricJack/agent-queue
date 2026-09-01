from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.message_model import MessageModel


T = TypeVar("T", bound="MessageReplyResponse")


@_attrs_define
class MessageReplyResponse:
    """
    Attributes:
        message_id (str):
        reply_id (str):
        reply (MessageModel): Rendered message dict (see ``src/commands/message_commands.py::message_to_dict``).
    """

    message_id: str
    reply_id: str
    reply: MessageModel
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        message_id = self.message_id

        reply_id = self.reply_id

        reply = self.reply.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "message_id": message_id,
                "reply_id": reply_id,
                "reply": reply,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.message_model import MessageModel  # noqa: PLC0415

        d = dict(src_dict)
        message_id = d.pop("message_id")

        reply_id = d.pop("reply_id")

        reply = MessageModel.from_dict(d.pop("reply"))

        message_reply_response = cls(
            message_id=message_id,
            reply_id=reply_id,
            reply=reply,
        )

        message_reply_response.additional_properties = d
        return message_reply_response

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
