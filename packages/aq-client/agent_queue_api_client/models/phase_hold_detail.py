from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.phase_hold_child import PhaseHoldChild
    from ..models.phase_hold_remedy import PhaseHoldRemedy


T = TypeVar("T", bound="PhaseHoldDetail")


@_attrs_define
class PhaseHoldDetail:
    """Bounded evidence for a phase retained by failed child work.

    Attributes:
        phase_id (str):
        failed_children (list[PhaseHoldChild] | Unset):
        failed_children_total (int | Unset):  Default: 0.
        descendant_blocker_count (int | Unset):  Default: 0.
        remedies (list[PhaseHoldRemedy] | Unset):
    """

    phase_id: str
    failed_children: list[PhaseHoldChild] | Unset = UNSET
    failed_children_total: int | Unset = 0
    descendant_blocker_count: int | Unset = 0
    remedies: list[PhaseHoldRemedy] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        phase_id = self.phase_id

        failed_children: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.failed_children, Unset):
            failed_children = []
            for failed_children_item_data in self.failed_children:
                failed_children_item = failed_children_item_data.to_dict()
                failed_children.append(failed_children_item)

        failed_children_total = self.failed_children_total

        descendant_blocker_count = self.descendant_blocker_count

        remedies: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.remedies, Unset):
            remedies = []
            for remedies_item_data in self.remedies:
                remedies_item = remedies_item_data.to_dict()
                remedies.append(remedies_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "phase_id": phase_id,
            }
        )
        if failed_children is not UNSET:
            field_dict["failed_children"] = failed_children
        if failed_children_total is not UNSET:
            field_dict["failed_children_total"] = failed_children_total
        if descendant_blocker_count is not UNSET:
            field_dict["descendant_blocker_count"] = descendant_blocker_count
        if remedies is not UNSET:
            field_dict["remedies"] = remedies

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.phase_hold_child import PhaseHoldChild
        from ..models.phase_hold_remedy import PhaseHoldRemedy

        d = dict(src_dict)
        phase_id = d.pop("phase_id")

        _failed_children = d.pop("failed_children", UNSET)
        failed_children: list[PhaseHoldChild] | Unset = UNSET
        if _failed_children is not UNSET:
            failed_children = []
            for failed_children_item_data in _failed_children:
                failed_children_item = PhaseHoldChild.from_dict(failed_children_item_data)

                failed_children.append(failed_children_item)

        failed_children_total = d.pop("failed_children_total", UNSET)

        descendant_blocker_count = d.pop("descendant_blocker_count", UNSET)

        _remedies = d.pop("remedies", UNSET)
        remedies: list[PhaseHoldRemedy] | Unset = UNSET
        if _remedies is not UNSET:
            remedies = []
            for remedies_item_data in _remedies:
                remedies_item = PhaseHoldRemedy.from_dict(remedies_item_data)

                remedies.append(remedies_item)

        phase_hold_detail = cls(
            phase_id=phase_id,
            failed_children=failed_children,
            failed_children_total=failed_children_total,
            descendant_blocker_count=descendant_blocker_count,
            remedies=remedies,
        )

        phase_hold_detail.additional_properties = d
        return phase_hold_detail

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
