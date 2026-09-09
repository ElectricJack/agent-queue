from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.escalation_action import EscalationAction
    from ..models.escalation_apply_reply_response_action_result_type_0 import (
        EscalationApplyReplyResponseActionResultType0,
    )
    from ..models.escalation_record import EscalationRecord


T = TypeVar("T", bound="EscalationApplyReplyResponse")


@_attrs_define
class EscalationApplyReplyResponse:
    """
    Attributes:
        applied (bool):
        replayed (bool):
        action (EscalationAction):
        escalation (EscalationRecord):
        success (bool | Unset):  Default: True.
        action_result (EscalationApplyReplyResponseActionResultType0 | None | Unset):
    """

    applied: bool
    replayed: bool
    action: EscalationAction
    escalation: EscalationRecord
    success: bool | Unset = True
    action_result: EscalationApplyReplyResponseActionResultType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.escalation_apply_reply_response_action_result_type_0 import (
            EscalationApplyReplyResponseActionResultType0,
        )

        applied = self.applied

        replayed = self.replayed

        action = self.action.to_dict()

        escalation = self.escalation.to_dict()

        success = self.success

        action_result: dict[str, Any] | None | Unset
        if isinstance(self.action_result, Unset):
            action_result = UNSET
        elif isinstance(self.action_result, EscalationApplyReplyResponseActionResultType0):
            action_result = self.action_result.to_dict()
        else:
            action_result = self.action_result

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "applied": applied,
                "replayed": replayed,
                "action": action,
                "escalation": escalation,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if action_result is not UNSET:
            field_dict["action_result"] = action_result

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.escalation_action import EscalationAction
        from ..models.escalation_apply_reply_response_action_result_type_0 import (
            EscalationApplyReplyResponseActionResultType0,
        )
        from ..models.escalation_record import EscalationRecord

        d = dict(src_dict)
        applied = d.pop("applied")

        replayed = d.pop("replayed")

        action = EscalationAction.from_dict(d.pop("action"))

        escalation = EscalationRecord.from_dict(d.pop("escalation"))

        success = d.pop("success", UNSET)

        def _parse_action_result(data: object) -> EscalationApplyReplyResponseActionResultType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                action_result_type_0 = EscalationApplyReplyResponseActionResultType0.from_dict(data)

                return action_result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EscalationApplyReplyResponseActionResultType0 | None | Unset, data)

        action_result = _parse_action_result(d.pop("action_result", UNSET))

        escalation_apply_reply_response = cls(
            applied=applied,
            replayed=replayed,
            action=action,
            escalation=escalation,
            success=success,
            action_result=action_result,
        )

        escalation_apply_reply_response.additional_properties = d
        return escalation_apply_reply_response

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
