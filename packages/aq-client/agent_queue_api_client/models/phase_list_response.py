from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.phase_summary import PhaseSummary


T = TypeVar("T", bound="PhaseListResponse")


@_attrs_define
class PhaseListResponse:
    """
    Attributes:
        phases (list[PhaseSummary] | Unset):
    """

    phases: list[PhaseSummary] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        phases: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.phases, Unset):
            phases = []
            for phases_item_data in self.phases:
                phases_item = phases_item_data.to_dict()
                phases.append(phases_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if phases is not UNSET:
            field_dict["phases"] = phases

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.phase_summary import PhaseSummary

        d = dict(src_dict)
        _phases = d.pop("phases", UNSET)
        phases: list[PhaseSummary] | Unset = UNSET
        if _phases is not UNSET:
            phases = []
            for phases_item_data in _phases:
                phases_item = PhaseSummary.from_dict(phases_item_data)

                phases.append(phases_item)

        phase_list_response = cls(
            phases=phases,
        )

        phase_list_response.additional_properties = d
        return phase_list_response

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
