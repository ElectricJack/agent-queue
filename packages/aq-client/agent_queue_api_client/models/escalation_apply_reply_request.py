from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EscalationApplyReplyRequest")


@_attrs_define
class EscalationApplyReplyRequest:
    """
    Attributes:
        escalation_id (str):
        reply_id (str):
        expected_revision (int):
        idempotency_key (str):
        action_kind (str):
        target_id (str):
        decision (None | str | Unset):
    """

    escalation_id: str
    reply_id: str
    expected_revision: int
    idempotency_key: str
    action_kind: str
    target_id: str
    decision: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        escalation_id = self.escalation_id

        reply_id = self.reply_id

        expected_revision = self.expected_revision

        idempotency_key = self.idempotency_key

        action_kind = self.action_kind

        target_id = self.target_id

        decision: None | str | Unset
        if isinstance(self.decision, Unset):
            decision = UNSET
        else:
            decision = self.decision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "escalation_id": escalation_id,
                "reply_id": reply_id,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
                "action_kind": action_kind,
                "target_id": target_id,
            }
        )
        if decision is not UNSET:
            field_dict["decision"] = decision

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        escalation_id = d.pop("escalation_id")

        reply_id = d.pop("reply_id")

        expected_revision = d.pop("expected_revision")

        idempotency_key = d.pop("idempotency_key")

        action_kind = d.pop("action_kind")

        target_id = d.pop("target_id")

        def _parse_decision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        decision = _parse_decision(d.pop("decision", UNSET))

        escalation_apply_reply_request = cls(
            escalation_id=escalation_id,
            reply_id=reply_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            action_kind=action_kind,
            target_id=target_id,
            decision=decision,
        )

        escalation_apply_reply_request.additional_properties = d
        return escalation_apply_reply_request

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
