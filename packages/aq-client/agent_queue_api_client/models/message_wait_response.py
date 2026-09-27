from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.message_wait_response_state import MessageWaitResponseState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_wait_record import AgentWaitRecord
    from ..models.collaboration_message_record import CollaborationMessageRecord


T = TypeVar("T", bound="MessageWaitResponse")


@_attrs_define
class MessageWaitResponse:
    """
    Attributes:
        state (MessageWaitResponseState):
        wait (AgentWaitRecord): Persisted wait/result shape shared by contracts and generated clients.
        messages (list[CollaborationMessageRecord] | None | Unset):
        next_cursor (int | None | Unset):
        has_more (bool | None | Unset):
        cursor (int | None | Unset):
        next_step (None | str | Unset):
        success (bool | Unset):  Default: True.
    """

    state: MessageWaitResponseState
    wait: AgentWaitRecord
    messages: list[CollaborationMessageRecord] | None | Unset = UNSET
    next_cursor: int | None | Unset = UNSET
    has_more: bool | None | Unset = UNSET
    cursor: int | None | Unset = UNSET
    next_step: None | str | Unset = UNSET
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        state = self.state.value

        wait = self.wait.to_dict()

        messages: list[dict[str, Any]] | None | Unset
        if isinstance(self.messages, Unset):
            messages = UNSET
        elif isinstance(self.messages, list):
            messages = []
            for messages_type_0_item_data in self.messages:
                messages_type_0_item = messages_type_0_item_data.to_dict()
                messages.append(messages_type_0_item)

        else:
            messages = self.messages

        next_cursor: int | None | Unset
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor

        has_more: bool | None | Unset
        if isinstance(self.has_more, Unset):
            has_more = UNSET
        else:
            has_more = self.has_more

        cursor: int | None | Unset
        if isinstance(self.cursor, Unset):
            cursor = UNSET
        else:
            cursor = self.cursor

        next_step: None | str | Unset
        if isinstance(self.next_step, Unset):
            next_step = UNSET
        else:
            next_step = self.next_step

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "state": state,
                "wait": wait,
            }
        )
        if messages is not UNSET:
            field_dict["messages"] = messages
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor
        if has_more is not UNSET:
            field_dict["has_more"] = has_more
        if cursor is not UNSET:
            field_dict["cursor"] = cursor
        if next_step is not UNSET:
            field_dict["next_step"] = next_step
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_wait_record import AgentWaitRecord
        from ..models.collaboration_message_record import CollaborationMessageRecord

        d = dict(src_dict)
        state = MessageWaitResponseState(d.pop("state"))

        wait = AgentWaitRecord.from_dict(d.pop("wait"))

        def _parse_messages(data: object) -> list[CollaborationMessageRecord] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                messages_type_0 = []
                _messages_type_0 = data
                for messages_type_0_item_data in _messages_type_0:
                    messages_type_0_item = CollaborationMessageRecord.from_dict(messages_type_0_item_data)

                    messages_type_0.append(messages_type_0_item)

                return messages_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[CollaborationMessageRecord] | None | Unset, data)

        messages = _parse_messages(d.pop("messages", UNSET))

        def _parse_next_cursor(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))

        def _parse_has_more(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        has_more = _parse_has_more(d.pop("has_more", UNSET))

        def _parse_cursor(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        cursor = _parse_cursor(d.pop("cursor", UNSET))

        def _parse_next_step(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_step = _parse_next_step(d.pop("next_step", UNSET))

        success = d.pop("success", UNSET)

        message_wait_response = cls(
            state=state,
            wait=wait,
            messages=messages,
            next_cursor=next_cursor,
            has_more=has_more,
            cursor=cursor,
            next_step=next_step,
            success=success,
        )

        return message_wait_response
