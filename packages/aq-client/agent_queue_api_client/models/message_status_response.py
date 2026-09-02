from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.message_model import MessageModel


T = TypeVar("T", bound="MessageStatusResponse")


@_attrs_define
class MessageStatusResponse:
    """
    Attributes:
        message_id (str):
        state (str):
        message (MessageModel): Rendered message dict (see ``src/commands/message_commands.py::message_to_dict``).
        via (None | str | Unset):
        delivered_at (float | None | Unset):
        acknowledged_at (float | None | Unset):
    """

    message_id: str
    state: str
    message: MessageModel
    via: None | str | Unset = UNSET
    delivered_at: float | None | Unset = UNSET
    acknowledged_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        message_id = self.message_id

        state = self.state

        message = self.message.to_dict()

        via: None | str | Unset
        if isinstance(self.via, Unset):
            via = UNSET
        else:
            via = self.via

        delivered_at: float | None | Unset
        if isinstance(self.delivered_at, Unset):
            delivered_at = UNSET
        else:
            delivered_at = self.delivered_at

        acknowledged_at: float | None | Unset
        if isinstance(self.acknowledged_at, Unset):
            acknowledged_at = UNSET
        else:
            acknowledged_at = self.acknowledged_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "message_id": message_id,
                "state": state,
                "message": message,
            }
        )
        if via is not UNSET:
            field_dict["via"] = via
        if delivered_at is not UNSET:
            field_dict["delivered_at"] = delivered_at
        if acknowledged_at is not UNSET:
            field_dict["acknowledged_at"] = acknowledged_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.message_model import MessageModel

        d = dict(src_dict)
        message_id = d.pop("message_id")

        state = d.pop("state")

        message = MessageModel.from_dict(d.pop("message"))

        def _parse_via(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        via = _parse_via(d.pop("via", UNSET))

        def _parse_delivered_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        delivered_at = _parse_delivered_at(d.pop("delivered_at", UNSET))

        def _parse_acknowledged_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        acknowledged_at = _parse_acknowledged_at(d.pop("acknowledged_at", UNSET))

        message_status_response = cls(
            message_id=message_id,
            state=state,
            message=message,
            via=via,
            delivered_at=delivered_at,
            acknowledged_at=acknowledged_at,
        )

        message_status_response.additional_properties = d
        return message_status_response

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
