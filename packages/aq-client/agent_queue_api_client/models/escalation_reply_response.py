from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.escalation_message import EscalationMessage
    from ..models.escalation_record import EscalationRecord


T = TypeVar("T", bound="EscalationReplyResponse")


@_attrs_define
class EscalationReplyResponse:
    """
    Attributes:
        reply (EscalationMessage):
        escalation (EscalationRecord):
        created (bool):
        supervisor_enqueued (bool):
        terminal (bool):
        success (bool | Unset):  Default: True.
    """

    reply: EscalationMessage
    escalation: EscalationRecord
    created: bool
    supervisor_enqueued: bool
    terminal: bool
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        reply = self.reply.to_dict()

        escalation = self.escalation.to_dict()

        created = self.created

        supervisor_enqueued = self.supervisor_enqueued

        terminal = self.terminal

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "reply": reply,
                "escalation": escalation,
                "created": created,
                "supervisor_enqueued": supervisor_enqueued,
                "terminal": terminal,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.escalation_message import EscalationMessage
        from ..models.escalation_record import EscalationRecord

        d = dict(src_dict)
        reply = EscalationMessage.from_dict(d.pop("reply"))

        escalation = EscalationRecord.from_dict(d.pop("escalation"))

        created = d.pop("created")

        supervisor_enqueued = d.pop("supervisor_enqueued")

        terminal = d.pop("terminal")

        success = d.pop("success", UNSET)

        escalation_reply_response = cls(
            reply=reply,
            escalation=escalation,
            created=created,
            supervisor_enqueued=supervisor_enqueued,
            terminal=terminal,
            success=success,
        )

        escalation_reply_response.additional_properties = d
        return escalation_reply_response

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
