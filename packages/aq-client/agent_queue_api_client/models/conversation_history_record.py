from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.conversation_history_record_state import ConversationHistoryRecordState

if TYPE_CHECKING:
    from ..models.conversation_input_record import ConversationInputRecord


T = TypeVar("T", bound="ConversationHistoryRecord")


@_attrs_define
class ConversationHistoryRecord:
    """
    Attributes:
        id (str):
        transport (str):
        guild_id (str):
        channel_id (str):
        external_root_message_id (str):
        external_thread_id (None | str):
        thread_id (str):
        created_by (str):
        audience (list[str]):
        state (ConversationHistoryRecordState):
        created_at (float):
        updated_at (float):
        closed_at (float | None):
        inputs (list[ConversationInputRecord]):
        next_before (float | None):
    """

    id: str
    transport: str
    guild_id: str
    channel_id: str
    external_root_message_id: str
    external_thread_id: None | str
    thread_id: str
    created_by: str
    audience: list[str]
    state: ConversationHistoryRecordState
    created_at: float
    updated_at: float
    closed_at: float | None
    inputs: list[ConversationInputRecord]
    next_before: float | None
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        transport = self.transport

        guild_id = self.guild_id

        channel_id = self.channel_id

        external_root_message_id = self.external_root_message_id

        external_thread_id: None | str
        external_thread_id = self.external_thread_id

        thread_id = self.thread_id

        created_by = self.created_by

        audience = self.audience

        state = self.state.value

        created_at = self.created_at

        updated_at = self.updated_at

        closed_at: float | None
        closed_at = self.closed_at

        inputs = []
        for inputs_item_data in self.inputs:
            inputs_item = inputs_item_data.to_dict()
            inputs.append(inputs_item)

        next_before: float | None
        next_before = self.next_before

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "transport": transport,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "external_root_message_id": external_root_message_id,
                "external_thread_id": external_thread_id,
                "thread_id": thread_id,
                "created_by": created_by,
                "audience": audience,
                "state": state,
                "created_at": created_at,
                "updated_at": updated_at,
                "closed_at": closed_at,
                "inputs": inputs,
                "next_before": next_before,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.conversation_input_record import ConversationInputRecord

        d = dict(src_dict)
        id = d.pop("id")

        transport = d.pop("transport")

        guild_id = d.pop("guild_id")

        channel_id = d.pop("channel_id")

        external_root_message_id = d.pop("external_root_message_id")

        def _parse_external_thread_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        external_thread_id = _parse_external_thread_id(d.pop("external_thread_id"))

        thread_id = d.pop("thread_id")

        created_by = d.pop("created_by")

        audience = cast(list[str], d.pop("audience"))

        state = ConversationHistoryRecordState(d.pop("state"))

        created_at = d.pop("created_at")

        updated_at = d.pop("updated_at")

        def _parse_closed_at(data: object) -> float | None:
            if data is None:
                return data
            return cast(float | None, data)

        closed_at = _parse_closed_at(d.pop("closed_at"))

        inputs = []
        _inputs = d.pop("inputs")
        for inputs_item_data in _inputs:
            inputs_item = ConversationInputRecord.from_dict(inputs_item_data)

            inputs.append(inputs_item)

        def _parse_next_before(data: object) -> float | None:
            if data is None:
                return data
            return cast(float | None, data)

        next_before = _parse_next_before(d.pop("next_before"))

        conversation_history_record = cls(
            id=id,
            transport=transport,
            guild_id=guild_id,
            channel_id=channel_id,
            external_root_message_id=external_root_message_id,
            external_thread_id=external_thread_id,
            thread_id=thread_id,
            created_by=created_by,
            audience=audience,
            state=state,
            created_at=created_at,
            updated_at=updated_at,
            closed_at=closed_at,
            inputs=inputs,
            next_before=next_before,
        )

        conversation_history_record.additional_properties = d
        return conversation_history_record

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
