from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SupervisorInboxReplyResponse")


@_attrs_define
class SupervisorInboxReplyResponse:
    """
    Attributes:
        created (bool):
        reply_message_id (str):
        delivery_dedup_key (str):
        discord_text_chars (int):
        truncated (bool):
        success (bool | Unset):  Default: True.
    """

    created: bool
    reply_message_id: str
    delivery_dedup_key: str
    discord_text_chars: int
    truncated: bool
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        reply_message_id = self.reply_message_id

        delivery_dedup_key = self.delivery_dedup_key

        discord_text_chars = self.discord_text_chars

        truncated = self.truncated

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
                "reply_message_id": reply_message_id,
                "delivery_dedup_key": delivery_dedup_key,
                "discord_text_chars": discord_text_chars,
                "truncated": truncated,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created = d.pop("created")

        reply_message_id = d.pop("reply_message_id")

        delivery_dedup_key = d.pop("delivery_dedup_key")

        discord_text_chars = d.pop("discord_text_chars")

        truncated = d.pop("truncated")

        success = d.pop("success", UNSET)

        supervisor_inbox_reply_response = cls(
            created=created,
            reply_message_id=reply_message_id,
            delivery_dedup_key=delivery_dedup_key,
            discord_text_chars=discord_text_chars,
            truncated=truncated,
            success=success,
        )

        supervisor_inbox_reply_response.additional_properties = d
        return supervisor_inbox_reply_response

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
