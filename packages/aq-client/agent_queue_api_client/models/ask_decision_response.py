from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_decision_response_receipt_type_0 import AskDecisionResponseReceiptType0


T = TypeVar("T", bound="AskDecisionResponse")


@_attrs_define
class AskDecisionResponse:
    """
    Attributes:
        success (bool):
        decision_id (str):
        application_state (str):
        created (bool | None | Unset):
        receipt (AskDecisionResponseReceiptType0 | None | Unset):
    """

    success: bool
    decision_id: str
    application_state: str
    created: bool | None | Unset = UNSET
    receipt: AskDecisionResponseReceiptType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.ask_decision_response_receipt_type_0 import AskDecisionResponseReceiptType0

        success = self.success

        decision_id = self.decision_id

        application_state = self.application_state

        created: bool | None | Unset
        if isinstance(self.created, Unset):
            created = UNSET
        else:
            created = self.created

        receipt: dict[str, Any] | None | Unset
        if isinstance(self.receipt, Unset):
            receipt = UNSET
        elif isinstance(self.receipt, AskDecisionResponseReceiptType0):
            receipt = self.receipt.to_dict()
        else:
            receipt = self.receipt

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
                "decision_id": decision_id,
                "application_state": application_state,
            }
        )
        if created is not UNSET:
            field_dict["created"] = created
        if receipt is not UNSET:
            field_dict["receipt"] = receipt

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_decision_response_receipt_type_0 import AskDecisionResponseReceiptType0

        d = dict(src_dict)
        success = d.pop("success")

        decision_id = d.pop("decision_id")

        application_state = d.pop("application_state")

        def _parse_created(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        created = _parse_created(d.pop("created", UNSET))

        def _parse_receipt(data: object) -> AskDecisionResponseReceiptType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                receipt_type_0 = AskDecisionResponseReceiptType0.from_dict(data)

                return receipt_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AskDecisionResponseReceiptType0 | None | Unset, data)

        receipt = _parse_receipt(d.pop("receipt", UNSET))

        ask_decision_response = cls(
            success=success,
            decision_id=decision_id,
            application_state=application_state,
            created=created,
            receipt=receipt,
        )

        ask_decision_response.additional_properties = d
        return ask_decision_response

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
