from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.escalation_action import EscalationAction
    from ..models.escalation_delivery import EscalationDelivery
    from ..models.escalation_message import EscalationMessage
    from ..models.escalation_record import EscalationRecord


T = TypeVar("T", bound="EscalationGetResponse")


@_attrs_define
class EscalationGetResponse:
    """
    Attributes:
        escalation (EscalationRecord):
        messages (list[EscalationMessage]):
        deliveries (list[EscalationDelivery]):
        actions (list[EscalationAction]):
        ask_id (None | str | Unset):
        ask_url (None | str | Unset):
        deprecation (None | str | Unset):
        success (bool | Unset):  Default: True.
    """

    escalation: EscalationRecord
    messages: list[EscalationMessage]
    deliveries: list[EscalationDelivery]
    actions: list[EscalationAction]
    ask_id: None | str | Unset = UNSET
    ask_url: None | str | Unset = UNSET
    deprecation: None | str | Unset = UNSET
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        escalation = self.escalation.to_dict()

        messages = []
        for messages_item_data in self.messages:
            messages_item = messages_item_data.to_dict()
            messages.append(messages_item)

        deliveries = []
        for deliveries_item_data in self.deliveries:
            deliveries_item = deliveries_item_data.to_dict()
            deliveries.append(deliveries_item)

        actions = []
        for actions_item_data in self.actions:
            actions_item = actions_item_data.to_dict()
            actions.append(actions_item)

        ask_id: None | str | Unset
        if isinstance(self.ask_id, Unset):
            ask_id = UNSET
        else:
            ask_id = self.ask_id

        ask_url: None | str | Unset
        if isinstance(self.ask_url, Unset):
            ask_url = UNSET
        else:
            ask_url = self.ask_url

        deprecation: None | str | Unset
        if isinstance(self.deprecation, Unset):
            deprecation = UNSET
        else:
            deprecation = self.deprecation

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "escalation": escalation,
                "messages": messages,
                "deliveries": deliveries,
                "actions": actions,
            }
        )
        if ask_id is not UNSET:
            field_dict["ask_id"] = ask_id
        if ask_url is not UNSET:
            field_dict["ask_url"] = ask_url
        if deprecation is not UNSET:
            field_dict["deprecation"] = deprecation
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.escalation_action import EscalationAction
        from ..models.escalation_delivery import EscalationDelivery
        from ..models.escalation_message import EscalationMessage
        from ..models.escalation_record import EscalationRecord

        d = dict(src_dict)
        escalation = EscalationRecord.from_dict(d.pop("escalation"))

        messages = []
        _messages = d.pop("messages")
        for messages_item_data in _messages:
            messages_item = EscalationMessage.from_dict(messages_item_data)

            messages.append(messages_item)

        deliveries = []
        _deliveries = d.pop("deliveries")
        for deliveries_item_data in _deliveries:
            deliveries_item = EscalationDelivery.from_dict(deliveries_item_data)

            deliveries.append(deliveries_item)

        actions = []
        _actions = d.pop("actions")
        for actions_item_data in _actions:
            actions_item = EscalationAction.from_dict(actions_item_data)

            actions.append(actions_item)

        def _parse_ask_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ask_id = _parse_ask_id(d.pop("ask_id", UNSET))

        def _parse_ask_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ask_url = _parse_ask_url(d.pop("ask_url", UNSET))

        def _parse_deprecation(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        deprecation = _parse_deprecation(d.pop("deprecation", UNSET))

        success = d.pop("success", UNSET)

        escalation_get_response = cls(
            escalation=escalation,
            messages=messages,
            deliveries=deliveries,
            actions=actions,
            ask_id=ask_id,
            ask_url=ask_url,
            deprecation=deprecation,
            success=success,
        )

        escalation_get_response.additional_properties = d
        return escalation_get_response

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
