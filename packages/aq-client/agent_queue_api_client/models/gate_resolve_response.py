from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.gate_resolve_response_receipt_type_0 import GateResolveResponseReceiptType0


T = TypeVar("T", bound="GateResolveResponse")


@_attrs_define
class GateResolveResponse:
    """
    Attributes:
        gate_id (str):
        success (bool | Unset):  Default: True.
        unblocked_task_ids (list[str] | Unset):
        decision_id (None | str | Unset):
        application_state (None | str | Unset):
        created (bool | None | Unset):
        receipt (GateResolveResponseReceiptType0 | None | Unset):
    """

    gate_id: str
    success: bool | Unset = True
    unblocked_task_ids: list[str] | Unset = UNSET
    decision_id: None | str | Unset = UNSET
    application_state: None | str | Unset = UNSET
    created: bool | None | Unset = UNSET
    receipt: GateResolveResponseReceiptType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.gate_resolve_response_receipt_type_0 import GateResolveResponseReceiptType0

        gate_id = self.gate_id

        success = self.success

        unblocked_task_ids: list[str] | Unset = UNSET
        if not isinstance(self.unblocked_task_ids, Unset):
            unblocked_task_ids = self.unblocked_task_ids

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
        elif isinstance(self.receipt, GateResolveResponseReceiptType0):
            receipt = self.receipt.to_dict()
        else:
            receipt = self.receipt

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "gate_id": gate_id,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if unblocked_task_ids is not UNSET:
            field_dict["unblocked_task_ids"] = unblocked_task_ids
        if decision_id is not UNSET:
            field_dict["decision_id"] = decision_id
        if application_state is not UNSET:
            field_dict["application_state"] = application_state
        if created is not UNSET:
            field_dict["created"] = created
        if receipt is not UNSET:
            field_dict["receipt"] = receipt

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.gate_resolve_response_receipt_type_0 import GateResolveResponseReceiptType0

        d = dict(src_dict)
        gate_id = d.pop("gate_id")

        success = d.pop("success", UNSET)

        unblocked_task_ids = cast(list[str], d.pop("unblocked_task_ids", UNSET))

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

        def _parse_receipt(data: object) -> GateResolveResponseReceiptType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                receipt_type_0 = GateResolveResponseReceiptType0.from_dict(data)

                return receipt_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GateResolveResponseReceiptType0 | None | Unset, data)

        receipt = _parse_receipt(d.pop("receipt", UNSET))

        gate_resolve_response = cls(
            gate_id=gate_id,
            success=success,
            unblocked_task_ids=unblocked_task_ids,
            decision_id=decision_id,
            application_state=application_state,
            created=created,
            receipt=receipt,
        )

        gate_resolve_response.additional_properties = d
        return gate_resolve_response

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
