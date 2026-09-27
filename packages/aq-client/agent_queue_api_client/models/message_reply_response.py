from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

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
        message_ids (list[str] | None | Unset):
        seq (int | None | Unset):
        replayed (bool | None | Unset):
        state (None | str | Unset):
    """

    message_id: str
    reply_id: str
    reply: MessageModel
    message_ids: list[str] | None | Unset = UNSET
    seq: int | None | Unset = UNSET
    replayed: bool | None | Unset = UNSET
    state: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        message_id = self.message_id

        reply_id = self.reply_id

        reply = self.reply.to_dict()

        message_ids: list[str] | None | Unset
        if isinstance(self.message_ids, Unset):
            message_ids = UNSET
        elif isinstance(self.message_ids, list):
            message_ids = self.message_ids

        else:
            message_ids = self.message_ids

        seq: int | None | Unset
        if isinstance(self.seq, Unset):
            seq = UNSET
        else:
            seq = self.seq

        replayed: bool | None | Unset
        if isinstance(self.replayed, Unset):
            replayed = UNSET
        else:
            replayed = self.replayed

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "message_id": message_id,
                "reply_id": reply_id,
                "reply": reply,
            }
        )
        if message_ids is not UNSET:
            field_dict["message_ids"] = message_ids
        if seq is not UNSET:
            field_dict["seq"] = seq
        if replayed is not UNSET:
            field_dict["replayed"] = replayed
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.message_model import MessageModel

        d = dict(src_dict)
        message_id = d.pop("message_id")

        reply_id = d.pop("reply_id")

        reply = MessageModel.from_dict(d.pop("reply"))

        def _parse_message_ids(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                message_ids_type_0 = cast(list[str], data)

                return message_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        message_ids = _parse_message_ids(d.pop("message_ids", UNSET))

        def _parse_seq(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        seq = _parse_seq(d.pop("seq", UNSET))

        def _parse_replayed(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        replayed = _parse_replayed(d.pop("replayed", UNSET))

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        message_reply_response = cls(
            message_id=message_id,
            reply_id=reply_id,
            reply=reply,
            message_ids=message_ids,
            seq=seq,
            replayed=replayed,
            state=state,
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
