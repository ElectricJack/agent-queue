from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.message_model import MessageModel


T = TypeVar("T", bound="MessageInboxResponse")


@_attrs_define
class MessageInboxResponse:
    """
    Attributes:
        to_kind (str):
        to_id (str):
        count (int | Unset):  Default: 0.
        injected (int | None | Unset):
        archived (int | None | Unset):
        messages (list[MessageModel] | Unset):
    """

    to_kind: str
    to_id: str
    count: int | Unset = 0
    injected: int | None | Unset = UNSET
    archived: int | None | Unset = UNSET
    messages: list[MessageModel] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        to_kind = self.to_kind

        to_id = self.to_id

        count = self.count

        injected: int | None | Unset
        if isinstance(self.injected, Unset):
            injected = UNSET
        else:
            injected = self.injected

        archived: int | None | Unset
        if isinstance(self.archived, Unset):
            archived = UNSET
        else:
            archived = self.archived

        messages: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.messages, Unset):
            messages = []
            for messages_item_data in self.messages:
                messages_item = messages_item_data.to_dict()
                messages.append(messages_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "to_kind": to_kind,
                "to_id": to_id,
            }
        )
        if count is not UNSET:
            field_dict["count"] = count
        if injected is not UNSET:
            field_dict["injected"] = injected
        if archived is not UNSET:
            field_dict["archived"] = archived
        if messages is not UNSET:
            field_dict["messages"] = messages

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.message_model import MessageModel

        d = dict(src_dict)
        to_kind = d.pop("to_kind")

        to_id = d.pop("to_id")

        count = d.pop("count", UNSET)

        def _parse_injected(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        injected = _parse_injected(d.pop("injected", UNSET))

        def _parse_archived(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        archived = _parse_archived(d.pop("archived", UNSET))

        _messages = d.pop("messages", UNSET)
        messages: list[MessageModel] | Unset = UNSET
        if _messages is not UNSET:
            messages = []
            for messages_item_data in _messages:
                messages_item = MessageModel.from_dict(messages_item_data)

                messages.append(messages_item)

        message_inbox_response = cls(
            to_kind=to_kind,
            to_id=to_id,
            count=count,
            injected=injected,
            archived=archived,
            messages=messages,
        )

        message_inbox_response.additional_properties = d
        return message_inbox_response

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
