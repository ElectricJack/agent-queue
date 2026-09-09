from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

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
        success (bool | Unset):  Default: True.
    """

    escalation: EscalationRecord
    messages: list[EscalationMessage]
    deliveries: list[EscalationDelivery]
    actions: list[EscalationAction]
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

        success = d.pop("success", UNSET)

        escalation_get_response = cls(
            escalation=escalation,
            messages=messages,
            deliveries=deliveries,
            actions=actions,
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
