from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.collaboration_message_record import CollaborationMessageRecord
    from ..models.collaboration_thread_record import CollaborationThreadRecord


T = TypeVar("T", bound="CollaborationGetResponse")


@_attrs_define
class CollaborationGetResponse:
    """
    Attributes:
        thread (CollaborationThreadRecord):
        messages (list[CollaborationMessageRecord]):
        has_more (bool):
        capacity_hold (bool):
        next_step (str):
        next_cursor (int | None | Unset):
        success (bool | Unset):  Default: True.
    """

    thread: CollaborationThreadRecord
    messages: list[CollaborationMessageRecord]
    has_more: bool
    capacity_hold: bool
    next_step: str
    next_cursor: int | None | Unset = UNSET
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        thread = self.thread.to_dict()

        messages = []
        for messages_item_data in self.messages:
            messages_item = messages_item_data.to_dict()
            messages.append(messages_item)

        has_more = self.has_more

        capacity_hold = self.capacity_hold

        next_step = self.next_step

        next_cursor: int | None | Unset
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "thread": thread,
                "messages": messages,
                "has_more": has_more,
                "capacity_hold": capacity_hold,
                "next_step": next_step,
            }
        )
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.collaboration_message_record import CollaborationMessageRecord
        from ..models.collaboration_thread_record import CollaborationThreadRecord

        d = dict(src_dict)
        thread = CollaborationThreadRecord.from_dict(d.pop("thread"))

        messages = []
        _messages = d.pop("messages")
        for messages_item_data in _messages:
            messages_item = CollaborationMessageRecord.from_dict(messages_item_data)

            messages.append(messages_item)

        has_more = d.pop("has_more")

        capacity_hold = d.pop("capacity_hold")

        next_step = d.pop("next_step")

        def _parse_next_cursor(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))

        success = d.pop("success", UNSET)

        collaboration_get_response = cls(
            thread=thread,
            messages=messages,
            has_more=has_more,
            capacity_hold=capacity_hold,
            next_step=next_step,
            next_cursor=next_cursor,
            success=success,
        )

        return collaboration_get_response
