from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.escalation_record import EscalationRecord


T = TypeVar("T", bound="EscalationListResponse")


@_attrs_define
class EscalationListResponse:
    """
    Attributes:
        escalations (list[EscalationRecord]):
        count (int):
        success (bool | Unset):  Default: True.
    """

    escalations: list[EscalationRecord]
    count: int
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        escalations = []
        for escalations_item_data in self.escalations:
            escalations_item = escalations_item_data.to_dict()
            escalations.append(escalations_item)

        count = self.count

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "escalations": escalations,
                "count": count,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.escalation_record import EscalationRecord

        d = dict(src_dict)
        escalations = []
        _escalations = d.pop("escalations")
        for escalations_item_data in _escalations:
            escalations_item = EscalationRecord.from_dict(escalations_item_data)

            escalations.append(escalations_item)

        count = d.pop("count")

        success = d.pop("success", UNSET)

        escalation_list_response = cls(
            escalations=escalations,
            count=count,
            success=success,
        )

        escalation_list_response.additional_properties = d
        return escalation_list_response

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
