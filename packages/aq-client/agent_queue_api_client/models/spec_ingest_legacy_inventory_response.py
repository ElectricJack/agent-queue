from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.spec_ingest_legacy_inventory_response_assignments_item import (
        SpecIngestLegacyInventoryResponseAssignmentsItem,
    )


T = TypeVar("T", bound="SpecIngestLegacyInventoryResponse")


@_attrs_define
class SpecIngestLegacyInventoryResponse:
    """
    Attributes:
        assignments (list[SpecIngestLegacyInventoryResponseAssignmentsItem]):
        next_after_id (None | str):
        policy (str):
        success (bool | Unset):  Default: True.
    """

    assignments: list[SpecIngestLegacyInventoryResponseAssignmentsItem]
    next_after_id: None | str
    policy: str
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        assignments = []
        for assignments_item_data in self.assignments:
            assignments_item = assignments_item_data.to_dict()
            assignments.append(assignments_item)

        next_after_id: None | str
        next_after_id = self.next_after_id

        policy = self.policy

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "assignments": assignments,
                "next_after_id": next_after_id,
                "policy": policy,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.spec_ingest_legacy_inventory_response_assignments_item import (
            SpecIngestLegacyInventoryResponseAssignmentsItem,
        )

        d = dict(src_dict)
        assignments = []
        _assignments = d.pop("assignments")
        for assignments_item_data in _assignments:
            assignments_item = SpecIngestLegacyInventoryResponseAssignmentsItem.from_dict(assignments_item_data)

            assignments.append(assignments_item)

        def _parse_next_after_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        next_after_id = _parse_next_after_id(d.pop("next_after_id"))

        policy = d.pop("policy")

        success = d.pop("success", UNSET)

        spec_ingest_legacy_inventory_response = cls(
            assignments=assignments,
            next_after_id=next_after_id,
            policy=policy,
            success=success,
        )

        spec_ingest_legacy_inventory_response.additional_properties = d
        return spec_ingest_legacy_inventory_response

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
