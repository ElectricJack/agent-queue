from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.escalation_apply_reply_request_expected_type_0 import EscalationApplyReplyRequestExpectedType0


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
        expected (EscalationApplyReplyRequestExpectedType0 | None | Unset):
        preview_id (None | str | Unset):
        option_key (None | str | Unset):
        prepare (bool | None | Unset):
        decision (None | str | Unset):
    """

    escalation_id: str
    reply_id: str
    expected_revision: int
    idempotency_key: str
    action_kind: str
    target_id: str
    expected: EscalationApplyReplyRequestExpectedType0 | None | Unset = UNSET
    preview_id: None | str | Unset = UNSET
    option_key: None | str | Unset = UNSET
    prepare: bool | None | Unset = UNSET
    decision: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.escalation_apply_reply_request_expected_type_0 import EscalationApplyReplyRequestExpectedType0

        escalation_id = self.escalation_id

        reply_id = self.reply_id

        expected_revision = self.expected_revision

        idempotency_key = self.idempotency_key

        action_kind = self.action_kind

        target_id = self.target_id

        expected: dict[str, Any] | None | Unset
        if isinstance(self.expected, Unset):
            expected = UNSET
        elif isinstance(self.expected, EscalationApplyReplyRequestExpectedType0):
            expected = self.expected.to_dict()
        else:
            expected = self.expected

        preview_id: None | str | Unset
        if isinstance(self.preview_id, Unset):
            preview_id = UNSET
        else:
            preview_id = self.preview_id

        option_key: None | str | Unset
        if isinstance(self.option_key, Unset):
            option_key = UNSET
        else:
            option_key = self.option_key

        prepare: bool | None | Unset
        if isinstance(self.prepare, Unset):
            prepare = UNSET
        else:
            prepare = self.prepare

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
        if expected is not UNSET:
            field_dict["expected"] = expected
        if preview_id is not UNSET:
            field_dict["preview_id"] = preview_id
        if option_key is not UNSET:
            field_dict["option_key"] = option_key
        if prepare is not UNSET:
            field_dict["prepare"] = prepare
        if decision is not UNSET:
            field_dict["decision"] = decision

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.escalation_apply_reply_request_expected_type_0 import EscalationApplyReplyRequestExpectedType0

        d = dict(src_dict)
        escalation_id = d.pop("escalation_id")

        reply_id = d.pop("reply_id")

        expected_revision = d.pop("expected_revision")

        idempotency_key = d.pop("idempotency_key")

        action_kind = d.pop("action_kind")

        target_id = d.pop("target_id")

        def _parse_expected(data: object) -> EscalationApplyReplyRequestExpectedType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                expected_type_0 = EscalationApplyReplyRequestExpectedType0.from_dict(data)

                return expected_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EscalationApplyReplyRequestExpectedType0 | None | Unset, data)

        expected = _parse_expected(d.pop("expected", UNSET))

        def _parse_preview_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        preview_id = _parse_preview_id(d.pop("preview_id", UNSET))

        def _parse_option_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        option_key = _parse_option_key(d.pop("option_key", UNSET))

        def _parse_prepare(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        prepare = _parse_prepare(d.pop("prepare", UNSET))

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
            expected=expected,
            preview_id=preview_id,
            option_key=option_key,
            prepare=prepare,
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
