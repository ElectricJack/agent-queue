from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.conversation_history_record import ConversationHistoryRecord


T = TypeVar("T", bound="SupervisorInboxHistoryResponse")


@_attrs_define
class SupervisorInboxHistoryResponse:
    """
    Attributes:
        conversations (list[ConversationHistoryRecord]):
        next_before (float | None): `before` for the next page; null when exhausted.
        next_before_id (None | str): `before_id` paired with `next_before`.
        success (bool | Unset):  Default: True.
    """

    conversations: list[ConversationHistoryRecord]
    next_before: float | None
    next_before_id: None | str
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        conversations = []
        for conversations_item_data in self.conversations:
            conversations_item = conversations_item_data.to_dict()
            conversations.append(conversations_item)

        next_before: float | None
        next_before = self.next_before

        next_before_id: None | str
        next_before_id = self.next_before_id

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "conversations": conversations,
                "next_before": next_before,
                "next_before_id": next_before_id,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.conversation_history_record import ConversationHistoryRecord

        d = dict(src_dict)
        conversations = []
        _conversations = d.pop("conversations")
        for conversations_item_data in _conversations:
            conversations_item = ConversationHistoryRecord.from_dict(conversations_item_data)

            conversations.append(conversations_item)

        def _parse_next_before(data: object) -> float | None:
            if data is None:
                return data
            return cast(float | None, data)

        next_before = _parse_next_before(d.pop("next_before"))

        def _parse_next_before_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        next_before_id = _parse_next_before_id(d.pop("next_before_id"))

        success = d.pop("success", UNSET)

        supervisor_inbox_history_response = cls(
            conversations=conversations,
            next_before=next_before,
            next_before_id=next_before_id,
            success=success,
        )

        supervisor_inbox_history_response.additional_properties = d
        return supervisor_inbox_history_response

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
