from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.spec_ingest_reconcile_response_errors_item import SpecIngestReconcileResponseErrorsItem


T = TypeVar("T", bound="SpecIngestReconcileResponse")


@_attrs_define
class SpecIngestReconcileResponse:
    """
    Attributes:
        dispatched (int):
        errors (list[SpecIngestReconcileResponseErrorsItem]):
        success (bool | Unset):  Default: True.
    """

    dispatched: int
    errors: list[SpecIngestReconcileResponseErrorsItem]
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        dispatched = self.dispatched

        errors = []
        for errors_item_data in self.errors:
            errors_item = errors_item_data.to_dict()
            errors.append(errors_item)

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "dispatched": dispatched,
                "errors": errors,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.spec_ingest_reconcile_response_errors_item import SpecIngestReconcileResponseErrorsItem

        d = dict(src_dict)
        dispatched = d.pop("dispatched")

        errors = []
        _errors = d.pop("errors")
        for errors_item_data in _errors:
            errors_item = SpecIngestReconcileResponseErrorsItem.from_dict(errors_item_data)

            errors.append(errors_item)

        success = d.pop("success", UNSET)

        spec_ingest_reconcile_response = cls(
            dispatched=dispatched,
            errors=errors,
            success=success,
        )

        spec_ingest_reconcile_response.additional_properties = d
        return spec_ingest_reconcile_response

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
