from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_preview import AskPreview
    from ..models.ask_version import AskVersion
    from ..models.escalation_apply_reply_response_receipt_type_0 import EscalationApplyReplyResponseReceiptType0
    from ..models.escalation_record import EscalationRecord


T = TypeVar("T", bound="EscalationApplyReplyResponse")


@_attrs_define
class EscalationApplyReplyResponse:
    """
    Attributes:
        ask_id (str):
        success (bool | Unset):  Default: True.
        applied (bool | Unset):  Default: False.
        replayed (bool | Unset):  Default: False.
        preview (AskPreview | None | Unset):
        expected (AskVersion | None | Unset):
        decision_id (None | str | Unset):
        application_state (None | str | Unset):
        created (bool | None | Unset):
        receipt (EscalationApplyReplyResponseReceiptType0 | None | Unset):
        escalation (EscalationRecord | None | Unset):
    """

    ask_id: str
    success: bool | Unset = True
    applied: bool | Unset = False
    replayed: bool | Unset = False
    preview: AskPreview | None | Unset = UNSET
    expected: AskVersion | None | Unset = UNSET
    decision_id: None | str | Unset = UNSET
    application_state: None | str | Unset = UNSET
    created: bool | None | Unset = UNSET
    receipt: EscalationApplyReplyResponseReceiptType0 | None | Unset = UNSET
    escalation: EscalationRecord | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.ask_preview import AskPreview
        from ..models.ask_version import AskVersion
        from ..models.escalation_apply_reply_response_receipt_type_0 import EscalationApplyReplyResponseReceiptType0
        from ..models.escalation_record import EscalationRecord

        ask_id = self.ask_id

        success = self.success

        applied = self.applied

        replayed = self.replayed

        preview: dict[str, Any] | None | Unset
        if isinstance(self.preview, Unset):
            preview = UNSET
        elif isinstance(self.preview, AskPreview):
            preview = self.preview.to_dict()
        else:
            preview = self.preview

        expected: dict[str, Any] | None | Unset
        if isinstance(self.expected, Unset):
            expected = UNSET
        elif isinstance(self.expected, AskVersion):
            expected = self.expected.to_dict()
        else:
            expected = self.expected

        decision_id: None | str | Unset
        if isinstance(self.decision_id, Unset):
            decision_id = UNSET
        else:
            decision_id = self.decision_id

        application_state: None | str | Unset
        if isinstance(self.application_state, Unset):
            application_state = UNSET
        else:
            application_state = self.application_state

        created: bool | None | Unset
        if isinstance(self.created, Unset):
            created = UNSET
        else:
            created = self.created

        receipt: dict[str, Any] | None | Unset
        if isinstance(self.receipt, Unset):
            receipt = UNSET
        elif isinstance(self.receipt, EscalationApplyReplyResponseReceiptType0):
            receipt = self.receipt.to_dict()
        else:
            receipt = self.receipt

        escalation: dict[str, Any] | None | Unset
        if isinstance(self.escalation, Unset):
            escalation = UNSET
        elif isinstance(self.escalation, EscalationRecord):
            escalation = self.escalation.to_dict()
        else:
            escalation = self.escalation

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ask_id": ask_id,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if applied is not UNSET:
            field_dict["applied"] = applied
        if replayed is not UNSET:
            field_dict["replayed"] = replayed
        if preview is not UNSET:
            field_dict["preview"] = preview
        if expected is not UNSET:
            field_dict["expected"] = expected
        if decision_id is not UNSET:
            field_dict["decision_id"] = decision_id
        if application_state is not UNSET:
            field_dict["application_state"] = application_state
        if created is not UNSET:
            field_dict["created"] = created
        if receipt is not UNSET:
            field_dict["receipt"] = receipt
        if escalation is not UNSET:
            field_dict["escalation"] = escalation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_preview import AskPreview
        from ..models.ask_version import AskVersion
        from ..models.escalation_apply_reply_response_receipt_type_0 import EscalationApplyReplyResponseReceiptType0
        from ..models.escalation_record import EscalationRecord

        d = dict(src_dict)
        ask_id = d.pop("ask_id")

        success = d.pop("success", UNSET)

        applied = d.pop("applied", UNSET)

        replayed = d.pop("replayed", UNSET)

        def _parse_preview(data: object) -> AskPreview | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                preview_type_0 = AskPreview.from_dict(data)

                return preview_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AskPreview | None | Unset, data)

        preview = _parse_preview(d.pop("preview", UNSET))

        def _parse_expected(data: object) -> AskVersion | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                expected_type_0 = AskVersion.from_dict(data)

                return expected_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AskVersion | None | Unset, data)

        expected = _parse_expected(d.pop("expected", UNSET))

        def _parse_decision_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        decision_id = _parse_decision_id(d.pop("decision_id", UNSET))

        def _parse_application_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        application_state = _parse_application_state(d.pop("application_state", UNSET))

        def _parse_created(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        created = _parse_created(d.pop("created", UNSET))

        def _parse_receipt(data: object) -> EscalationApplyReplyResponseReceiptType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                receipt_type_0 = EscalationApplyReplyResponseReceiptType0.from_dict(data)

                return receipt_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EscalationApplyReplyResponseReceiptType0 | None | Unset, data)

        receipt = _parse_receipt(d.pop("receipt", UNSET))

        def _parse_escalation(data: object) -> EscalationRecord | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                escalation_type_0 = EscalationRecord.from_dict(data)

                return escalation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EscalationRecord | None | Unset, data)

        escalation = _parse_escalation(d.pop("escalation", UNSET))

        escalation_apply_reply_response = cls(
            ask_id=ask_id,
            success=success,
            applied=applied,
            replayed=replayed,
            preview=preview,
            expected=expected,
            decision_id=decision_id,
            application_state=application_state,
            created=created,
            receipt=receipt,
            escalation=escalation,
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
